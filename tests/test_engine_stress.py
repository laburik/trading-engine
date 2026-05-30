# =============================================================================
# tests/test_engine_stress.py — Stress / concurrency test untuk ENGINE
# =============================================================================
# Fokus: lapisan EKSEKUSI (execution.place_order + _live_place_order_async),
# BUKAN strategi. Kita suapi engine dengan sinyal sintetik yang timing-nya
# dikontrol presisi untuk MEMAKSA race condition yang di run demo tidak terpicu.
#
# Yang diuji:
#   1. In-flight guard benar-benar mencegah dispatch order kembar saat banyak
#      "tick" menembak place_order sebelum order pertama selesai (race nyata).
#   2. Guard reset dengan benar setelah order GAGAL/exception → bot tidak
#      ter-lock permanen (kalau tidak reset, bot berhenti trading selamanya).
#   3. Burst tinggi (100 fire beruntun) → tetap cuma 1 yang ter-dispatch.
#   4. Race pada CLOSE → cuma 1 clear (tidak double-close).
#
# Pola import & mock mengikuti tests/test_execution.py.
# =============================================================================
from __future__ import annotations

import asyncio
import time
import types
from unittest.mock import MagicMock

import pytest
from sortedcontainers import SortedDict

# Import execution LANGSUNG (conftest.py sudah menambah engine/ ke sys.path dan
# autouse fixture mock ccxt exchange). Sengaja TIDAK pakai trik mock-and-restore
# sys.modules seperti test_execution.py: trik itu mengubah keanggotaan
# sys.modules["trade_logger"] yang memicu race daemon-writer di test_trade_logger
# saat full-suite. Import langsung tidak menyentuh state global modul lain.
# Dependensi berat (ccxt_client/data_stream/position_manager) di-override per-test
# lewat fixture `eng` di bawah, jadi tidak ada network call.
import execution  # type: ignore[import-not-found]


# =============================================================================
# FIXTURE — engine dengan ds & position_manager terkontrol, MODE=demo (live path)
# =============================================================================
@pytest.fixture
def eng(monkeypatch):
    ds_stub = types.SimpleNamespace(
        best_bid={"price": 1.500, "qty": 1000.0},
        best_ask={"price": 1.501, "qty": 1000.0},
        best_bid_updated_at=time.time(),
        best_ask_updated_at=time.time(),
        orderbook_snapshot={
            "bids": SortedDict({1.498: 300.0, 1.499: 400.0, 1.500: 5000.0}),
            "asks": SortedDict({1.501: 5000.0, 1.502: 400.0, 1.503: 300.0}),
        },
    )
    pm = MagicMock()
    pm.get_position.return_value = {"side": "none", "entry_price": 0.0, "qty": 0.0, "balance": 1000.0}
    pm.set_position_optimistic = MagicMock()
    pm.clear_position_optimistic = MagicMock()

    monkeypatch.setattr(execution, "data_stream", ds_stub)
    monkeypatch.setattr(execution, "position_manager", pm)
    monkeypatch.setattr(execution, "log_trade", lambda *a, **kw: None)
    monkeypatch.setattr(execution, "MODE", "demo")
    monkeypatch.setattr(execution, "_qty_step_cache", 0.1)
    monkeypatch.setattr(execution, "SYMBOL", "XRPUSDT")
    monkeypatch.setattr(execution, "FEE_RATE", 0.0002)
    monkeypatch.setattr(execution, "SLIPPAGE_TOLERANCE", 0.01)
    # Selalu mulai dari kondisi guard bersih (test lain mungkin meninggalkan True)
    monkeypatch.setattr(execution, "_live_order_in_flight", False)
    return execution, ds_stub, pm


async def _drain(loops: int = 20, dt: float = 0.01) -> None:
    """Beri kesempatan event loop menjalankan task fire-and-forget yang pending."""
    for _ in range(loops):
        await asyncio.sleep(dt)


# =============================================================================
# 1. RACE — guard mencegah dispatch kembar saat order pertama masih in-flight
# =============================================================================
class TestConcurrencyGuard:
    def test_guard_prevents_concurrent_dispatch(self, eng):
        execm, ds, pm = eng
        started = {"n": 0}

        async def scenario():
            release = asyncio.Event()

            async def slow_order(*a, **kw):
                started["n"] += 1
                await release.wait()  # tahan order pertama tetap "in-flight"
                return {"id": "x", "status": "closed", "filled": 4.4}

            # set via fixture's execution.exchange
            import unittest.mock as m
            with m.patch.object(execm.exchange, "create_order", slow_order):
                # 10 "tick" menembak place_order sebelum order pertama selesai
                for _ in range(10):
                    execm.place_order({"action": "buy", "qty": 4.4, "price": 1.501, "reason": "race"})

                await _drain(loops=10)  # biarkan task pertama jalan sampai blok di create_order

                # HANYA 1 order yang benar-benar mulai; sisanya di-skip guard
                assert started["n"] == 1, f"Guard bocor: {started['n']} order ter-dispatch (harus 1)"
                assert execm._live_order_in_flight

                release.set()           # lepas order pertama → selesai
                await _drain(loops=10)

            # Guard reset, hanya 1 posisi optimistic di-set, tidak ada stacking
            assert not execm._live_order_in_flight, "Guard tidak reset setelah order selesai"
            assert pm.set_position_optimistic.call_count == 1
            assert started["n"] == 1

        asyncio.run(scenario())

    def test_burst_100_fires_single_dispatch(self, eng):
        """Burst ekstrem: 100 fire beruntun → tetap cuma 1 dispatch."""
        execm, ds, pm = eng
        started = {"n": 0}

        async def scenario():
            release = asyncio.Event()

            async def slow_order(*a, **kw):
                started["n"] += 1
                await release.wait()
                return {"id": "x", "status": "closed", "filled": 4.4}

            import unittest.mock as m
            with m.patch.object(execm.exchange, "create_order", slow_order):
                for _ in range(100):
                    execm.place_order({"action": "buy", "qty": 4.4, "price": 1.501, "reason": "burst"})
                await _drain(loops=10)
                assert started["n"] == 1, f"{started['n']} dispatch dari 100 fire (harus 1)"
                release.set()
                await _drain(loops=10)
            assert not execm._live_order_in_flight
            assert pm.set_position_optimistic.call_count == 1

        asyncio.run(scenario())

    def test_close_race_single_clear(self, eng):
        """Race pada CLOSE: 10 percobaan close beruntun → cuma 1 yang dispatch & 1 clear."""
        execm, ds, pm = eng
        pm.get_position.return_value = {"side": "long", "entry_price": 1.48, "qty": 4.4, "balance": 1000.0}
        started = {"n": 0}

        async def scenario():
            release = asyncio.Event()

            async def slow_order(*a, **kw):
                started["n"] += 1
                await release.wait()
                return {"id": "x", "status": "closed", "filled": 4.4}

            import unittest.mock as m
            with m.patch.object(execm.exchange, "create_order", slow_order):
                for _ in range(10):
                    execm.place_order({"action": "close", "qty": 4.4, "price": 1.5, "reason": "close-race"})
                await _drain(loops=10)
                assert started["n"] == 1
                release.set()
                await _drain(loops=10)
            assert pm.clear_position_optimistic.call_count == 1, "Double-close terjadi!"
            assert not execm._live_order_in_flight

        asyncio.run(scenario())


# =============================================================================
# 2. LOCKOUT RESILIENCE — guard WAJIB reset walau order gagal/exception
# =============================================================================
class TestGuardLockoutResilience:
    def test_guard_resets_after_repeated_exception(self, eng):
        """create_order selalu raise → setelah retry habis, guard HARUS reset (bukan lock permanen)."""
        execm, ds, pm = eng
        calls = {"n": 0}

        async def boom(*a, **kw):
            calls["n"] += 1
            raise RuntimeError("simulated exchange meltdown")

        async def scenario():
            import unittest.mock as m
            with m.patch.object(execm.exchange, "create_order", boom):
                execm.place_order({"action": "buy", "qty": 4.4, "price": 1.501, "reason": "fail"})
                # RETRY_DELAY_MS≈20ms × MAX_RETRY → beri waktu cukup
                await _drain(loops=40, dt=0.02)
            assert not execm._live_order_in_flight, "BOT TER-LOCK: guard tidak reset setelah gagal!"
            # order tidak pernah berhasil → tidak ada state optimistic
            pm.set_position_optimistic.assert_not_called()

        asyncio.run(scenario())

    def test_can_dispatch_again_after_failure(self, eng):
        """Setelah order gagal & guard reset, order BERIKUTNYA harus bisa jalan."""
        execm, ds, pm = eng
        outcomes = {"calls": 0}

        async def scenario():
            import unittest.mock as m

            async def fail_once(*a, **kw):
                outcomes["calls"] += 1
                raise RuntimeError("fail #1 burst")

            with m.patch.object(execm.exchange, "create_order", fail_once):
                execm.place_order({"action": "buy", "qty": 4.4, "price": 1.501, "reason": "fail"})
                await _drain(loops=40, dt=0.02)
            assert not execm._live_order_in_flight

            async def ok_order(*a, **kw):
                return {"id": "ok", "status": "closed", "filled": 4.4}

            with m.patch.object(execm.exchange, "create_order", ok_order):
                execm.place_order({"action": "buy", "qty": 4.4, "price": 1.501, "reason": "retry-after"})
                await _drain(loops=10)
            # Order kedua berhasil → posisi optimistic ter-set
            assert pm.set_position_optimistic.call_count == 1

        asyncio.run(scenario())
