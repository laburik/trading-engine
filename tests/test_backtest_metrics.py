# =============================================================================
# tests/test_backtest_metrics.py — Unit test untuk engine/backtest_metrics.py
# =============================================================================
# Pure functions atas dict hasil simulator → mudah dites deterministik.
# =============================================================================
from __future__ import annotations

import math

import pytest

import backtest_metrics as bm


# --- helper ----------------------------------------------------------------
def _result(trades, equity_curve, final_balance):
    return {"trades": trades, "equity_curve": equity_curve, "final_balance": final_balance}


# =============================================================================
# compute_metrics
# =============================================================================
class TestComputeMetrics:
    def test_empty_result_all_zero(self):
        m = bm.compute_metrics(_result([], [], 1000.0), 1000.0)
        assert m["num_trades"] == 0
        assert m["pnl"] == 0.0
        assert m["pnl_pct"] == 0.0
        assert m["win_rate"] == 0.0
        assert m["profit_factor"] == 0.0      # gross_profit==0 → 0.0 (bukan inf)
        assert m["max_dd_pct"] == 0.0
        assert m["sharpe"] == 0.0
        assert m["total_fee"] == 0.0
        assert m["max_consec_wins"] == 0
        assert m["max_consec_losses"] == 0

    def test_mixed_trades_metrics(self):
        trades = [
            {"pnl": 10.0, "fee": 0.5},
            {"pnl": -4.0, "fee": 0.5},
            {"pnl": 6.0, "fee": 0.5},
        ]
        equity = [1000.0, 1010.0, 1006.0, 1012.0]
        m = bm.compute_metrics(_result(trades, equity, 1012.0), 1000.0)

        assert m["num_trades"] == 3
        assert m["pnl"] == pytest.approx(12.0)
        assert m["pnl_pct"] == pytest.approx(1.2)
        assert m["win_rate"] == pytest.approx(66.6667, rel=1e-3)
        assert m["profit_factor"] == pytest.approx(16.0 / 4.0)   # 4.0
        assert m["total_fee"] == pytest.approx(1.5)
        assert m["max_consec_wins"] == 1
        assert m["max_consec_losses"] == 1
        assert m["max_dd_pct"] > 0.0                              # dari equity curve
        assert m["sharpe"] > 0.0                                  # num_trades>1 & std>0

    def test_all_wins_profit_factor_inf(self):
        trades = [{"pnl": 5.0}, {"pnl": 7.0}, {"pnl": 3.0}]
        m = bm.compute_metrics(_result(trades, [1000, 1005, 1012, 1015], 1015.0), 1000.0)
        assert math.isinf(m["profit_factor"])
        assert m["win_rate"] == 100.0
        assert m["max_consec_wins"] == 3
        assert m["max_consec_losses"] == 0

    def test_consecutive_losses_streak(self):
        trades = [{"pnl": -1.0}, {"pnl": -2.0}, {"pnl": -3.0}, {"pnl": 4.0}]
        m = bm.compute_metrics(_result(trades, [], 998.0), 1000.0)
        assert m["max_consec_losses"] == 3
        assert m["max_consec_wins"] == 1
        # pnl<=0 dihitung loss; profit_factor finite karena ada 1 win
        assert not math.isinf(m["profit_factor"])

    def test_initial_balance_zero_pnl_pct_zero(self):
        m = bm.compute_metrics(_result([{"pnl": 1.0}], [10.0], 11.0), 0.0)
        assert m["pnl_pct"] == 0.0          # cabang initial_balance<=0

    def test_initial_balance_zero_multi_trade_no_crash(self):
        # Guard: initial_balance==0 DAN >1 trade tidak boleh ZeroDivisionError di Sharpe.
        m = bm.compute_metrics(
            _result([{"pnl": 1.0}, {"pnl": 2.0}], [10.0, 12.0], 13.0), 0.0)
        assert m["sharpe"] == 0.0           # cabang Sharpe di-skip karena initial_balance<=0
        assert m["pnl_pct"] == 0.0

    def test_single_trade_sharpe_zero(self):
        # num_trades==1 → sharpe = 0.0 (cabang else)
        m = bm.compute_metrics(_result([{"pnl": 5.0}], [1000, 1005], 1005.0), 1000.0)
        assert m["sharpe"] == 0.0

    def test_fee_missing_defaults_zero(self):
        m = bm.compute_metrics(_result([{"pnl": 1.0}, {"pnl": 2.0}], [], 1003.0), 1000.0)
        assert m["total_fee"] == 0.0


# =============================================================================
# _fmt_pf + report printers
# =============================================================================
class TestFormatters:
    def test_fmt_pf_inf(self):
        assert bm._fmt_pf(float("inf")) == "inf"

    def test_fmt_pf_number(self):
        assert bm._fmt_pf(2.345) == "2.35"

    def test_print_metric_block_runs(self, capsys):
        m = bm.compute_metrics(
            _result([{"pnl": 5.0, "fee": 0.1}, {"pnl": -2.0, "fee": 0.1}],
                    [1000, 1005, 1003], 1003.0), 1000.0)
        bm.print_metric_block("In-Sample", m)
        out = capsys.readouterr().out
        assert "In-Sample" in out
        assert "PnL" in out
        assert "Sharpe" in out

    def test_print_backtest_report_runs(self, capsys):
        m = bm.compute_metrics(
            _result([{"pnl": 5.0, "fee": 0.1}], [1000, 1005], 1005.0), 1000.0)
        bm.print_backtest_report("HEADER X", m)
        out = capsys.readouterr().out
        assert "HEADER X" in out
        assert "Net PnL" in out
        assert "Win Rate" in out

    def test_print_backtest_report_negative_pnl(self, capsys):
        # cabang pnl < 0 (tanpa tanda '+')
        m = bm.compute_metrics(_result([{"pnl": -5.0}], [1000, 995], 995.0), 1000.0)
        bm.print_backtest_report("LOSS", m)
        out = capsys.readouterr().out
        assert "-5.00" in out
