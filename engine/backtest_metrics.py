# =============================================================================
# backtest_metrics.py — Perhitungan metrik dari hasil simulasi + output teks
# =============================================================================
# Diekstrak dari hypertune.py. Pure functions atas dict hasil simulator
# ({"trades", "equity_curve", "final_balance"}). Dipakai bersama backtest.py &
# hypertune.py.
# =============================================================================
from __future__ import annotations

import math

import numpy as np


def compute_metrics(result: dict, initial_balance: float) -> dict:
    trades: list[dict] = result["trades"]
    equity_curve: list[float] = result["equity_curve"]
    final_balance: float = result["final_balance"]

    total_pnl = final_balance - initial_balance
    total_pnl_pct = (total_pnl / initial_balance) * 100 if initial_balance > 0 else 0.0

    num_trades = len(trades)
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    win_rate = (len(wins) / num_trades * 100) if num_trades > 0 else 0.0

    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss   = abs(sum(t["pnl"] for t in losses))
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = float("inf") if gross_profit > 0 else 0.0

    avg_win  = (gross_profit / len(wins)) if wins else 0.0
    avg_loss = (sum(t["pnl"] for t in losses) / len(losses)) if losses else 0.0  # <= 0
    expectancy = (total_pnl / num_trades) if num_trades > 0 else 0.0

    if equity_curve:
        eq = np.array(equity_curve, dtype=float)
        running_max = np.maximum.accumulate(eq)
        dd_pct = np.where(running_max > 0, (running_max - eq) / running_max, 0.0)
        max_dd_pct = float(dd_pct.max() * 100)
    else:
        max_dd_pct = 0.0

    max_consec_wins = 0
    max_consec_losses = 0
    cur_wins = 0
    cur_losses = 0
    for tr in trades:
        if tr["pnl"] > 0:
            cur_wins += 1
            cur_losses = 0
            if cur_wins > max_consec_wins:
                max_consec_wins = cur_wins
        else:
            cur_losses += 1
            cur_wins = 0
            if cur_losses > max_consec_losses:
                max_consec_losses = cur_losses

    if num_trades > 1 and initial_balance > 0:
        returns = np.array([t["pnl"] / initial_balance for t in trades])
        std = returns.std(ddof=1)
        sharpe = float(returns.mean() / std * math.sqrt(num_trades)) if std > 0 else 0.0
    else:
        sharpe = 0.0

    # Total fee = jumlah fee per trade (kalau simulator menyimpannya). Aman kalau
    # field 'fee' tidak ada (default 0) → metrik lain tak terpengaruh.
    total_fee = sum(t.get("fee", 0.0) for t in trades)

    return {
        "pnl":               total_pnl,
        "pnl_pct":           total_pnl_pct,
        "num_trades":        num_trades,
        "win_rate":          win_rate,
        "profit_factor":     profit_factor,
        "gross_profit":      gross_profit,
        "gross_loss":        gross_loss,
        "avg_win":           avg_win,
        "avg_loss":          avg_loss,
        "expectancy":        expectancy,
        "max_dd_pct":        max_dd_pct,
        "max_consec_wins":   max_consec_wins,
        "max_consec_losses": max_consec_losses,
        "sharpe":            sharpe,
        "total_fee":         total_fee,
    }


def _fmt_pf(pf: float) -> str:
    return "inf" if math.isinf(pf) else f"{pf:.2f}"


def print_metric_block(label: str, m: dict) -> None:
    sign = "+" if m["pnl"] >= 0 else ""
    print(f"  {label}:")
    print(f"    PnL: {sign}{m['pnl']:.2f} USDT ({sign}{m['pnl_pct']:.2f}%)   "
          f"Trades: {m['num_trades']}   Win: {m['win_rate']:.1f}%")
    print(f"    Max DD: {m['max_dd_pct']:.2f}%   Profit Factor: {_fmt_pf(m['profit_factor'])}   "
          f"Sharpe: {m['sharpe']:.3f}")
    print(f"    Max Consec Wins: {m['max_consec_wins']}   Max Consec Losses: {m['max_consec_losses']}")


def print_backtest_report(header: str, m: dict) -> None:
    """Output ringkas backtest CLI (python backtest.py). Metrik lengkap, teks."""
    pnl_sign = "+" if m["pnl"] >= 0 else ""
    wins = round(m["num_trades"] * m["win_rate"] / 100.0)
    losses = m["num_trades"] - wins
    print("=" * 64)
    print(f"  {header}")
    print("=" * 64)
    print(f"  Net PnL         : {pnl_sign}{m['pnl']:.2f} USDT ({pnl_sign}{m['pnl_pct']:.2f}%)")
    print(f"  Profit Factor   : {_fmt_pf(m['profit_factor'])}")
    print(f"  Max Drawdown    : -{m['max_dd_pct']:.2f}%")
    print(f"  Total Trades    : {m['num_trades']}  (W {wins} / L {losses})")
    print(f"  Win Rate        : {m['win_rate']:.1f}%")
    print(f"  Max Consecutive : {m['max_consec_wins']} win / {m['max_consec_losses']} loss")
    print(f"  Total Fee       : {m.get('total_fee', 0.0):.2f} USDT")
    print(f"  Sharpe          : {m['sharpe']:.3f}")
    print("=" * 64)
