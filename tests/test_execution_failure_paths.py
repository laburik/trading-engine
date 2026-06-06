# =============================================================================
# tests/test_execution_failure_paths.py — Jalur UANG: cara-cara GAGAL exchange
# =============================================================================
# Melengkapi tests/test_execution.py (parser & happy-path) dengan menguji TIAP
# mode gagal di _live_place_order_async() lewat jalur PENUH (bukan cuma parser),
# plus 1 test regresi untuk bug duplicate-fire, dan matriks parametrized.
#
# Peta ke spec densitas test 1.5:1 (jalur uang):
#   A. Jalur GAGAL exchange   → TestExchangeFailurePaths
#   (regresi bug lama)        → TestDuplicateFireRegression
#   E. Matriks parametrized   → TestOrderMatrix
#
# Gaya assertion (sengaja tahan-refactor):
#   - Hasil di-cek lewat STATE position_manager ASLI (pm.get_position()), BUKAN
#     mock.assert_called/call_args — supaya rename/ubah-API internal tidak bikin
#     test merah palsu.
#   - Yang tetap di-cek lewat "apa yang dipanggil" HANYA di batas exchange
#     (create_order params/amount/type, retry count, backoff). Itu kontrak nyata
#     ke Bybit, bukan detail implementasi internal.
#
# Yang SUDAH dicover di test_execution.py (tidak diduplikasi di sini):
#   - 110017 clears optimistic / no-retry, in-flight guard, partial CLOSE,
#     silent-reject di level _parse_order_fill.
#
# Setup import sama dengan test_execution.py: execution.py punya import
# side-effect berat (ccxt_client, data_stream, position_manager, trade_logger),
# jadi kita mock saat import lalu restore sys.modules.
# =============================================================================
from __future__ import annotations

import asyncio
import sys
import time
import types
from unittest.mock import MagicMock

import pytest
from sortedcontainers import SortedDict


# -----------------------------------------------------------------------------
# Isolated import (mirror test_execution.py)
# -----------------------------------------------------------------------------
_SAVED_MODULES: dict[str, object] = {}
_TO_MOCK = ["ccxt_client", "data_stream", "position_manager", "trade_logger"]

for _name in _TO_MOCK:
    _SAVED_MODULES[_name] = sys.modules.get(_name)
    sys.modules[_name] = MagicMock()
_SAVED_MODULES["execution"] = sys.modules.pop("execution", None)

try:
    import execution  # type: ignore[import-not-found]
finally:
    for _name, _orig in _SAVED_MODULES.items():
        if _orig is not None:
            sys.modules[_name] = _orig  # type: ignore[assignment]
        else:
            sys.modules.pop(_name, None)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _instant_sleep(monkeypatch, recorder: list[float] | None = None) -> None:
    """Patch asyncio.sleep → no-op instan (opsional rekam durasi backoff)."""

    async def _sleep(delay: float = 0.0, *a, **kw):  # type: ignore[no-untyped-def]
        if recorder is not None:
            recorder.append(delay)
        return None

    monkeypatch.setattr(asyncio, "sleep", _sleep)


def _make_ds_stub() -> types.SimpleNamespace:
    """data_stream stub: fresh bid/ask + orderbook 3 level per sisi (depth 1200)."""
    return types.SimpleNamespace(
        best_bid={"price": 1.500, "qty": 1000.0},
        best_ask={"price": 1.501, "qty": 1000.0},
        best_bid_updated_at=time.time(),
        best_ask_updated_at=time.time(),
        orderbook_snapshot={
            "bids": SortedDict({1.498: 300.0, 1.499: 400.0, 1.500: 500.0}),
            "asks": SortedDict({1.501: 500.0, 1.502: 400.0, 1.503: 300.0}),
        },
    )


# =============================================================================
# FIXTURE — execution dengan position_manager ASLI (assert ke STATE, bukan mock)
# =============================================================================
@pytest.fixture
def integ(monkeypatch):
    import position_manager as real_pm

    # Reset state real pm ke default (paper-style balance) supaya tidak bocor.
    real_pm._state.update({
        "side": "none", "entry_price": 0.0, "qty": 0.0, "balance": 1000.0,
        "unrealized_pnl": 0.0, "equity": 1000.0, "realized_pnl_total": 0.0,
        "total_fees": 0.0, "open_time": None,
    })

    ds_stub = _make_ds_stub()
    monkeypatch.setattr(execution, "data_stream", ds_stub)
    monkeypatch.setattr(execution, "position_manager", real_pm)
    monkeypatch.setattr(execution, "log_trade", lambda *a, **kw: None)
    monkeypatch.setattr(execution, "_qty_step_cache", 0.1)
    monkeypatch.setattr(execution, "FEE_RATE", 0.0002)
    monkeypatch.setattr(execution, "SYMBOL", "XRPUSDT")
    monkeypatch.setattr(execution, "SLIPPAGE_TOLERANCE", 0.0005)
    monkeypatch.setattr(execution, "CANCEL_ON_PARTIAL", False)
    monkeypatch.setattr(execution, "MODE", "demo")
    monkeypatch.setattr(execution, "_live_order_in_flight", False)
    return execution, real_pm, ds_stub


# =============================================================================
# A. JALUR GAGAL EXCHANGE — tiap mode gagal = 1 test
# =============================================================================
class TestExchangeFailurePaths:
    def test_reduceonly_hedge_fallback_retries_without_flag(self, integ, monkeypatch):
        """
        110025 (Hedge Mode) saat close → retry TANPA reduceOnly (execution.py:652).
        Batas exchange: attempt 1 reduceOnly=True ditolak, attempt 2 reduceOnly=False.
        State nyata: posisi ter-clear setelah close penuh.
        """
        execm, pm, _ = integ
        pm._state.update({"side": "long", "entry_price": 1.5, "qty": 100.0})
        import ccxt.pro as ccxt

        params_seen: list[dict] = []

        async def _fake_order(*a, **kw):
            params_seen.append(dict(kw.get("params", {})))
            if len(params_seen) == 1:
                raise ccxt.BadRequest(
                    'bybit {"retCode":110025,"retMsg":"reduceOnly order not allowed in Hedge Mode"}'
                )
            return {"id": "ok", "status": "closed", "filled": 100.0}

        monkeypatch.setattr(execm.exchange, "create_order", _fake_order, raising=False)
        _instant_sleep(monkeypatch)

        result = asyncio.run(execm._live_place_order_async(
            {"action": "close", "reason": "tp", "qty": 100.0, "price": 1.5}
        ))

        # Batas exchange (kontrak ke Bybit): retry terjadi & flag dilepas di attempt-2.
        assert len(params_seen) == 2, "harus retry 1x setelah 110025"
        assert params_seen[0]["reduceOnly"] is True, "attempt pertama pakai reduceOnly"
        assert params_seen[1]["reduceOnly"] is False, "retry HARUS tanpa reduceOnly"
        assert result["status"] == "filled"
        # State nyata: posisi habis (close penuh) — bukan cek 'fungsi dipanggil'.
        pos = pm.get_position()
        assert pos["side"] == "none"
        assert pos["qty"] == 0.0

    def test_rate_limit_429_backoff_then_succeeds(self, integ, monkeypatch):
        """429 di attempt 1 → backoff (sleep>0) → attempt 2 sukses (execution.py:698)."""
        execm, pm, _ = integ
        import ccxt.pro as ccxt

        calls = {"n": 0}
        sleeps: list[float] = []

        async def _fake_order(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ccxt.RateLimitExceeded("bybit 429 Too Many Requests")
            return {"id": "ok", "status": "closed", "filled": 100.0}

        monkeypatch.setattr(execm.exchange, "create_order", _fake_order, raising=False)
        _instant_sleep(monkeypatch, recorder=sleeps)

        result = asyncio.run(execm._live_place_order_async(
            {"action": "buy", "reason": "entry", "qty": 100.0, "price": 1.501}
        ))

        assert calls["n"] == 2, "harus retry tepat 1x setelah rate limit"
        assert result["status"] == "filled"
        assert sleeps and sleeps[0] > 0, "rate-limit branch wajib backoff sebelum retry"
        # State nyata: posisi long ter-catat dgn qty ter-fill.
        pos = pm.get_position()
        assert pos["side"] == "long"
        assert pos["qty"] == pytest.approx(100.0)
        assert pos["entry_price"] == pytest.approx(1.501)

    def test_silent_reject_filled_zero_returns_failure(self, integ, monkeypatch):
        """
        filled=0 (silent reject matching engine) lewat jalur PENUH → return failure,
        BUKAN pura-pura full-fill. State HARUS tetap flat (tak ada posisi hantu).
        """
        execm, pm, _ = integ

        calls = {"n": 0}

        async def _fake_order(*a, **kw):
            calls["n"] += 1
            return {"id": "x", "status": "closed", "filled": 0.0}

        monkeypatch.setattr(execm.exchange, "create_order", _fake_order, raising=False)
        _instant_sleep(monkeypatch)

        result = asyncio.run(execm._live_place_order_async(
            {"action": "buy", "reason": "entry", "qty": 100.0, "price": 1.501}
        ))

        assert result["status"] == "failed", "zero-fill harus failure, bukan filled"
        assert calls["n"] == 1, "silent reject return langsung, tidak retry sia-sia"
        # State nyata TIDAK berubah — tetap flat, tidak nyangkut posisi palsu.
        pos = pm.get_position()
        assert pos["side"] == "none"
        assert pos["qty"] == 0.0

    def test_partial_fill_on_open_sets_position_with_filled_qty(self, integ, monkeypatch):
        """
        filled < qty*0.999 saat OPEN → posisi dicatat pakai qty TER-FILL (40),
        bukan qty diminta (100), biar sync nanti rekonsiliasi sisa.
        """
        execm, pm, _ = integ

        async def _fake_order(*a, **kw):
            return {"id": "x", "status": "closed", "filled": 40.0}

        monkeypatch.setattr(execm.exchange, "create_order", _fake_order, raising=False)
        _instant_sleep(monkeypatch)

        result = asyncio.run(execm._live_place_order_async(
            {"action": "buy", "reason": "entry", "qty": 100.0, "price": 1.501}
        ))

        assert result["status"] == "filled"
        assert result["qty"] == pytest.approx(40.0)
        # State nyata: qty = ter-fill (40), BUKAN diminta (100).
        pos = pm.get_position()
        assert pos["side"] == "long"
        assert pos["qty"] == pytest.approx(40.0), "posisi pakai qty ter-fill, bukan diminta"
        assert pos["entry_price"] == pytest.approx(1.501)

    def test_max_retries_exhausted_returns_failed(self, integ, monkeypatch):
        """Network error terus-menerus → habis MAX_RETRY → 'failed' (tidak ngegantung)."""
        execm, pm, _ = integ
        import ccxt.pro as ccxt

        calls = {"n": 0}

        async def _fake_order(*a, **kw):
            calls["n"] += 1
            raise ccxt.NetworkError("connection reset")

        monkeypatch.setattr(execm.exchange, "create_order", _fake_order, raising=False)
        _instant_sleep(monkeypatch)

        result = asyncio.run(execm._live_place_order_async(
            {"action": "buy", "reason": "entry", "qty": 100.0, "price": 1.501}
        ))

        assert calls["n"] == execm.MAX_RETRY, "harus coba persis MAX_RETRY kali"
        assert result["status"] == "failed"
        # State nyata tetap flat — tak ada posisi hantu setelah gagal total.
        assert pm.get_position()["side"] == "none"


# =============================================================================
# A (lanjutan). STATUS PASAR/SIMBOL — order ditolak karena simbol dalam status
# khusus: contract not live / settlement / pre-delivery / reduce-only period.
# Ini BUKAN error transient jaringan: kondisi diatur exchange, jadi retry cepat
# 3x cuma buang waktu + bikin log menyesatkan ("Max retries exceeded" padahal
# masalahnya status pasar). Harapan: FAIL-FAST — 1 panggilan create_order,
# status "rejected", dan state tetap flat (tak ada posisi hantu).
#
# Test ini SENGAJA ditulis lebih dulu (TDD). Di kode LAMA dia MERAH karena kode-
# kode ini jatuh ke cabang "BadRequest lain → retry" → dicoba MAX_RETRY kali lalu
# "failed". Setelah logika grup A ditambahkan, dia HIJAU.
# =============================================================================
class TestMarketStatusRejections:
    # PENTING: kelas exception dicocokkan dengan pemetaan CCXT 4.5.x ASLI
    # (diverifikasi dari ccxt.bybit().exceptions), BUKAN diasumsikan BadRequest:
    #   110074/110063 → ExchangeError   |   110023/110042 → InvalidOrder
    # Kalau di-raise pakai kelas yang salah (mis. BadRequest), test bisa LOLOS
    # PALSU padahal di live kode lolos cabang & tetap diretry. Ini justru jebakan
    # "mock != realita" yang kita hindari.
    @pytest.mark.parametrize(
        "ret_code,exc_name,ret_msg",
        [
            ("110074", "ExchangeError", "Contract is not live"),
            ("110063", "ExchangeError", "Settlement in progress; symbol unavailable for trading"),
            ("110023", "InvalidOrder", "Current symbol allows position reduction only"),
            ("110042", "InvalidOrder", "Pre-delivery status; position reduction only"),
        ],
    )
    def test_market_status_rejected_no_retry(self, integ, monkeypatch, ret_code, exc_name, ret_msg):
        """Status pasar khusus → fail-fast (1 panggilan, 'rejected'), state flat."""
        execm, pm, _ = integ
        import ccxt.pro as ccxt
        exc_cls = getattr(ccxt, exc_name)

        calls = {"n": 0}

        async def _fake_order(*a, **kw):
            calls["n"] += 1
            raise exc_cls(f'bybit {{"retCode":{ret_code},"retMsg":"{ret_msg}"}}')

        monkeypatch.setattr(execm.exchange, "create_order", _fake_order, raising=False)
        _instant_sleep(monkeypatch)

        result = asyncio.run(execm._live_place_order_async(
            {"action": "buy", "reason": "entry", "qty": 100.0, "price": 1.501}
        ))

        # Kontrak ke Bybit: kode status pasar TIDAK boleh diretry.
        assert calls["n"] == 1, (
            f"{ret_code} bukan transient → cukup 1 panggilan, dapat {calls['n']} "
            f"(kode lama retry MAX_RETRY)"
        )
        assert result["status"] == "rejected", (
            f"{ret_code} = status pasar → harus 'rejected', bukan retry→'failed'"
        )
        # State nyata tetap flat — tidak nyangkut posisi palsu setelah ditolak.
        pos = pm.get_position()
        assert pos["side"] == "none"
        assert pos["qty"] == 0.0


# =============================================================================
# REGRESI — duplicate-fire tidak boleh menumpuk posisi (bug 2026-05-29)
# =============================================================================
# Skenario: tick 100ms kedua men-dispatch order kembar SEBELUM order pertama
# konfirmasi & state ke-update. In-flight guard harus memblok yang kedua →
# posisi tetap TUNGGAL (4.7), bukan dobel (9.4). Pakai position_manager ASLI.
class TestDuplicateFireRegression:
    def test_duplicate_fire_does_not_stack(self, integ, monkeypatch):
        execm, pm, _ = integ

        gate = asyncio.Event()
        amounts: list[object] = []

        async def _slow_order(*a, **kw):
            # Order pertama "nyangkut" di gate → tetap in-flight saat tick ke-2 datang.
            amounts.append(kw.get("amount"))
            await gate.wait()
            return {"id": str(len(amounts)), "status": "closed", "filled": kw.get("amount")}

        monkeypatch.setattr(execm.exchange, "create_order", _slow_order, raising=False)

        async def _run():
            # Tick 1: dispatch (set _live_order_in_flight=True), task nyangkut di gate.
            execm.place_order({"action": "buy", "reason": "tick1", "qty": 4.7, "price": 1.501})
            # Tick 2 (100ms kemudian, state belum sync): in-flight guard HARUS skip.
            execm.place_order({"action": "buy", "reason": "tick2", "qty": 4.7, "price": 1.501})
            pending = list(execm._background_tasks)  # snapshot sebelum gate dibuka
            gate.set()
            if pending:
                await asyncio.gather(*pending)

        asyncio.run(_run())

        assert len(amounts) == 1, f"hanya 1 order boleh ter-dispatch, dapat {len(amounts)}"
        pos = pm.get_position()
        assert pos["side"] == "long"
        assert pos["qty"] == pytest.approx(4.7), "posisi TUNGGAL 4.7, BUKAN 9.4 (stacked)"
        # Guard wajib reset setelah selesai supaya order berikutnya bisa jalan.
        assert execm._live_order_in_flight is False


# =============================================================================
# E. MATRIKS PARAMETRIZED — kombinasi dari sedikit logika
# =============================================================================
# Sumbu: mode {paper, demo} × side {long, short} × qtyStep {0.001..1.0}.
# Sumbu "market" KONSTAN — bot hanya kirim market order (type="market"); ini
# di-assert eksplisit di jalur demo. Produksi menangani semua di 1 jalur
# (place_order → _paper_execute / _live_place_order_async); test menyebut tiap
# kombinasi: side benar + qty floor ke step + posisi ter-catat di state.
#
# Entry point = place_order ASLI (dispatcher real). Dipanggil dari konteks sync
# (tanpa event loop) → place_order jalan inline via asyncio.run. position_manager
# ASLI dipakai supaya assertion seragam lintas mode (paper: open_position;
# demo: set_position_optimistic → keduanya bermuara ke _state).
class TestOrderMatrix:
    @pytest.mark.parametrize("mode", ["paper", "demo"])
    @pytest.mark.parametrize("action,expected_side", [("buy", "long"), ("sell", "short")])
    @pytest.mark.parametrize("qty_step", [0.001, 0.01, 0.1, 1.0])
    def test_open_side_and_qty_step_matrix(
        self, integ, monkeypatch, mode, action, expected_side, qty_step
    ):
        execm, pm, ds = integ
        monkeypatch.setattr(execm, "MODE", mode)
        monkeypatch.setattr(execm, "_qty_step_cache", qty_step)
        monkeypatch.setattr(execm, "ORDER_SIZE_USDT", 2)
        monkeypatch.setattr(execm, "LEVERAGE", 3)

        # Buy isi @ ASK, sell @ BID. qty = floor((ORDER_SIZE×LEVERAGE)/price, step).
        price = ds.best_ask["price"] if action == "buy" else ds.best_bid["price"]
        expected_qty = execm._round_to_step((2 * 3) / price, qty_step)
        assert expected_qty > 0, "prasyarat test: qty tidak boleh membulat ke nol"

        sent: dict[str, object] = {}

        async def _fake_order(*a, **kw):
            sent["amount"] = kw["amount"]
            sent["type"] = kw.get("type")
            return {"id": "ok", "status": "closed", "filled": kw["amount"]}

        monkeypatch.setattr(execm.exchange, "create_order", _fake_order, raising=False)
        _instant_sleep(monkeypatch)

        # qty TIDAK diberikan → tiap jalur hitung & floor ke step sendiri.
        execm.place_order({"action": action, "reason": "matrix"})

        pos = pm.get_position()
        assert pos["side"] == expected_side
        assert pos["qty"] == pytest.approx(expected_qty)

        if mode == "demo":
            # Verifikasi apa yang DIKIRIM ke exchange (live path).
            assert sent["amount"] == pytest.approx(expected_qty), "qty dikirim = floor ke step"
            assert sent["type"] == "market", "bot hanya kirim market order"
        else:
            # Paper tidak menyentuh exchange sama sekali.
            assert sent == {}, "paper mode tidak boleh panggil create_order"
