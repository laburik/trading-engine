# =============================================================================
# tests/test_position_manager.py — Unit test untuk PnL math
# =============================================================================
# Cover: open_position, close_position, update_pnl, get_position, get_pnl_summary
# =============================================================================
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def pm(monkeypatch):
    """
    Fresh import position_manager dengan MODE=paper. State direset tiap test
    supaya tidak ada bocor antar test.
    """
    monkeypatch.setenv("MODE", "paper")
    import position_manager as _pm

    # Reset state ke kondisi awal (paper mode default balance)
    _pm._state["side"] = "none"
    _pm._state["entry_price"] = 0.0
    _pm._state["qty"] = 0.0
    _pm._state["balance"] = 1000.0
    _pm._state["unrealized_pnl"] = 0.0
    _pm._state["equity"] = 1000.0
    _pm._state["realized_pnl_total"] = 0.0
    _pm._state["total_fees"] = 0.0
    _pm._state["open_time"] = None

    # Patch MODE module-level to "paper" supaya update_pnl jalan
    monkeypatch.setattr(_pm, "MODE", "paper")
    return _pm


# =============================================================================
# OPEN POSITION
# =============================================================================
class TestOpenPosition:
    def test_open_long_sets_state(self, pm):
        pm.open_position("long", entry_price=1.5, qty=100, entry_fee=0.0)
        assert pm._state["side"] == "long"
        assert pm._state["entry_price"] == 1.5
        assert pm._state["qty"] == 100

    def test_open_short_sets_state(self, pm):
        pm.open_position("short", entry_price=1.5, qty=50, entry_fee=0.0)
        assert pm._state["side"] == "short"
        assert pm._state["qty"] == 50

    def test_entry_fee_deducted_from_balance(self, pm):
        initial = pm._state["balance"]
        pm.open_position("long", entry_price=1.5, qty=100, entry_fee=2.5)
        assert pm._state["balance"] == initial - 2.5
        assert pm._state["total_fees"] == 2.5

    def test_open_when_already_in_position_ignored(self, pm):
        pm.open_position("long", 1.5, 100)
        pm.open_position("short", 2.0, 50)  # harus diabaikan
        assert pm._state["side"] == "long"
        assert pm._state["entry_price"] == 1.5


# =============================================================================
# CLOSE POSITION (PnL math — yang paling kritis)
# =============================================================================
class TestClosePosition:
    def test_long_profit(self, pm):
        # Beli @1.5, jual @1.6, qty 100 → gross = 10.0
        pm.open_position("long", 1.5, 100)
        net_pnl, _ = pm.close_position(exit_price=1.6, exit_fee=0.0)
        assert net_pnl == pytest.approx(10.0)
        assert pm._state["balance"] == pytest.approx(1010.0)

    def test_long_loss(self, pm):
        # Beli @1.5, jual @1.4, qty 100 → gross = -10.0
        pm.open_position("long", 1.5, 100)
        net_pnl, _ = pm.close_position(exit_price=1.4)
        assert net_pnl == pytest.approx(-10.0)
        assert pm._state["balance"] == pytest.approx(990.0)

    def test_short_profit(self, pm):
        # Sell @1.5, buy @1.4, qty 100 → gross = 10.0 (short untung saat harga turun)
        pm.open_position("short", 1.5, 100)
        net_pnl, _ = pm.close_position(exit_price=1.4)
        assert net_pnl == pytest.approx(10.0)

    def test_short_loss(self, pm):
        # Sell @1.5, buy @1.6 → gross = -10.0
        pm.open_position("short", 1.5, 100)
        net_pnl, _ = pm.close_position(exit_price=1.6)
        assert net_pnl == pytest.approx(-10.0)

    def test_exit_fee_reduces_net_pnl(self, pm):
        pm.open_position("long", 1.5, 100)
        net_pnl, fee = pm.close_position(exit_price=1.6, exit_fee=2.0)
        # gross = 10.0, fee = 2.0 → net = 8.0
        assert net_pnl == pytest.approx(8.0)
        assert fee == 2.0

    def test_close_when_no_position_safe(self, pm):
        net_pnl, fee = pm.close_position(exit_price=1.5)
        assert net_pnl == 0.0
        assert fee == 0.0
        assert pm._state["balance"] == 1000.0  # unchanged

    def test_state_reset_after_close(self, pm):
        pm.open_position("long", 1.5, 100)
        pm.close_position(exit_price=1.6)
        assert pm._state["side"] == "none"
        assert pm._state["qty"] == 0.0
        assert pm._state["entry_price"] == 0.0
        assert pm._state["unrealized_pnl"] == 0.0


# =============================================================================
# UPDATE PNL (tick-by-tick)
# =============================================================================
class TestUpdatePnl:
    def test_no_position_zero_unrealized(self, pm):
        pm.update_pnl(bid_price=1.5, ask_price=1.51)
        assert pm._state["unrealized_pnl"] == 0.0
        assert pm._state["equity"] == pm._state["balance"]

    def test_long_uses_bid_price(self, pm):
        # LONG → unrealized = (bid - entry) * qty (gunakan BID karena exit long = jual @ bid)
        pm.open_position("long", entry_price=1.5, qty=100)
        pm.update_pnl(bid_price=1.6, ask_price=1.61)
        assert pm._state["unrealized_pnl"] == pytest.approx(10.0)  # (1.6 - 1.5) * 100

    def test_short_uses_ask_price(self, pm):
        # SHORT → unrealized = (entry - ask) * qty (gunakan ASK karena exit short = beli @ ask)
        pm.open_position("short", entry_price=1.5, qty=100)
        pm.update_pnl(bid_price=1.39, ask_price=1.4)
        assert pm._state["unrealized_pnl"] == pytest.approx(10.0)  # (1.5 - 1.4) * 100

    def test_equity_includes_unrealized(self, pm):
        pm.open_position("long", entry_price=1.5, qty=100)
        pm.update_pnl(bid_price=1.6, ask_price=1.61)
        # equity = balance + unrealized
        assert pm._state["equity"] == pytest.approx(pm._state["balance"] + 10.0)


# =============================================================================
# ACCESSORS (dipakai dashboard & strategy_runtime)
# =============================================================================
class TestAccessors:
    def test_get_position_returns_state(self, pm):
        pm.open_position("long", 1.5, 100)
        pos = pm.get_position()
        assert pos["side"] == "long"
        assert pos["entry_price"] == 1.5
        assert pos["qty"] == 100

    def test_get_pnl_summary_has_required_keys(self, pm):
        summary = pm.get_pnl_summary()
        # Required keys yang dipakai dashboard.py & strategy_runtime.py
        for k in ("balance", "equity", "unrealized_pnl", "side"):
            assert k in summary, f"Missing key: {k}"


# =============================================================================
# PARSE POSITION RESPONSE — pure parser, no IO
# =============================================================================
# Bug yang di-cover: API return partial dict (entryPrice=None, contracts=None,
# dst) MENG-OVERWRITE local state ke 0 padahal posisi real masih terbuka.
# Parser sekarang: 3 outcomes (clear / skip / update). Caller harus respect
# 'skip' supaya state lokal tidak corrupt saat data quality buruk.
class TestParsePositionResponse:
    def test_empty_list_returns_clear(self, pm):
        status, payload = pm._parse_position_response([])
        assert status == "clear"
        assert payload is None

    def test_only_inactive_positions_returns_clear(self, pm):
        """Bybit kadang return list of position slots dengan contracts=0 = 'no position'."""
        status, payload = pm._parse_position_response([
            {"side": "long", "contracts": 0, "entryPrice": 100.0},
            {"side": "short", "contracts": 0.0, "entryPrice": 200.0},
        ])
        assert status == "clear"

    def test_valid_long_position_returns_update(self, pm):
        status, payload = pm._parse_position_response([{
            "side": "long",
            "contracts": 1.5,
            "entryPrice": 100.0,
            "unrealizedPnl": 5.0,
        }])
        assert status == "update"
        side, entry, qty, unreal = payload
        assert side == "long"
        assert entry == 100.0
        assert qty == 1.5
        assert unreal == 5.0

    def test_valid_short_position_returns_update(self, pm):
        status, payload = pm._parse_position_response([{
            "side": "SHORT",  # uppercase — should be normalized
            "contracts": 2.0,
            "entryPrice": 50.0,
            "unrealizedPnl": -1.5,
        }])
        assert status == "update"
        side, entry, qty, unreal = payload
        assert side == "short"
        assert entry == 50.0


# =============================================================================
# PARSE POSITION — SKIP scenarios (the actual bug fix)
# =============================================================================
class TestParsePositionResponseSkip:
    """Skenario di mana state lokal HARUS dipertahankan, bukan di-overwrite."""

    def test_missing_entry_price_returns_skip(self, pm):
        """KASUS BUG: contracts>0 tapi entryPrice missing → JANGAN reset state ke 0."""
        status, payload = pm._parse_position_response([{
            "side": "long",
            "contracts": 1.5,
            # entryPrice missing!
        }])
        assert status == "skip"
        assert "entryPrice" in payload or "entry" in payload.lower()

    def test_none_entry_price_returns_skip(self, pm):
        status, payload = pm._parse_position_response([{
            "side": "long",
            "contracts": 1.5,
            "entryPrice": None,
        }])
        assert status == "skip"

    def test_zero_entry_price_returns_skip(self, pm):
        """entry_price=0 tidak masuk akal untuk active position."""
        status, payload = pm._parse_position_response([{
            "side": "long",
            "contracts": 1.5,
            "entryPrice": 0,
        }])
        assert status == "skip"
        assert "positive" in payload.lower() or "entryprice" in payload.lower()

    def test_negative_entry_price_returns_skip(self, pm):
        status, payload = pm._parse_position_response([{
            "side": "long",
            "contracts": 1.5,
            "entryPrice": -100.0,
        }])
        assert status == "skip"

    def test_non_numeric_entry_price_returns_skip(self, pm):
        status, payload = pm._parse_position_response([{
            "side": "long",
            "contracts": 1.5,
            "entryPrice": "pending",
        }])
        assert status == "skip"
        assert "numeric" in payload.lower() or "entryprice" in payload.lower()

    def test_invalid_side_returns_skip(self, pm):
        status, payload = pm._parse_position_response([{
            "side": "unknown",
            "contracts": 1.5,
            "entryPrice": 100.0,
        }])
        assert status == "skip"
        assert "side" in payload.lower()

    # Regresi pin: ditemukan hypothesis (entryPrice='INF'). float('inf'/'nan')
    # lolos cek `<= 0` → harus ditolak via math.isfinite. Tes deterministik di
    # sini supaya tak bergantung fuzzer kebetulan melempar nilai ini lagi.
    @pytest.mark.parametrize("bad", ["INF", "inf", "Infinity", "-inf", float("inf"), float("-inf")])
    def test_infinite_entry_price_returns_skip(self, pm, bad):
        status, payload = pm._parse_position_response([{
            "side": "long", "contracts": 1.5, "entryPrice": bad,
        }])
        assert status == "skip"
        assert "finite" in payload.lower() or "entryprice" in payload.lower()

    @pytest.mark.parametrize("bad", ["nan", "NaN", float("nan")])
    def test_nan_entry_price_returns_skip(self, pm, bad):
        status, payload = pm._parse_position_response([{
            "side": "long", "contracts": 1.5, "entryPrice": bad,
        }])
        assert status == "skip"

    @pytest.mark.parametrize("bad", ["inf", "Infinity", float("inf")])
    def test_infinite_contracts_returns_skip(self, pm, bad):
        # contracts non-finite lolos filter `> 0` (inf>0) → qty=inf harus ditolak juga.
        status, payload = pm._parse_position_response([{
            "side": "long", "contracts": bad, "entryPrice": 100.0,
        }])
        assert status == "skip"
        assert "finite" in payload.lower() or "contracts" in payload.lower()


# =============================================================================
# PARSE POSITION — defensive against exchange quirks
# =============================================================================
class TestParsePositionResponseDefensive:
    def test_non_dict_entries_skipped(self, pm):
        """Jika exchange return list dengan non-dict (None, string), jangan crash."""
        status, payload = pm._parse_position_response([None, "weird", {"side": "long", "contracts": 1.5, "entryPrice": 100.0}])
        assert status == "update"

    def test_contracts_as_string_coerced(self, pm):
        """CCXT exchange tertentu return numeric sebagai string — harus dicoerce."""
        status, payload = pm._parse_position_response([{
            "side": "long",
            "contracts": "1.5",
            "entryPrice": "100.0",
            "unrealizedPnl": "5.0",
        }])
        assert status == "update"
        _, entry, qty, _ = payload
        assert entry == 100.0
        assert qty == 1.5

    def test_unrealized_missing_defaults_to_zero(self, pm):
        """unrealizedPnl optional — kalau missing, default 0, JANGAN skip update."""
        status, payload = pm._parse_position_response([{
            "side": "long",
            "contracts": 1.5,
            "entryPrice": 100.0,
            # unrealizedPnl missing
        }])
        assert status == "update"
        _, _, _, unreal = payload
        assert unreal == 0.0

    def test_first_active_among_inactive_picked(self, pm):
        """Skip yang contracts=0, ambil yang aktif."""
        status, payload = pm._parse_position_response([
            {"side": "long", "contracts": 0, "entryPrice": 50.0},
            {"side": "short", "contracts": 2.0, "entryPrice": 100.0, "unrealizedPnl": 0.0},
        ])
        assert status == "update"
        side, _, _, _ = payload
        assert side == "short"


# =============================================================================
# PARSE BALANCE RESPONSE — distinguish missing vs zero
# =============================================================================
# Bug lama: `usdt.get("total") or _state["balance"]` membuat balance=0 (user kehabisan
# margin) jatuh ke balance lama → loss tersembunyi.
class TestParseBalanceResponse:
    def test_valid_balance_returns_update(self, pm):
        status, payload = pm._parse_balance_response({"USDT": {"total": 1234.56}})
        assert status == "update"
        assert payload == 1234.56

    def test_zero_balance_returns_update_zero(self, pm):
        """KASUS BUG: total=0.0 (legit habis margin) HARUS dipakai apa adanya."""
        status, payload = pm._parse_balance_response({"USDT": {"total": 0.0}})
        assert status == "update"
        assert payload == 0.0  # bukan jatuh ke fallback

    def test_zero_int_balance_returns_update_zero(self, pm):
        status, payload = pm._parse_balance_response({"USDT": {"total": 0}})
        assert status == "update"
        assert payload == 0.0

    def test_string_balance_coerced(self, pm):
        status, payload = pm._parse_balance_response({"USDT": {"total": "500.75"}})
        assert status == "update"
        assert payload == 500.75

    def test_missing_usdt_returns_skip(self, pm):
        """Tidak ada entry USDT — preserve state lama."""
        status, payload = pm._parse_balance_response({"BTC": {"total": 0.5}})
        assert status == "skip"
        assert "usdt" in payload.lower()

    def test_missing_total_returns_skip(self, pm):
        status, payload = pm._parse_balance_response({"USDT": {"free": 100.0}})
        assert status == "skip"
        assert "total" in payload.lower()

    def test_none_total_returns_skip(self, pm):
        status, payload = pm._parse_balance_response({"USDT": {"total": None}})
        assert status == "skip"

    def test_non_numeric_total_returns_skip(self, pm):
        status, payload = pm._parse_balance_response({"USDT": {"total": "pending"}})
        assert status == "skip"
        assert "numeric" in payload.lower()

    def test_negative_total_returns_skip(self, pm):
        """Balance negatif suspect (margin call?) — minta investigasi manual."""
        status, payload = pm._parse_balance_response({"USDT": {"total": -100.0}})
        assert status == "skip"
        assert "negative" in payload.lower()

    def test_non_dict_response_returns_skip(self, pm):
        status, payload = pm._parse_balance_response("error string")  # type: ignore[arg-type]
        assert status == "skip"


# =============================================================================
# BUG REGRESSION — confirms old behaviors are gone
# =============================================================================
class TestSyncBugRegression:
    def test_position_with_zero_entry_price_does_not_corrupt_state(self, pm):
        """
        Bug lama: `float(pos.get("entryPrice") or 0)` membuat entry_price=0
        saat field missing. PnL calc rusak (`(bid - 0) * qty` = infinite gain).

        Fix: parser return ("skip", reason) → caller preserve previous state.
        """
        status, payload = pm._parse_position_response([{
            "side": "long",
            "contracts": 1.5,
            "entryPrice": None,  # missing
        }])
        assert status == "skip", "BUG: would have corrupted state to entry_price=0"

    def test_zero_balance_not_hidden_by_fallback(self, pm):
        """
        Bug lama: `float(usdt.get("total") or _state["balance"])` —
        kalau total=0 (user habis margin), jatuh ke balance lama. Loss tersembunyi.

        Fix: 0.0 sekarang dipakai apa adanya.
        """
        status, payload = pm._parse_balance_response({"USDT": {"total": 0.0}})
        assert status == "update"
        assert payload == 0.0, "BUG: zero balance would have been hidden by fallback"


# =============================================================================
# _sync_position — async IO wrapper di sekitar parser
# =============================================================================
# Parser sudah di-test exhaustively di atas. Di sini kita fokus ke side-effect
# IO wrapper-nya: error policy, state mutation, dan counter konsekutif.
class TestSyncPositionAsync:
    def test_api_error_does_not_touch_state(self, pm, monkeypatch):
        """Exception dari exchange → state lama dipertahankan, counter naik."""
        # Set state lama yang harus dipreservasi
        pm._state["side"] = "long"
        pm._state["entry_price"] = 100.0
        pm._state["qty"] = 2.0
        # Reset counter
        monkeypatch.setattr(pm, "_consecutive_position_sync_failures", 0)

        async def _boom(*a, **kw):
            raise RuntimeError("network down")

        monkeypatch.setattr(pm.exchange, "fetch_positions", _boom, raising=False)

        asyncio.run(pm._sync_position())

        # State HARUS tidak berubah
        assert pm._state["side"] == "long"
        assert pm._state["entry_price"] == 100.0
        assert pm._state["qty"] == 2.0
        # Counter harus naik 1
        assert pm._consecutive_position_sync_failures == 1

    def test_consecutive_failures_escalate_to_error_log(self, pm, monkeypatch, caplog):
        """≥ SYNC_FAIL_WARN_THRESHOLD → log level error (bukan warning)."""
        monkeypatch.setattr(pm, "_consecutive_position_sync_failures", pm.SYNC_FAIL_WARN_THRESHOLD - 1)

        async def _boom(*a, **kw):
            raise RuntimeError("network down")

        monkeypatch.setattr(pm.exchange, "fetch_positions", _boom, raising=False)
        import logging
        with caplog.at_level(logging.ERROR, logger="position_manager"):
            asyncio.run(pm._sync_position())
        # Setelah panggilan: counter == threshold → eskalasi error
        assert pm._consecutive_position_sync_failures == pm.SYNC_FAIL_WARN_THRESHOLD
        # Error level harus muncul (eskalasi)
        assert any(rec.levelname == "ERROR" for rec in caplog.records)

    def test_non_list_response_logs_and_preserves_state(self, pm, monkeypatch):
        """fetch_positions return tipe selain list → state unchanged."""
        pm._state["side"] = "short"
        pm._state["entry_price"] = 50.0

        async def _return_dict(*a, **kw):
            return {"unexpected": "shape"}

        monkeypatch.setattr(pm.exchange, "fetch_positions", _return_dict, raising=False)
        asyncio.run(pm._sync_position())
        # State tidak berubah
        assert pm._state["side"] == "short"
        assert pm._state["entry_price"] == 50.0

    def test_clear_status_resets_state_to_none(self, pm, monkeypatch):
        """Tidak ada posisi aktif di exchange → state lokal di-reset."""
        pm._state["side"] = "long"
        pm._state["entry_price"] = 100.0
        pm._state["qty"] = 2.0
        pm._state["unrealized_pnl"] = 5.0

        async def _empty(*a, **kw):
            return []  # tidak ada posisi

        monkeypatch.setattr(pm.exchange, "fetch_positions", _empty, raising=False)
        asyncio.run(pm._sync_position())

        assert pm._state["side"] == "none"
        assert pm._state["entry_price"] == 0.0
        assert pm._state["qty"] == 0.0
        assert pm._state["unrealized_pnl"] == 0.0

    def test_skip_status_does_not_touch_state(self, pm, monkeypatch):
        """Parser return 'skip' (data invalid) → state local TIDAK berubah."""
        pm._state["side"] = "long"
        pm._state["entry_price"] = 99.0
        pm._state["qty"] = 1.5

        async def _bad_data(*a, **kw):
            # entryPrice None → parser return skip
            return [{"side": "long", "contracts": 1.0, "entryPrice": None}]

        monkeypatch.setattr(pm.exchange, "fetch_positions", _bad_data, raising=False)
        asyncio.run(pm._sync_position())

        assert pm._state["side"] == "long"
        assert pm._state["entry_price"] == 99.0
        assert pm._state["qty"] == 1.5

    def test_update_status_overwrites_state_atomically(self, pm, monkeypatch):
        """Posisi valid → state lokal di-overwrite dengan data exchange."""
        pm._state["side"] = "none"

        async def _good(*a, **kw):
            return [{
                "side": "long", "contracts": 3.0,
                "entryPrice": 250.0, "unrealizedPnl": 12.5,
            }]

        monkeypatch.setattr(pm.exchange, "fetch_positions", _good, raising=False)
        monkeypatch.setattr(pm, "_consecutive_position_sync_failures", 5)
        asyncio.run(pm._sync_position())

        assert pm._state["side"] == "long"
        assert pm._state["entry_price"] == 250.0
        assert pm._state["qty"] == 3.0
        assert pm._state["unrealized_pnl"] == 12.5
        # Counter direset setelah sukses
        assert pm._consecutive_position_sync_failures == 0


# =============================================================================
# _sync_balance — async IO wrapper
# =============================================================================
class TestSyncBalanceAsync:
    def test_api_error_preserves_balance(self, pm, monkeypatch):
        pm._state["balance"] = 500.0
        monkeypatch.setattr(pm, "_consecutive_balance_sync_failures", 0)

        async def _boom(*a, **kw):
            raise RuntimeError("network down")

        monkeypatch.setattr(pm.exchange, "fetch_balance", _boom, raising=False)
        asyncio.run(pm._sync_balance())

        assert pm._state["balance"] == 500.0
        assert pm._consecutive_balance_sync_failures == 1

    def test_skip_status_preserves_balance(self, pm, monkeypatch):
        pm._state["balance"] = 500.0

        async def _bad(*a, **kw):
            return {"BTC": {"total": 0.5}}  # tidak ada USDT → skip

        monkeypatch.setattr(pm.exchange, "fetch_balance", _bad, raising=False)
        asyncio.run(pm._sync_balance())
        assert pm._state["balance"] == 500.0

    def test_valid_response_updates_balance(self, pm, monkeypatch):
        pm._state["balance"] = 500.0

        async def _good(*a, **kw):
            return {"USDT": {"total": 1234.56}}

        monkeypatch.setattr(pm.exchange, "fetch_balance", _good, raising=False)
        monkeypatch.setattr(pm, "_consecutive_balance_sync_failures", 3)
        asyncio.run(pm._sync_balance())

        assert pm._state["balance"] == 1234.56
        assert pm._consecutive_balance_sync_failures == 0

    def test_zero_balance_applied_as_is(self, pm, monkeypatch):
        """Bug fix: balance=0 (habis margin) dipakai apa adanya, bukan fallback."""
        pm._state["balance"] = 500.0

        async def _zero(*a, **kw):
            return {"USDT": {"total": 0.0}}

        monkeypatch.setattr(pm.exchange, "fetch_balance", _zero, raising=False)
        asyncio.run(pm._sync_balance())
        assert pm._state["balance"] == 0.0


# =============================================================================
# initial_sync — dipanggil sekali sebelum strategy loop mulai
# =============================================================================
class TestInitialSync:
    def test_paper_mode_no_op(self, pm, monkeypatch):
        """Mode paper → initial_sync langsung return tanpa panggil exchange."""
        monkeypatch.setattr(pm, "MODE", "paper")
        called = []

        async def _spy(*a, **kw):
            called.append("called")

        monkeypatch.setattr(pm.exchange, "fetch_positions", _spy, raising=False)
        monkeypatch.setattr(pm.exchange, "fetch_balance", _spy, raising=False)
        asyncio.run(pm.initial_sync())
        assert called == []

    def test_demo_mode_syncs_position_and_balance(self, pm, monkeypatch):
        monkeypatch.setattr(pm, "MODE", "demo")

        async def _pos(*a, **kw):
            return [{"side": "long", "contracts": 1.0, "entryPrice": 100.0, "unrealizedPnl": 0.0}]

        async def _bal(*a, **kw):
            return {"USDT": {"total": 2000.0}}

        monkeypatch.setattr(pm.exchange, "fetch_positions", _pos, raising=False)
        monkeypatch.setattr(pm.exchange, "fetch_balance", _bal, raising=False)

        asyncio.run(pm.initial_sync())

        assert pm._state["side"] == "long"
        assert pm._state["entry_price"] == 100.0
        assert pm._state["balance"] == 2000.0
        assert pm._state["equity"] == 2000.0  # balance + unrealized(0)

    def test_initial_sync_failure_is_non_fatal(self, pm, monkeypatch):
        """initial_sync wrap exception — failure tidak crash bot."""
        monkeypatch.setattr(pm, "MODE", "demo")

        async def _boom(*a, **kw):
            raise RuntimeError("fail")

        # _sync_position swallow exception → initial_sync sendiri tidak raise
        monkeypatch.setattr(pm.exchange, "fetch_positions", _boom, raising=False)
        monkeypatch.setattr(pm.exchange, "fetch_balance", _boom, raising=False)
        # Tidak boleh raise
        asyncio.run(pm.initial_sync())
