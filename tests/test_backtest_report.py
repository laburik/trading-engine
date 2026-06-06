# =============================================================================
# tests/test_backtest_report.py — Unit test untuk engine/backtest_report.py
# =============================================================================
# backtest_report = modul PRESENTASI (dict hasil + metrik → HTML). Tidak import
# backtest_core, jadi AMAN di-import top-level (tak ada mock sys.modules).
# =============================================================================
from __future__ import annotations

import math
import os

import pytest

import backtest_metrics as bm
import backtest_report as br


def _result(trades, equity, final):
    return {"trades": trades, "equity_curve": equity, "final_balance": final}


def _meta(**kw):
    base = {"symbol": "XRPUSDT", "timeframe": "2h", "range": "2026-01-01 → now",
            "strategy": "strategy", "model": "single", "realism": False,
            "initial_balance": 1000.0}
    base.update(kw)
    return base


def _full(trades=None, equity=None):
    trades = trades if trades is not None else [
        {"side": "long", "entry": 100.0, "exit": 110.0, "pnl": 6.0,
         "fee": 0.1, "funding": 0.0, "reason": "TP"},
        {"side": "short", "entry": 110.0, "exit": 112.0, "pnl": -1.2,
         "fee": 0.1, "funding": 0.0, "reason": "SL"},
    ]
    equity = equity if equity is not None else [1000.0, 1006.0, 1004.8]
    res = _result(trades, equity, 1004.8)
    met = bm.compute_metrics(res, 1000.0)
    return res, met


# =============================================================================
# compute_metrics — field tambahan utk laporan (gross/avg/expectancy)
# =============================================================================
class TestMetricsExposed:
    def test_gross_avg_expectancy_exposed(self):
        trades = [{"pnl": 10.0, "fee": 0.5}, {"pnl": -4.0, "fee": 0.5}]
        m = bm.compute_metrics(_result(trades, [1000, 1010, 1006], 1006.0), 1000.0)
        assert m["gross_profit"] == pytest.approx(10.0)
        assert m["gross_loss"] == pytest.approx(4.0)
        assert m["avg_win"] == pytest.approx(10.0)
        assert m["avg_loss"] == pytest.approx(-4.0)
        assert m["expectancy"] == pytest.approx((10.0 - 4.0) / 2)

    def test_empty_exposed_zero(self):
        m = bm.compute_metrics(_result([], [], 1000.0), 1000.0)
        for k in ("gross_profit", "gross_loss", "avg_win", "avg_loss", "expectancy"):
            assert m[k] == 0.0


# =============================================================================
# render_report — struktur HTML
# =============================================================================
class TestRender:
    def test_template_path_exists(self):
        assert br._TEMPLATE_PATH.is_file()

    def test_has_core_sections(self):
        res, met = _full()
        out = br.render_report(res, met, _meta())
        assert "<html" in out.lower()
        assert "XRPUSDT" in out
        assert "<svg" in out and "<polyline" in out      # chart ekuitas inline SVG
        assert "$" not in out, "semua placeholder $ harus tersubstitusi"

    def test_trade_rows_rendered(self):
        res, met = _full()
        out = br.render_report(res, met, _meta())
        assert out.count("<tr") >= 3        # header + 2 baris trade
        assert "TP" in out and "SL" in out

    def test_pnl_color_classes(self):
        res, met = _full()
        out = br.render_report(res, met, _meta())
        assert 'class="pos"' in out and 'class="neg"' in out

    def test_empty_trades_no_crash(self):
        res, met = _full(trades=[], equity=[])
        out = br.render_report(res, met, _meta())
        assert "<html" in out.lower()
        assert "Tidak ada" in out             # marker kosong yang ramah

    def test_html_escapes_reason(self):
        trades = [{"side": "long", "entry": 1.0, "exit": 2.0, "pnl": 1.0,
                   "fee": 0.0, "funding": 0.0, "reason": "<script>x</script>"}]
        res, met = _full(trades=trades, equity=[1000, 1001])
        out = br.render_report(res, met, _meta())
        assert "<script>x</script>" not in out
        assert "&lt;script&gt;" in out


# =============================================================================
# SVG helpers
# =============================================================================
class TestSvg:
    def test_equity_polyline(self):
        svg = br._svg_equity_curve([1000.0, 1010.0, 1005.0, 1020.0], 1000.0)
        assert svg.startswith("<svg") and "<polyline" in svg

    def test_equity_empty_graceful(self):
        svg = br._svg_equity_curve([], 1000.0)
        assert "Tidak ada" in svg            # tidak crash, marker kosong

    def test_equity_flat_no_zero_division(self):
        svg = br._svg_equity_curve([1000.0, 1000.0, 1000.0], 1000.0)
        assert "<polyline" in svg            # span 0 ditangani

    def test_winloss_bar(self):
        svg = br._svg_winloss(3, 1)
        assert "<svg" in svg and "<rect" in svg


# =============================================================================
# write_report — file IO
# =============================================================================
class TestWrite:
    def test_creates_html_file(self, tmp_path):
        res, met = _full()
        path = br.write_report(res, met, _meta(), str(tmp_path))
        assert os.path.isfile(path)
        assert path.endswith(".html")
        content = open(path, encoding="utf-8").read()
        assert "XRPUSDT" in content and "<svg" in content

    def test_makedirs_if_missing(self, tmp_path):
        out = os.path.join(str(tmp_path), "nested", "backtest_data")
        res, met = _full()
        path = br.write_report(res, met, _meta(), out)
        assert os.path.isfile(path)
