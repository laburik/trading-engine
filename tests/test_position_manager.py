# =============================================================================
# tests/test_position_manager.py — Unit test untuk PnL math
# =============================================================================
# Cover: open_position, close_position, update_pnl, get_position, get_pnl_summary
# =============================================================================
from __future__ import annotations

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
