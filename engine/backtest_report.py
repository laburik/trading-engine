# =============================================================================
# backtest_report.py — Render hasil backtest → laporan HTML self-contained
# =============================================================================
# Modul PRESENTASI murni: input dict hasil simulator + dict metrik + meta →
# string HTML (chart inline SVG, CSS inline → bisa dibuka offline). TIDAK import
# backtest_core (tak menyentuh mesin simulasi), jadi aman & gampang dites.
#
# Template HTML dipisah di engine/templates/backtest_report.html (placeholder
# gaya $NAMA via string.Template). Pemisahan: core=sim, metrics=math,
# data=IO, report=presentasi.
# =============================================================================
from __future__ import annotations

import html as _html
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from string import Template

_TEMPLATE_PATH = Path(__file__).parent / "templates" / "backtest_report.html"


def _load_template() -> str:
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def _fmt(v: float, nd: int = 2) -> str:
    return f"{v:,.{nd}f}"


# =============================================================================
# CHART — inline SVG (nol dependency, render di browser tanpa internet)
# =============================================================================
def _svg_equity_curve(equity: list[float], initial: float,
                      width: int = 880, height: int = 280, pad: int = 38) -> str:
    """Kurva ekuitas sebagai polyline SVG + area fill + garis dasar modal awal."""
    if not equity:
        return '<div class="empty">Tidak ada data ekuitas.</div>'

    n = len(equity)
    lo = min(min(equity), initial)
    hi = max(max(equity), initial)
    span = (hi - lo) or 1.0           # flat → hindari bagi nol
    inner_w = width - 2 * pad
    inner_h = height - 2 * pad

    def X(i: int) -> float:
        return pad + (inner_w * (i / (n - 1))) if n > 1 else pad + inner_w / 2.0

    def Y(v: float) -> float:
        return pad + inner_h * (1.0 - (v - lo) / span)

    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(equity))
    base_y = Y(initial)
    bottom = pad + inner_h
    # Area di bawah kurva: mulai kiri-bawah → kurva → kanan-bawah (polyline auto-close).
    area = f"{pad:.1f},{bottom:.1f} {pts} {pad + inner_w:.1f},{bottom:.1f}"
    up = equity[-1] >= initial
    line = "#16a34a" if up else "#dc2626"
    fill = "rgba(22,163,74,0.13)" if up else "rgba(220,38,38,0.13)"

    return (
        f'<svg viewBox="0 0 {width} {height}" class="equity" '
        f'preserveAspectRatio="none" role="img" aria-label="Kurva ekuitas">\n'
        f'  <polyline points="{area}" fill="{fill}" stroke="none"/>\n'
        f'  <line x1="{pad}" y1="{base_y:.1f}" x2="{pad + inner_w}" y2="{base_y:.1f}" '
        f'stroke="#94a3b8" stroke-width="1" stroke-dasharray="4 4"/>\n'
        f'  <polyline points="{pts}" fill="none" stroke="{line}" stroke-width="2"/>\n'
        f'  <text x="{pad}" y="{pad - 12:.0f}" class="axis">{_fmt(hi)}</text>\n'
        f'  <text x="{pad}" y="{height - pad + 20:.0f}" class="axis">{_fmt(lo)}</text>\n'
        f'  <text x="{pad + inner_w - 4:.0f}" y="{base_y - 6:.1f}" class="axis" '
        f'text-anchor="end">awal {_fmt(initial)}</text>\n'
        f'</svg>'
    )


def _svg_winloss(wins: int, losses: int, width: int = 880, height: int = 46) -> str:
    """Bar horizontal proporsi win vs loss."""
    total = wins + losses
    if total == 0:
        return '<div class="empty">Belum ada trade.</div>'
    w_frac = wins / total
    w_px = width * w_frac
    return (
        f'<svg viewBox="0 0 {width} {height}" class="wl" '
        f'preserveAspectRatio="none" role="img" aria-label="Win vs Loss">\n'
        f'  <rect x="0" y="0" width="{w_px:.1f}" height="{height}" fill="#16a34a"/>\n'
        f'  <rect x="{w_px:.1f}" y="0" width="{width - w_px:.1f}" height="{height}" fill="#dc2626"/>\n'
        f'  <text x="14" y="{height / 2 + 5:.0f}" class="wl-label">Win {wins} ({w_frac * 100:.0f}%)</text>\n'
        f'  <text x="{width - 14}" y="{height / 2 + 5:.0f}" class="wl-label" '
        f'text-anchor="end">Loss {losses} ({(1 - w_frac) * 100:.0f}%)</text>\n'
        f'</svg>'
    )


def _trade_rows(trades: list[dict]) -> str:
    if not trades:
        return '<tr><td colspan="7" class="empty">Tidak ada trade.</td></tr>'
    rows: list[str] = []
    for i, t in enumerate(trades, 1):
        pnl = t.get("pnl", 0.0)
        cls = "pos" if pnl > 0 else "neg"
        rows.append(
            f'<tr><td>{i}</td>'
            f'<td>{_html.escape(str(t.get("side", "")))}</td>'
            f'<td>{t.get("entry", 0.0):.4f}</td>'
            f'<td>{t.get("exit", 0.0):.4f}</td>'
            f'<td class="{cls}">{pnl:+.4f}</td>'
            f'<td>{t.get("fee", 0.0):.4f}</td>'
            f'<td>{_html.escape(str(t.get("reason", "")))}</td></tr>'
        )
    return "\n        ".join(rows)


# =============================================================================
# RENDER + WRITE
# =============================================================================
def render_report(result: dict, metrics: dict, meta: dict) -> str:
    """Hasil + metrik + meta → string HTML penuh (placeholder tersubstitusi)."""
    m = metrics
    trades = result.get("trades", [])
    equity = result.get("equity_curve", [])
    initial = float(meta.get("initial_balance", 0.0))
    final = float(result.get("final_balance", initial))
    num = m["num_trades"]
    wins = round(num * m["win_rate"] / 100.0)
    losses = num - wins
    pnl = m["pnl"]
    pf = m["profit_factor"]

    mapping = {
        "TITLE":             _html.escape(f"Backtest {meta.get('symbol', '')} {meta.get('timeframe', '')}"),
        "SYMBOL":            _html.escape(str(meta.get("symbol", ""))),
        "TIMEFRAME":         _html.escape(str(meta.get("timeframe", ""))),
        "RANGE":             _html.escape(str(meta.get("range", ""))),
        "STRATEGY":          _html.escape(str(meta.get("strategy", ""))),
        "MODEL":             _html.escape(str(meta.get("model", ""))),
        "REALISM":           "1m intrabar" if meta.get("realism") else "level-candle",
        "GENERATED_AT":      datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "NET_PNL":           f"{pnl:+,.2f}",
        "NET_PNL_PCT":       f"{m['pnl_pct']:+.2f}",
        "PNL_CLASS":         "pos" if pnl >= 0 else "neg",
        "INITIAL_BALANCE":   _fmt(initial),
        "FINAL_BALANCE":     _fmt(final),
        "PROFIT_FACTOR":     "inf" if math.isinf(pf) else f"{pf:.2f}",
        "MAX_DD":            f"{m['max_dd_pct']:.2f}",
        "NUM_TRADES":        str(num),
        "NUM_WINS":          str(wins),
        "NUM_LOSSES":        str(losses),
        "WIN_RATE":          f"{m['win_rate']:.1f}",
        "MAX_CONSEC_WINS":   str(m["max_consec_wins"]),
        "MAX_CONSEC_LOSSES": str(m["max_consec_losses"]),
        "TOTAL_FEE":         _fmt(m.get("total_fee", 0.0)),
        "SHARPE":            f"{m['sharpe']:.3f}",
        "GROSS_PROFIT":      _fmt(m.get("gross_profit", 0.0)),
        "GROSS_LOSS":        _fmt(m.get("gross_loss", 0.0)),
        "AVG_WIN":           _fmt(m.get("avg_win", 0.0)),
        "AVG_LOSS":          _fmt(m.get("avg_loss", 0.0)),
        "EXPECTANCY":        f"{m.get('expectancy', 0.0):+.4f}",
        "EQUITY_SVG":        _svg_equity_curve(equity, initial),
        "WINLOSS_SVG":       _svg_winloss(wins, losses),
        "TRADE_ROWS":        _trade_rows(trades),
    }
    return Template(_load_template()).safe_substitute(mapping)


def write_report(result: dict, metrics: dict, meta: dict, out_dir: str) -> str:
    """Render + tulis ke out_dir. Return path file. Folder dibuat kalau belum ada."""
    html_str = render_report(result, metrics, meta)
    os.makedirs(out_dir, exist_ok=True)
    sym = str(meta.get("symbol", "sym")).replace("/", "").replace(":", "")
    tf = str(meta.get("timeframe", "tf"))
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, f"backtest_{sym}_{tf}_{ts}.html")
    Path(path).write_text(html_str, encoding="utf-8")
    return path
