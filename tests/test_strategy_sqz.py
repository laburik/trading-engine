# =============================================================================
# tests/test_strategy_sqz.py — Unit test untuk strategy.py SQZ MOM
# =============================================================================
# Cover: warmup guard, candle count guard, spread guard, signal format
# =============================================================================
from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.conftest import make_candles


# =============================================================================
# GUARDS — situasi yang harus return "hold"
# =============================================================================
class TestGuards:
    def test_warmup_returns_hold(self, warmup_market_data):
        import strategy
        result = strategy.generate_signal(warmup_market_data)
        assert result["action"] == "hold"
        assert "Warmup" in result["reason"]

    def test_not_enough_candles_returns_hold(self):
        import strategy
        few_candles = make_candles(n=5)
        data = {
            "candles": {"2h": few_candles[:-1]},
            "current": {"2h": few_candles[-1]},
            "best_bid": {"price": 1.5}, "best_ask": {"price": 1.501},
            "bid_ask_spread": 0.0001, "funding_rate": 0.0,
            "is_warmup": False,
        }
        result = strategy.generate_signal(data)
        assert result["action"] == "hold"
        assert "Not enough" in result["reason"]

    def test_wide_spread_returns_hold(self, dummy_market_data):
        import strategy
        data = dict(dummy_market_data)
        data["bid_ask_spread"] = 0.05  # > MAX_SPREAD_USDT (0.0005)
        result = strategy.generate_signal(data)
        assert result["action"] == "hold"
        assert "Spread too wide" in result["reason"]

    def test_missing_current_candle_returns_hold(self, dummy_market_data):
        import strategy
        data = dict(dummy_market_data)
        data["current"] = {"2h": None}
        result = strategy.generate_signal(data)
        assert result["action"] == "hold"


# =============================================================================
# SIGNAL FORMAT CONTRACT
# =============================================================================
class TestSignalFormat:
    def test_returns_dict(self, dummy_market_data):
        import strategy
        result = strategy.generate_signal(dummy_market_data)
        assert isinstance(result, dict)

    def test_has_action_key(self, dummy_market_data):
        import strategy
        result = strategy.generate_signal(dummy_market_data)
        assert "action" in result

    def test_action_in_valid_set(self, dummy_market_data):
        import strategy
        result = strategy.generate_signal(dummy_market_data)
        assert result["action"] in {"buy", "sell", "close", "hold"}

    def test_has_reason_key(self, dummy_market_data):
        import strategy
        result = strategy.generate_signal(dummy_market_data)
        assert "reason" in result
        assert isinstance(result["reason"], str)

    def test_warmup_action_is_hold_for_multiple_seeds(self):
        import strategy
        # Pastikan warmup ALWAYS hold, regardless of data shape
        for seed in (0, 7, 42, 99, 123):
            candles = make_candles(n=100, seed=seed)
            data = {
                "candles": {"2h": candles[:-1]}, "current": {"2h": candles[-1]},
                "best_bid": {"price": 1.5}, "best_ask": {"price": 1.501},
                "bid_ask_spread": 0.0001, "funding_rate": 0.0001,
                "is_warmup": True,
            }
            assert strategy.generate_signal(data)["action"] == "hold"


# =============================================================================
# POSITION-AWARE EXITS (Take Profit / Stop Loss)
# =============================================================================
class TestPositionExits:
    def _data_with_price(self, bid: float, ask: float):
        candles = make_candles(n=100)
        return {
            "candles": {"2h": candles[:-1]}, "current": {"2h": candles[-1]},
            "best_bid": {"price": bid}, "best_ask": {"price": ask},
            # Spread sengaja di-set kecil supaya tidak kena MAX_SPREAD_USDT guard.
            # Kita test TP/SL logic, bukan spread guard.
            "bid_ask_spread": 0.0001, "funding_rate": 0.0001,
            "is_warmup": False,
        }

    def test_long_take_profit_triggers_close(self):
        import strategy
        # entry @1.5, TAKE_PROFIT_PCT=0.020 → TP @1.530
        with patch("strategy.get_position", return_value={"side": "long", "entry_price": 1.5}):
            data = self._data_with_price(bid=1.55, ask=1.5501)  # bid jauh di atas TP
            result = strategy.generate_signal(data)
            assert result["action"] == "close"
            assert "Take profit" in result["reason"]

    def test_long_stop_loss_triggers_close(self):
        import strategy
        # entry @1.5, STOP_LOSS_PCT=0.008 → SL @1.488
        with patch("strategy.get_position", return_value={"side": "long", "entry_price": 1.5}):
            data = self._data_with_price(bid=1.40, ask=1.4001)  # bid jauh di bawah SL
            result = strategy.generate_signal(data)
            assert result["action"] == "close"
            assert "Stop loss" in result["reason"]

    def test_short_take_profit_triggers_close(self):
        import strategy
        # entry @1.5 short, TP @1.470
        with patch("strategy.get_position", return_value={"side": "short", "entry_price": 1.5}):
            data = self._data_with_price(bid=1.40, ask=1.4001)  # ask jauh di bawah TP
            result = strategy.generate_signal(data)
            assert result["action"] == "close"
            assert "Take profit" in result["reason"]

    def test_short_stop_loss_triggers_close(self):
        import strategy
        # entry @1.5 short, SL @1.512
        with patch("strategy.get_position", return_value={"side": "short", "entry_price": 1.5}):
            data = self._data_with_price(bid=1.59, ask=1.5901)  # ask jauh di atas SL
            result = strategy.generate_signal(data)
            assert result["action"] == "close"
            assert "Stop loss" in result["reason"]


# =============================================================================
# HELPER: _linreg & _compute_sqzmom (private but worth covering)
# =============================================================================
class TestLinReg:
    def test_zero_when_insufficient_data(self):
        import strategy
        assert strategy._linreg([1.0, 2.0], length=10) == 0.0

    def test_constant_series_returns_constant(self):
        import strategy
        # Series konstan → linreg = nilai konstan
        result = strategy._linreg([5.0] * 10, length=10)
        assert result == pytest.approx(5.0)

    def test_increasing_series_extrapolates(self):
        import strategy
        # 1, 2, 3, 4, 5 → linreg di point terakhir ≈ 5
        result = strategy._linreg([1.0, 2.0, 3.0, 4.0, 5.0], length=5)
        assert result == pytest.approx(5.0, abs=0.01)
