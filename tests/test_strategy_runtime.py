# =============================================================================
# tests/test_strategy_runtime.py — Unit test untuk engine orchestrator
# =============================================================================
# Cover:
#   - run_iteration (engine-managed mode)
#   - run_iteration (advanced mode dengan custom on_tick)
#   - _extract_ml_prob (regex parsing dari signal reason)
#   - error handling
# =============================================================================
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest


# =============================================================================
# FIXTURES
# =============================================================================
@pytest.fixture
def runtime():
    import strategy_runtime
    return strategy_runtime


@pytest.fixture
def fake_strategy_simple():
    """Strategy yang HANYA define generate_signal (engine-managed mode)."""
    mod = types.ModuleType("fake_strategy_simple")
    mod.generate_signal = MagicMock(return_value={"action": "hold", "reason": "test"})
    return mod


@pytest.fixture
def fake_strategy_with_on_tick():
    """Strategy yang OVERRIDE on_tick (advanced mode)."""
    mod = types.ModuleType("fake_strategy_with_on_tick")
    mod.generate_signal = MagicMock()
    mod.on_tick = MagicMock()
    return mod


@pytest.fixture
def mock_modules(monkeypatch):
    """Mock execution & bot_monitor agar tidak ada side effect ke production."""
    mock_exec = MagicMock()
    mock_monitor = MagicMock()
    monkeypatch.setitem(sys.modules, "execution", mock_exec)
    monkeypatch.setitem(sys.modules, "bot_monitor", mock_monitor)
    return {"execution": mock_exec, "bot_monitor": mock_monitor}


# =============================================================================
# _extract_ml_prob — regex parsing
# =============================================================================
class TestExtractMlProb:
    def test_extracts_from_typical_ml_reason(self, runtime):
        reason = "Sinyal RF ML Prob Naik > 60% (Skor: 0.72)"
        assert runtime._extract_ml_prob(reason) == 0.72

    def test_extracts_with_extra_decimals(self, runtime):
        reason = "Skor: 0.5"
        assert runtime._extract_ml_prob(reason) == 0.5

    def test_returns_none_when_no_match(self, runtime):
        assert runtime._extract_ml_prob("No crossover | val=0.123") is None

    def test_returns_none_for_empty(self, runtime):
        assert runtime._extract_ml_prob("") is None
        assert runtime._extract_ml_prob(None) is None

    def test_returns_none_for_invalid_number(self, runtime):
        assert runtime._extract_ml_prob("Skor: NaN-thing") is None


# =============================================================================
# run_iteration — engine-managed mode (default)
# =============================================================================
class TestRunIterationEngineManaged:
    def test_calls_generate_signal(self, runtime, fake_strategy_simple, mock_modules):
        runtime.run_iteration(fake_strategy_simple, {"foo": "bar"})
        fake_strategy_simple.generate_signal.assert_called_once_with({"foo": "bar"})

    def test_calls_record_tick_with_signal(self, runtime, fake_strategy_simple, mock_modules):
        runtime.run_iteration(fake_strategy_simple, {})
        mock_modules["bot_monitor"].record_tick.assert_called_once()
        args, kwargs = mock_modules["bot_monitor"].record_tick.call_args
        assert args[0] == {"action": "hold", "reason": "test"}

    def test_skips_place_order_when_hold(self, runtime, fake_strategy_simple, mock_modules):
        runtime.run_iteration(fake_strategy_simple, {})
        mock_modules["execution"].place_order.assert_not_called()

    def test_calls_place_order_for_buy(self, runtime, mock_modules):
        mod = types.ModuleType("strat_buy")
        mod.generate_signal = MagicMock(return_value={"action": "buy", "reason": "test"})
        runtime.run_iteration(mod, {})
        mock_modules["execution"].place_order.assert_called_once()

    def test_calls_place_order_for_sell(self, runtime, mock_modules):
        mod = types.ModuleType("strat_sell")
        mod.generate_signal = MagicMock(return_value={"action": "sell", "reason": "test"})
        runtime.run_iteration(mod, {})
        mock_modules["execution"].place_order.assert_called_once()

    def test_calls_place_order_for_close(self, runtime, mock_modules):
        mod = types.ModuleType("strat_close")
        mod.generate_signal = MagicMock(return_value={"action": "close", "reason": "test"})
        runtime.run_iteration(mod, {})
        mock_modules["execution"].place_order.assert_called_once()

    def test_extracts_ml_prob_from_reason(self, runtime, mock_modules):
        mod = types.ModuleType("strat_ml")
        mod.generate_signal = MagicMock(return_value={
            "action": "buy",
            "reason": "Sinyal ML (Skor: 0.85)"
        })
        runtime.run_iteration(mod, {})
        kwargs = mock_modules["bot_monitor"].record_tick.call_args.kwargs
        assert kwargs["ml_prob"] == 0.85


# =============================================================================
# run_iteration — advanced mode (custom on_tick)
# =============================================================================
class TestRunIterationAdvancedMode:
    def test_uses_custom_on_tick_when_defined(
        self, runtime, fake_strategy_with_on_tick, mock_modules
    ):
        runtime.run_iteration(fake_strategy_with_on_tick, {"data": "x"})
        fake_strategy_with_on_tick.on_tick.assert_called_once_with({"data": "x"})

    def test_skips_engine_orchestration_in_advanced_mode(
        self, runtime, fake_strategy_with_on_tick, mock_modules
    ):
        # Advanced mode → engine TIDAK panggil generate_signal/record_tick/place_order langsung
        runtime.run_iteration(fake_strategy_with_on_tick, {})
        fake_strategy_with_on_tick.generate_signal.assert_not_called()
        mock_modules["bot_monitor"].record_tick.assert_not_called()
        mock_modules["execution"].place_order.assert_not_called()


# =============================================================================
# Error handling
# =============================================================================
class TestErrorHandling:
    def test_generate_signal_crash_calls_record_error(self, runtime, mock_modules):
        mod = types.ModuleType("strat_crash")
        mod.generate_signal = MagicMock(side_effect=ValueError("oops"))
        runtime.run_iteration(mod, {})
        mock_modules["bot_monitor"].record_error.assert_called_once()
        # place_order TIDAK dipanggil saat error
        mock_modules["execution"].place_order.assert_not_called()

    def test_invalid_signal_format_records_error(self, runtime, mock_modules):
        mod = types.ModuleType("strat_invalid")
        mod.generate_signal = MagicMock(return_value="not a dict")
        runtime.run_iteration(mod, {})
        mock_modules["bot_monitor"].record_error.assert_called_once()

    def test_signal_missing_action_key_records_error(self, runtime, mock_modules):
        mod = types.ModuleType("strat_missing")
        mod.generate_signal = MagicMock(return_value={"reason": "no action"})
        runtime.run_iteration(mod, {})
        mock_modules["bot_monitor"].record_error.assert_called_once()
