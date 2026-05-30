# =============================================================================
# tests/test_execution.py — Order fill parsing (silent reject detection)
# =============================================================================
# Bug yang di-cover: filled_or_qty fallback membuat zero fill (silent reject)
# terlihat seperti full fill. Helper _parse_order_fill() sekarang explicit
# distinguishi None vs 0 vs positive.
#
# Setup: execution.py import side-effect-heavy (ccxt_client, data_stream,
# position_manager, trade_logger). Kita mock dependencies hanya untuk durasi
# import, lalu restore sys.modules — supaya test file lain tidak kebagian
# MagicMock yang salah.
# =============================================================================
from __future__ import annotations

import sys
import time
import types
from unittest.mock import MagicMock

import pytest
from sortedcontainers import SortedDict


# -----------------------------------------------------------------------------
# Isolated import: install mocks, import execution, capture _parse_order_fill,
# then RESTORE sys.modules — defensive teardown supaya test file lain bersih.
# -----------------------------------------------------------------------------
_SAVED_MODULES: dict[str, object] = {}
_TO_MOCK = ["ccxt_client", "data_stream", "position_manager", "trade_logger"]

for _name in _TO_MOCK:
    _SAVED_MODULES[_name] = sys.modules.get(_name)  # bisa None kalau belum di-load
    sys.modules[_name] = MagicMock()
_SAVED_MODULES["execution"] = sys.modules.pop("execution", None)

try:
    import execution  # type: ignore[import-not-found]
    _parse_order_fill = execution._parse_order_fill
finally:
    # Restore — original module objects kembali ke sys.modules. _parse_order_fill
    # tetap valid sebagai function reference (closure ke globals execution module).
    for _name, _orig in _SAVED_MODULES.items():
        if _orig is not None:
            sys.modules[_name] = _orig  # type: ignore[assignment]
        else:
            sys.modules.pop(_name, None)


# =============================================================================
# HAPPY PATH — order succeed normally
# =============================================================================
class TestHappyPath:
    def test_full_fill_returns_filled_no_error(self):
        order = {"id": "abc", "status": "closed", "filled": 100.0}
        filled, err = _parse_order_fill(order, 100.0)
        assert err is None
        assert filled == 100.0

    def test_partial_fill_returns_partial_no_error(self):
        """Partial fill bukan error — caller (partial-fill detection block) yang handle."""
        order = {"id": "abc", "status": "closed", "filled": 50.0}
        filled, err = _parse_order_fill(order, 100.0)
        assert err is None
        assert filled == 50.0

    def test_filled_returned_as_string_coerced_to_float(self):
        """CCXT exchange tertentu return 'filled' sebagai string — harus di-coerce."""
        order = {"id": "abc", "status": "closed", "filled": "99.5"}
        filled, err = _parse_order_fill(order, 100.0)
        assert err is None
        assert filled == 99.5


# =============================================================================
# SILENT REJECT — bug yang di-fix
# =============================================================================
class TestSilentReject:
    def test_explicit_zero_fill_returns_failure(self):
        """KASUS BUG: filled=0 (silent reject) JANGAN pretend full fill."""
        order = {"id": "abc", "status": "closed", "filled": 0.0}
        filled, err = _parse_order_fill(order, 100.0)
        assert err is not None
        assert err["status"] == "failed"
        assert "silent reject" in err["reason"].lower() or "zero-fill" in err["reason"].lower()
        assert filled == 0.0  # bukan 100.0 (qty) seperti behavior buggy lama

    def test_zero_fill_with_rejected_status_returns_failure(self):
        order = {"id": "abc", "status": "rejected", "filled": 0.0}
        filled, err = _parse_order_fill(order, 100.0)
        assert err is not None
        assert err["status"] == "failed"
        assert filled == 0.0

    def test_zero_int_fill_returns_failure(self):
        """Edge: filled = 0 (int, bukan float). Bug lama: `0 or qty` = qty (pretend full)."""
        order = {"id": "abc", "status": "closed", "filled": 0}
        filled, err = _parse_order_fill(order, 100.0)
        assert err is not None
        assert filled == 0.0

    def test_negative_filled_treated_as_failure(self):
        """Defensive: filled negatif tidak mungkin tapi harus di-reject."""
        order = {"id": "abc", "status": "closed", "filled": -10.0}
        filled, err = _parse_order_fill(order, 100.0)
        assert err is not None
        assert filled == 0.0


# =============================================================================
# MISSING FILLED FIELD — API quirk handling
# =============================================================================
class TestMissingFilledField:
    def test_missing_filled_with_closed_status_assumes_full_fill(self):
        """Bybit kadang tidak isi 'filled' di response — kalau status='closed', asumsi ok."""
        order = {"id": "abc", "status": "closed"}
        filled, err = _parse_order_fill(order, 100.0)
        assert err is None
        assert filled == 100.0  # fallback ke requested_qty

    def test_missing_filled_with_filled_status_assumes_full_fill(self):
        order = {"id": "abc", "status": "filled"}
        filled, err = _parse_order_fill(order, 100.0)
        assert err is None
        assert filled == 100.0

    def test_missing_filled_with_open_status_returns_failure(self):
        """Status 'open' = pending — tidak boleh asumsi fill."""
        order = {"id": "abc", "status": "open"}
        filled, err = _parse_order_fill(order, 100.0)
        assert err is not None
        assert err["status"] == "failed"
        assert filled == 0.0

    def test_missing_filled_with_unknown_status_returns_failure(self):
        order = {"id": "abc"}  # no status, no filled
        filled, err = _parse_order_fill(order, 100.0)
        assert err is not None
        assert err["status"] == "failed"
        assert filled == 0.0

    def test_explicit_none_filled_with_closed_status_assumes_full(self):
        """`order["filled"] = None` (eksplisit None) sama dengan missing."""
        order = {"id": "abc", "status": "closed", "filled": None}
        filled, err = _parse_order_fill(order, 100.0)
        assert err is None
        assert filled == 100.0

    def test_status_none_treated_as_optimistic_fill(self):
        """
        Regression: bug ke-detect saat smoke test demo (2026-05-29).
        Bybit demo return status='none' untuk market order async ack —
        response balik sebelum fill confirmation datang. Code lama treat
        sebagai zero-fill failure → bot local state desync dari exchange.
        Fix: 'none' masuk optimistic list. PnL sync reconcile state nanti.
        """
        order = {"id": "abc-demo-async", "status": "none"}
        filled, err = _parse_order_fill(order, 50.0)
        assert err is None, "status='none' harus optimistic (PnL sync reconcile), bukan failure"
        assert filled == 50.0

    def test_status_none_with_explicit_none_filled_treated_as_optimistic(self):
        """`filled=None + status='none'` (eksplisit) sama: optimistic."""
        order = {"id": "abc", "status": "none", "filled": None}
        filled, err = _parse_order_fill(order, 100.0)
        assert err is None
        assert filled == 100.0

    def test_unknown_status_still_returns_failure(self):
        """Status selain optimistic list tetap return failure (gak loose)."""
        order = {"id": "abc", "status": "pending_cancel"}
        filled, err = _parse_order_fill(order, 100.0)
        assert err is not None
        assert err["status"] == "failed"
        assert filled == 0.0


# =============================================================================
# INVALID DATA TYPES — defensive against exchange quirks
# =============================================================================
class TestInvalidTypes:
    def test_non_numeric_string_filled_returns_failure(self):
        """Jika exchange aneh return 'filled' = 'pending' atau string non-numeric → fail."""
        order = {"id": "abc", "status": "closed", "filled": "pending"}
        filled, err = _parse_order_fill(order, 100.0)
        assert err is not None
        assert err["status"] == "failed"
        assert filled == 0.0

    def test_dict_filled_returns_failure(self):
        """Edge case: exchange return nested dict — jangan crash."""
        order = {"id": "abc", "status": "closed", "filled": {"unexpected": "shape"}}
        filled, err = _parse_order_fill(order, 100.0)
        assert err is not None
        assert filled == 0.0


# =============================================================================
# BUG REGRESSION — confirms specific old-bug behavior is gone
# =============================================================================
class TestBugRegression:
    def test_zero_fill_does_not_pretend_full_fill(self):
        """
        Bug lama: `float(order.get("filled", qty) or qty)` →
            `float(0 or 100)` → `float(100)` = 100.0 (pretend full fill!)

        Fix: filled=0 sekarang return error_result, caller harus return failure.
        """
        order_buggy_input = {"id": "abc", "status": "closed", "filled": 0.0}

        # Behavior lama (untuk dokumentasi):
        old_behavior = float(order_buggy_input.get("filled", 100.0) or 100.0)
        assert old_behavior == 100.0, "Confirming bug exists in old logic"

        # Behavior baru:
        filled, err = _parse_order_fill(order_buggy_input, 100.0)
        assert filled == 0.0
        assert err is not None
        assert err["status"] == "failed"


# =============================================================================
# FIXTURE — bikin execution.data_stream / position_manager controllable
# =============================================================================
@pytest.fixture
def execmod(monkeypatch):
    """
    Provide `execution` module dengan stub data_stream & position_manager yang
    bisa dikendalikan per-test. FEE_RATE & SYMBOL juga di-pin ke nilai test.

    Default state:
      - best_bid 1.500 / best_ask 1.501 (qty 1000 di tiap sisi)
      - orderbook: 5 level di bids & asks
      - position: side="none"
    """
    # Stub data_stream — pakai SimpleNamespace supaya kita bisa set attribute
    # bebas tanpa menabrak frozen-dict MagicMock.
    ds_stub = types.SimpleNamespace(
        best_bid={"price": 1.500, "qty": 1000.0},
        best_ask={"price": 1.501, "qty": 1000.0},
        best_bid_updated_at=time.time(),
        best_ask_updated_at=time.time(),
        # SortedDict supaya .items() ascending — mirip persis dengan production
        # (_calculate_vwap pakai reversed(bids.items()) untuk descending).
        orderbook_snapshot={
            "bids": SortedDict({1.498: 300.0, 1.499: 400.0, 1.500: 500.0}),
            "asks": SortedDict({1.501: 500.0, 1.502: 400.0, 1.503: 300.0}),
        },
    )

    pm_stub = MagicMock()
    pm_stub.get_position.return_value = {
        "side": "none", "entry_price": 0.0, "qty": 0.0, "balance": 1000.0,
    }
    pm_stub.open_position = MagicMock()
    pm_stub.close_position = MagicMock(return_value=(0.0, 0.0))
    # Optimistic state hooks (demo/live) — dipanggil execution setelah fill konfirmasi.
    pm_stub.set_position_optimistic = MagicMock()
    pm_stub.clear_position_optimistic = MagicMock()

    monkeypatch.setattr(execution, "data_stream", ds_stub)
    monkeypatch.setattr(execution, "position_manager", pm_stub)
    monkeypatch.setattr(execution, "log_trade", lambda *a, **kw: None)
    # Reset qty_step cache supaya tiap test fresh
    monkeypatch.setattr(execution, "_qty_step_cache", None)
    # Pin config values (config diimport jadi nama di execution namespace)
    monkeypatch.setattr(execution, "FEE_RATE", 0.0002)
    monkeypatch.setattr(execution, "SYMBOL", "XRPUSDT")
    monkeypatch.setattr(execution, "SLIPPAGE_TOLERANCE", 0.0005)

    return execution, ds_stub, pm_stub


# =============================================================================
# _round_to_step — helper rounding ke step terkecil exchange
# =============================================================================
class TestRoundToStep:
    def test_step_zero_returns_value_unchanged(self, execmod):
        execm, _, _ = execmod
        assert execm._round_to_step(123.456, 0) == 123.456

    def test_rounds_down_to_step(self, execmod):
        execm, _, _ = execmod
        # step 0.1 → 1.234 jadi 1.2 (floor ke nearest 0.1)
        assert execm._round_to_step(1.234, 0.1) == pytest.approx(1.2)

    def test_step_0_001_keeps_three_decimals(self, execmod):
        execm, _, _ = execmod
        assert execm._round_to_step(0.12345, 0.001) == pytest.approx(0.123)


# =============================================================================
# _check_stale_data — reject order kalau WS data basi
# =============================================================================
class TestCheckStaleData:
    def test_fresh_data_passes(self, execmod):
        execm, ds, _ = execmod
        ds.best_bid_updated_at = time.time()
        ds.best_ask_updated_at = time.time()
        ok, err = execm._check_stale_data()
        assert ok is True
        assert err == ""

    def test_timestamp_zero_means_no_data(self, execmod):
        execm, ds, _ = execmod
        ds.best_bid_updated_at = 0.0
        ds.best_ask_updated_at = 0.0
        ok, err = execm._check_stale_data()
        assert ok is False
        assert "not yet received" in err or "timestamp = 0" in err

    def test_stale_bid_detected(self, execmod):
        execm, ds, _ = execmod
        ds.best_bid_updated_at = time.time() - 10.0  # 10 detik lalu
        ds.best_ask_updated_at = time.time()
        ok, err = execm._check_stale_data()
        assert ok is False
        assert "bid" in err.lower()

    def test_stale_ask_detected(self, execmod):
        execm, ds, _ = execmod
        ds.best_bid_updated_at = time.time()
        ds.best_ask_updated_at = time.time() - 10.0
        ok, err = execm._check_stale_data()
        assert ok is False
        assert "ask" in err.lower()

    def test_stale_exchange_event_detected(self, execmod):
        """Waktu-terima fresh TAPI ts_event tua (feed lag) → reject (lapis ts_event)."""
        execm, ds, _ = execmod
        ds.best_bid_updated_at = time.time()
        ds.best_ask_updated_at = time.time()
        ds.best_bid_ts_event = time.time() - 10.0   # 10s tua di sisi exchange
        ok, err = execm._check_stale_data()
        assert ok is False
        assert "exchange" in err.lower() or "ts_event" in err.lower()

    def test_fresh_event_ts_passes(self, execmod):
        execm, ds, _ = execmod
        ds.best_bid_updated_at = time.time()
        ds.best_ask_updated_at = time.time()
        ds.best_bid_ts_event = time.time()
        ok, err = execm._check_stale_data()
        assert ok is True

    def test_event_ts_zero_skips_exchange_check(self, execmod):
        """ts_event=0 (exchange tak kasih timestamp) → lapis ts_event dilewati."""
        execm, ds, _ = execmod
        ds.best_bid_updated_at = time.time()
        ds.best_ask_updated_at = time.time()
        ds.best_bid_ts_event = 0.0
        ok, err = execm._check_stale_data()
        assert ok is True


# =============================================================================
# _calculate_vwap — depth-aware fill simulation
# =============================================================================
class TestCalculateVwap:
    def test_invalid_qty_returns_error(self, execmod):
        execm, _, _ = execmod
        vwap, fillable, err = execm._calculate_vwap("buy", 0.0)
        assert vwap is None
        assert "Invalid qty" in err

    def test_invalid_side_returns_error(self, execmod):
        execm, _, _ = execmod
        vwap, fillable, err = execm._calculate_vwap("xyz", 10.0)
        assert vwap is None
        assert "Invalid side" in err

    def test_empty_book_returns_error(self, execmod):
        execm, ds, _ = execmod
        ds.orderbook_snapshot = {"bids": {}, "asks": {}}
        vwap, fillable, err = execm._calculate_vwap("buy", 10.0)
        assert vwap is None
        assert fillable == 0.0

    def test_buy_walks_asks_ascending(self, execmod):
        """Buy 100 dari level 1.501 (500 qty available) — full fill di level 1 saja."""
        execm, _, _ = execmod
        vwap, fillable, err = execm._calculate_vwap("buy", 100.0)
        assert err == ""
        assert fillable == pytest.approx(100.0)
        assert vwap == pytest.approx(1.501)

    def test_buy_walks_multiple_levels(self, execmod):
        """Buy 600 → 500 di level 1.501, 100 di level 1.502."""
        execm, _, _ = execmod
        vwap, fillable, err = execm._calculate_vwap("buy", 600.0)
        assert err == ""
        assert fillable == pytest.approx(600.0)
        # VWAP weighted: (500*1.501 + 100*1.502) / 600 ≈ 1.50117
        expected = (500 * 1.501 + 100 * 1.502) / 600
        assert vwap == pytest.approx(expected)

    def test_sell_walks_bids_descending(self, execmod):
        """Sell harus mulai dari bid tertinggi (1.500)."""
        execm, _, _ = execmod
        vwap, fillable, err = execm._calculate_vwap("sell", 100.0)
        assert err == ""
        assert fillable == pytest.approx(100.0)
        # Bids dict di-reversed → level pertama = 1.500 (kunci terbesar)
        assert vwap == pytest.approx(1.500)

    def test_partial_fill_when_book_thin(self, execmod):
        """Minta 5000 saat book cuma punya 1200 total → fillable < qty."""
        execm, _, _ = execmod
        vwap, fillable, err = execm._calculate_vwap("buy", 5000.0)
        assert err == ""
        # Total ask depth = 500+400+300 = 1200
        assert fillable == pytest.approx(1200.0)
        # VWAP tetap valid
        assert vwap is not None


# =============================================================================
# _validate_price — stale + liquidity + slippage
# =============================================================================
class TestValidatePrice:
    def test_stale_data_rejects(self, execmod):
        execm, ds, _ = execmod
        ds.best_bid_updated_at = 0.0
        ok, err = execm._validate_price("buy", 1.501, qty=100.0)
        assert ok is False
        assert "Market data" in err or "Stale" in err or "timestamp" in err

    def test_fresh_buy_within_tolerance_passes(self, execmod):
        execm, _, _ = execmod
        # intended = current ask, drift = 0 → pass
        ok, err = execm._validate_price("buy", 1.501, qty=100.0)
        assert ok is True
        assert err == ""

    def test_fresh_sell_within_tolerance_passes(self, execmod):
        execm, _, _ = execmod
        ok, err = execm._validate_price("sell", 1.500, qty=100.0)
        assert ok is True

    def test_buy_slippage_exceeded_rejects(self, execmod):
        """intended jauh lebih murah dari VWAP (book bergerak naik)."""
        execm, _, _ = execmod
        # intended 1.0 vs vwap ~1.501 → drift ~50% > tolerance 0.05%
        ok, err = execm._validate_price("buy", 1.0, qty=100.0)
        assert ok is False
        assert "slippage" in err.lower() or "VWAP" in err

    def test_sell_slippage_exceeded_rejects(self, execmod):
        execm, _, _ = execmod
        # intended 10.0 vs vwap 1.500 → SELL drift huge
        ok, err = execm._validate_price("sell", 10.0, qty=100.0)
        assert ok is False
        assert "slippage" in err.lower() or "VWAP" in err

    def test_insufficient_liquidity_rejects(self, execmod):
        """Order > 99% dari total book depth → reject."""
        execm, _, _ = execmod
        # Total ask depth 1200; minta 5000 → fillable hanya 1200 (< 99% × 5000)
        ok, err = execm._validate_price("buy", 1.501, qty=5000.0)
        assert ok is False
        assert "liquidity" in err.lower() or "insufficient" in err.lower()

    def test_zero_qty_falls_back_to_drift_check(self, execmod):
        """qty=0 (legacy paper) → cek best-level drift, bukan VWAP."""
        execm, _, _ = execmod
        # Intended = ask → drift 0 → pass
        ok, err = execm._validate_price("buy", 1.501, qty=0.0)
        assert ok is True

    def test_zero_qty_drift_exceeded(self, execmod):
        execm, _, _ = execmod
        ok, err = execm._validate_price("buy", 1.0, qty=0.0)
        assert ok is False
        assert "Slippage" in err or "slippage" in err.lower()


# =============================================================================
# _paper_execute — eksekusi paper trading lengkap
# =============================================================================
class TestPaperExecute:
    def test_no_market_data_rejects(self, execmod):
        execm, ds, _ = execmod
        ds.best_ask = {"price": 0.0, "qty": 0.0}
        ds.best_bid = {"price": 0.0, "qty": 0.0}
        result = execm._paper_execute({"action": "buy", "qty": 10.0, "price": 1.5})
        assert result["status"] == "rejected"
        assert "No market data" in result["reason"]

    def test_buy_filled_calls_open_position(self, execmod):
        execm, _, pm = execmod
        sig = {"action": "buy", "qty": 100.0, "price": 1.501, "reason": "test"}
        result = execm._paper_execute(sig)
        assert result["status"] == "filled"
        assert result["price"] == pytest.approx(1.501)
        assert result["qty"] == 100.0
        # fee = qty * price * FEE_RATE = 100 * 1.501 * 0.0002
        assert result["fee"] == pytest.approx(100 * 1.501 * 0.0002, rel=1e-6)
        pm.open_position.assert_called_once()
        args = pm.open_position.call_args[0]
        assert args[0] == "long"
        assert args[1] == pytest.approx(1.501)
        assert args[2] == 100.0

    def test_sell_filled_calls_open_position_short(self, execmod):
        execm, _, pm = execmod
        sig = {"action": "sell", "qty": 100.0, "price": 1.500, "reason": "test"}
        result = execm._paper_execute(sig)
        assert result["status"] == "filled"
        assert result["price"] == pytest.approx(1.500)
        pm.open_position.assert_called_once()
        assert pm.open_position.call_args[0][0] == "short"

    def test_buy_rejected_when_slippage_too_high(self, execmod):
        execm, _, pm = execmod
        # intended jauh dari best ask
        result = execm._paper_execute({"action": "buy", "qty": 100.0, "price": 1.0})
        assert result["status"] == "rejected"
        pm.open_position.assert_not_called()

    def test_close_when_no_position_skips(self, execmod):
        execm, _, pm = execmod
        pm.get_position.return_value = {"side": "none", "entry_price": 0.0, "qty": 0.0}
        result = execm._paper_execute({"action": "close", "reason": "test"})
        assert result["status"] == "skipped"
        pm.close_position.assert_not_called()

    def test_close_long_uses_bid_price(self, execmod):
        execm, _, pm = execmod
        pm.get_position.return_value = {
            "side": "long", "entry_price": 1.480, "qty": 100.0, "balance": 1000.0,
        }
        pm.close_position.return_value = (2.0, 0.030)  # net_pnl, fee
        result = execm._paper_execute({"action": "close", "reason": "tp"})
        assert result["status"] == "closed"
        assert result["price"] == pytest.approx(1.500)  # bid
        # close_position called with bid price + exit_fee
        pm.close_position.assert_called_once()
        called_price = pm.close_position.call_args[0][0]
        assert called_price == pytest.approx(1.500)

    def test_close_short_uses_ask_price(self, execmod):
        execm, _, pm = execmod
        pm.get_position.return_value = {
            "side": "short", "entry_price": 1.520, "qty": 100.0, "balance": 1000.0,
        }
        pm.close_position.return_value = (1.9, 0.030)
        result = execm._paper_execute({"action": "close", "reason": "tp"})
        assert result["status"] == "closed"
        assert result["price"] == pytest.approx(1.501)  # ask

    def test_hold_action_returns_hold(self, execmod):
        execm, _, _ = execmod
        result = execm._paper_execute({"action": "hold"})
        assert result["status"] == "hold"


# =============================================================================
# _validate_price (zero qty) — drift check untuk SELL branch
# =============================================================================
class TestValidatePriceLegacyDrift:
    def test_zero_qty_sell_within_tolerance_passes(self, execmod):
        execm, _, _ = execmod
        # intended = bid → drift 0 → pass
        ok, err = execm._validate_price("sell", 1.500, qty=0.0)
        assert ok is True

    def test_zero_qty_sell_drift_exceeded(self, execmod):
        execm, _, _ = execmod
        # intended jauh > bid → drift turun > tolerance
        ok, err = execm._validate_price("sell", 10.0, qty=0.0)
        assert ok is False
        assert "Slippage" in err or "slippage" in err.lower()


# =============================================================================
# _get_qty_step — cache behaviour
# =============================================================================
class TestGetQtyStep:
    def test_cache_hit_returns_cached(self, execmod, monkeypatch):
        execm, _, _ = execmod
        monkeypatch.setattr(execm, "_qty_step_cache", 0.123)
        import asyncio
        step = asyncio.run(execm._get_qty_step())
        assert step == 0.123

    def test_cache_miss_falls_through_to_default_when_market_fails(self, execmod, monkeypatch):
        """exchange.market() raise → fungsi return default 0.001."""
        execm, _, _ = execmod

        def _boom(*a, **kw):
            raise RuntimeError("market info unavailable")

        # exchange di-bind ke execution.exchange saat module load — patch di sana.
        monkeypatch.setattr(execm.exchange, "market", _boom, raising=False)
        monkeypatch.setattr(execm, "_qty_step_cache", None)
        import asyncio
        step = asyncio.run(execm._get_qty_step())
        assert step == 0.001  # fallback default


# =============================================================================
# place_order — dispatcher paper/live
# =============================================================================
class TestPlaceOrder:
    def test_hold_action_is_noop(self, execmod):
        execm, _, pm = execmod
        # Hold tidak boleh sentuh open/close
        execm.place_order({"action": "hold"})
        pm.open_position.assert_not_called()
        pm.close_position.assert_not_called()

    def test_paper_mode_no_running_loop_uses_asyncio_run(self, execmod, monkeypatch):
        """place_order paper tanpa event loop berjalan → asyncio.run path."""
        execm, _, pm = execmod
        monkeypatch.setattr(execm, "MODE", "paper")

        # Pasang qty_step cache supaya tidak panggil exchange.market
        monkeypatch.setattr(execm, "_qty_step_cache", 0.001)

        # Hindari sleep nyata di test (loop simulate 50ms latency)
        import asyncio as _asyncio
        _orig_sleep = _asyncio.sleep

        async def _instant(*a, **kw):
            return None

        monkeypatch.setattr(_asyncio, "sleep", _instant)
        try:
            execm.place_order({"action": "buy", "reason": "test", "qty": 100.0, "price": 1.501})
        finally:
            monkeypatch.setattr(_asyncio, "sleep", _orig_sleep)

        # Buy → open_position("long", ...)
        pm.open_position.assert_called_once()
        assert pm.open_position.call_args[0][0] == "long"


# =============================================================================
# _live_place_order_async — InvalidOrder NO RETRY (regression bug 2026-05-29)
# =============================================================================
# Bug ke-detect saat smoke test demo: ketika position di Bybit sudah 0 tapi bot
# lokal masih kira ada (PnL sync belum catch up dalam 2s), close order ditolak
# Bybit dengan retCode 110017. Code lama: catch-all `except Exception` → retry
# 3x → log error spam. Fix: catch `ccxt.InvalidOrder`, kalau 110017 NO RETRY.
class TestLivePlaceOrderInvalidOrder:
    def test_invalid_order_110017_no_retry(self, execmod, monkeypatch):
        """110017 (position is zero) → return rejected setelah 1 attempt."""
        execm, _, pm = execmod
        pm.get_position.return_value = {
            "side": "long", "entry_price": 1.5, "qty": 100.0, "balance": 1000.0,
        }
        monkeypatch.setattr(execm, "_qty_step_cache", 0.1)

        import ccxt.pro as ccxt
        attempts = {"n": 0}

        async def _fake_order(*a, **kw):
            attempts["n"] += 1
            raise ccxt.InvalidOrder(
                'bybit {"retCode":110017,"retMsg":"current position is zero, '
                'cannot fix reduce-only order qty","result":{},"retExtInfo":{},"time":1780059696466}'
            )

        monkeypatch.setattr(execm.exchange, "create_order", _fake_order, raising=False)

        # Bypass asyncio.sleep
        import asyncio as _asyncio
        async def _instant(*a, **kw):
            return None
        monkeypatch.setattr(_asyncio, "sleep", _instant)

        result = _asyncio.run(execm._live_place_order_async(
            {"action": "close", "reason": "test", "qty": 100.0, "price": 1.5}
        ))

        # Cuma 1 attempt — NO RETRY
        assert attempts["n"] == 1, f"Expected NO RETRY, got {attempts['n']} attempts"
        assert result["status"] == "rejected"
        assert "desync" in result["reason"].lower() or "zero" in result["reason"].lower()

    def test_invalid_order_other_code_no_retry(self, execmod, monkeypatch):
        """InvalidOrder selain 110017 juga NO RETRY (config issue, gak akan recover)."""
        execm, _, pm = execmod
        pm.get_position.return_value = {
            "side": "long", "entry_price": 1.5, "qty": 100.0, "balance": 1000.0,
        }
        monkeypatch.setattr(execm, "_qty_step_cache", 0.1)

        import ccxt.pro as ccxt
        attempts = {"n": 0}

        async def _fake_order(*a, **kw):
            attempts["n"] += 1
            raise ccxt.InvalidOrder("bybit invalid leverage modification")

        monkeypatch.setattr(execm.exchange, "create_order", _fake_order, raising=False)
        import asyncio as _asyncio
        async def _instant(*a, **kw):
            return None
        monkeypatch.setattr(_asyncio, "sleep", _instant)

        result = _asyncio.run(execm._live_place_order_async(
            {"action": "close", "reason": "test", "qty": 100.0, "price": 1.5}
        ))

        assert attempts["n"] == 1
        assert result["status"] == "rejected"
        assert "InvalidOrder" in result["reason"]


# =============================================================================
# OPTIMISTIC STATE UPDATE — tutup race window duplicate-fire (bug 2026-05-29)
# =============================================================================
# Bug: _live_place_order_async tidak update position_manager setelah fill, jadi
# state cuma berubah tiap PNL_SYNC_INTERVAL (~2s) lewat sync loop. Di window itu
# strategy tick berikutnya lihat side="none" → fire order kembar. Fix: panggil
# set_position_optimistic / clear_position_optimistic setelah fill terkonfirmasi.
class TestOptimisticStateUpdate:
    def _patch_order(self, execm, monkeypatch, order_response):
        monkeypatch.setattr(execm, "_qty_step_cache", 0.1)

        async def _fake_order(*a, **kw):
            return order_response

        monkeypatch.setattr(execm.exchange, "create_order", _fake_order, raising=False)
        import asyncio as _asyncio

        async def _instant(*a, **kw):
            return None

        monkeypatch.setattr(_asyncio, "sleep", _instant)

    def test_buy_fill_sets_position_optimistic_long(self, execmod, monkeypatch):
        execm, _, pm = execmod
        self._patch_order(execm, monkeypatch, {"id": "ok", "status": "closed", "filled": 100.0})

        import asyncio as _asyncio
        result = _asyncio.run(execm._live_place_order_async(
            {"action": "buy", "reason": "test", "qty": 100.0, "price": 1.501}
        ))

        assert result["status"] == "filled"
        pm.set_position_optimistic.assert_called_once()
        args = pm.set_position_optimistic.call_args[0]
        assert args[0] == "long"
        assert args[2] == pytest.approx(100.0)  # filled qty
        pm.clear_position_optimistic.assert_not_called()

    def test_sell_fill_sets_position_optimistic_short(self, execmod, monkeypatch):
        execm, _, pm = execmod
        self._patch_order(execm, monkeypatch, {"id": "ok", "status": "closed", "filled": 100.0})

        import asyncio as _asyncio
        result = _asyncio.run(execm._live_place_order_async(
            {"action": "sell", "reason": "test", "qty": 100.0, "price": 1.500}
        ))

        assert result["status"] == "filled"
        pm.set_position_optimistic.assert_called_once()
        assert pm.set_position_optimistic.call_args[0][0] == "short"

    def test_close_full_fill_clears_position_optimistic(self, execmod, monkeypatch):
        execm, _, pm = execmod
        pm.get_position.return_value = {
            "side": "long", "entry_price": 1.48, "qty": 100.0, "balance": 1000.0,
        }
        self._patch_order(execm, monkeypatch, {"id": "ok", "status": "closed", "filled": 100.0})

        import asyncio as _asyncio
        result = _asyncio.run(execm._live_place_order_async(
            {"action": "close", "reason": "tp", "qty": 100.0, "price": 1.5}
        ))

        assert result["status"] == "filled"
        pm.clear_position_optimistic.assert_called_once()
        pm.set_position_optimistic.assert_not_called()

    def test_partial_close_does_not_clear(self, execmod, monkeypatch):
        """Partial close fill → JANGAN clear (posisi sisa masih ada, biar sync update qty)."""
        execm, _, pm = execmod
        pm.get_position.return_value = {
            "side": "long", "entry_price": 1.48, "qty": 100.0, "balance": 1000.0,
        }
        self._patch_order(execm, monkeypatch, {"id": "ok", "status": "closed", "filled": 40.0})

        import asyncio as _asyncio
        _asyncio.run(execm._live_place_order_async(
            {"action": "close", "reason": "tp", "qty": 100.0, "price": 1.5}
        ))

        pm.clear_position_optimistic.assert_not_called()

    def test_110017_clears_position_optimistic(self, execmod, monkeypatch):
        """Reject 110017 (exchange konfirmasi posisi zero) → clear state lokal sekarang."""
        execm, _, pm = execmod
        pm.get_position.return_value = {
            "side": "long", "entry_price": 1.5, "qty": 100.0, "balance": 1000.0,
        }
        monkeypatch.setattr(execm, "_qty_step_cache", 0.1)

        import ccxt.pro as ccxt

        async def _fake_order(*a, **kw):
            raise ccxt.InvalidOrder(
                'bybit {"retCode":110017,"retMsg":"current position is zero"}'
            )

        monkeypatch.setattr(execm.exchange, "create_order", _fake_order, raising=False)
        import asyncio as _asyncio

        async def _instant(*a, **kw):
            return None

        monkeypatch.setattr(_asyncio, "sleep", _instant)

        result = _asyncio.run(execm._live_place_order_async(
            {"action": "close", "reason": "test", "qty": 100.0, "price": 1.5}
        ))

        assert result["status"] == "rejected"
        pm.clear_position_optimistic.assert_called_once()

    def test_rejected_order_does_not_touch_state(self, execmod, monkeypatch):
        """Pre-order validation reject (stale data) → state tak disentuh, create_order tak dipanggil."""
        execm, ds, pm = execmod
        monkeypatch.setattr(execm, "_qty_step_cache", 0.1)
        # Data basi → _validate_price reject SEBELUM create_order (live pakai live ask/bid,
        # bukan signal["price"], jadi stale adalah cara reject yang deterministik).
        ds.best_bid_updated_at = 0.0
        ds.best_ask_updated_at = 0.0

        called = {"n": 0}

        async def _fake_order(*a, **kw):
            called["n"] += 1
            return {"id": "x", "status": "closed", "filled": 100.0}

        monkeypatch.setattr(execm.exchange, "create_order", _fake_order, raising=False)

        import asyncio as _asyncio
        result = _asyncio.run(execm._live_place_order_async(
            {"action": "buy", "reason": "test", "qty": 100.0, "price": 1.501}
        ))

        assert result["status"] == "rejected"
        assert called["n"] == 0, "create_order tidak boleh dipanggil saat pre-validation reject"
        pm.set_position_optimistic.assert_not_called()
        pm.clear_position_optimistic.assert_not_called()


# =============================================================================
# IN-FLIGHT GUARD — cegah dispatch order kembar saat 1 order belum selesai
# =============================================================================
class TestInFlightGuard:
    def test_guard_skips_dispatch_when_order_in_flight(self, execmod, monkeypatch):
        """Kalau _live_order_in_flight=True, place_order live SKIP tanpa create_order."""
        execm, _, pm = execmod
        monkeypatch.setattr(execm, "MODE", "demo")
        monkeypatch.setattr(execm, "_qty_step_cache", 0.1)
        monkeypatch.setattr(execm, "_live_order_in_flight", True)

        called = {"n": 0}

        async def _fake_order(*a, **kw):
            called["n"] += 1
            return {"id": "x", "status": "closed", "filled": 100.0}

        monkeypatch.setattr(execm.exchange, "create_order", _fake_order, raising=False)

        execm.place_order({"action": "buy", "reason": "dup", "qty": 100.0, "price": 1.501})

        assert called["n"] == 0, "Guard harus mencegah dispatch order kembar"

    def test_guard_clear_allows_dispatch(self, execmod, monkeypatch):
        """Flag False → order pertama jalan & flag di-reset setelah selesai (inline path)."""
        execm, _, pm = execmod
        monkeypatch.setattr(execm, "MODE", "demo")
        monkeypatch.setattr(execm, "_qty_step_cache", 0.1)
        monkeypatch.setattr(execm, "_live_order_in_flight", False)

        called = {"n": 0}

        async def _fake_order(*a, **kw):
            called["n"] += 1
            return {"id": "x", "status": "closed", "filled": 100.0}

        monkeypatch.setattr(execm.exchange, "create_order", _fake_order, raising=False)
        import asyncio as _asyncio

        async def _instant(*a, **kw):
            return None

        monkeypatch.setattr(_asyncio, "sleep", _instant)

        # Sync context (tidak ada running loop) → place_order pakai asyncio.run inline.
        execm.place_order({"action": "buy", "reason": "test", "qty": 100.0, "price": 1.501})

        assert called["n"] == 1, "Order pertama harus ter-dispatch"
        # Flag wajib reset ke False setelah order selesai (siap untuk order berikutnya)
        assert execm._live_order_in_flight is False
