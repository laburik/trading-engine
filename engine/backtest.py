# =============================================================================
# backtest.py — Strategy Backtest Runner
# =============================================================================
#
# Cara pakai:
#   1. Edit strategy.py seperti biasa
#   2. python backtest.py
#   3. Bandingkan output sinyal dengan TradingView
#
# Script ini langsung import generate_signal() dari strategy.py sehingga
# tidak perlu copy-paste logika apapun. Edit strategi sekali, tes langsung.
#
# Output: daftar sinyal BUY/SELL per bar dengan timestamp (UTC),
#         harga close, nilai val, dan state squeeze.
# =============================================================================

import sys
import time
import requests
import numpy as np
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

# =============================================================================
# CONFIG — sesuaikan dengan strategy.py kamu
# =============================================================================
SYMBOL    = "DOGEUSDT"
TF        = "2h"            # timeframe yang dipakai strategi
INTERVAL  = "120"           # interval Bybit (menit): 2h = "120"
LIMIT     = 1000             # jumlah candle historis yang di-fetch
# =============================================================================


def ts_str(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def fetch_candles():
    """Fetch closed historical candles from Bybit REST API."""
    url = "https://api.bybit.com/v5/market/kline"
    params = {"category": "linear", "symbol": SYMBOL, "interval": INTERVAL, "limit": LIMIT}
    resp = requests.get(url, params=params, timeout=10)
    body = resp.json()
    if body.get("retCode") != 0:
        print("Bybit error: " + body.get("retMsg", ""))
        sys.exit(1)

    rows = sorted(body["result"]["list"], key=lambda r: int(r[0]))

    # Strip currently open candle (period not yet closed)
    interval_sec = int(INTERVAL) * 60
    if rows:
        newest_start_ms = int(rows[-1][0])
        if newest_start_ms + interval_sec * 1000 > int(time.time() * 1000):
            rows = rows[:-1]

    candles = []
    for r in rows:
        candles.append({
            "timeframe":   TF,
            "open_time":   int(r[0]) / 1000.0,
            "open":        float(r[1]),
            "high":        float(r[2]),
            "low":         float(r[3]),
            "close":       float(r[4]),
            "volume":      float(r[5]),
            "buy_volume":  0.0,
            "sell_volume": 0.0,
            "tick_count":  0,
        })
    return candles


def build_data(closed_candles, current_candle):
    """
    Build the data dict that generate_signal() expects.
    Simulates what data_resampler.get_live_data() returns.
    """
    mid = (current_candle["close"] + current_candle["open"]) / 2
    return {
        "candles":            {TF: closed_candles},
        "current":            {TF: current_candle},
        "best_bid":           {"price": current_candle["close"] * 0.9999, "qty": 1.0},
        "best_ask":           {"price": current_candle["close"] * 1.0001, "qty": 1.0},
        "bid_ask_spread":     current_candle["close"] * 0.0002,
        "orderbook_imbalance": 0.0,
        "volume_delta":       0.0,
        "funding_rate":       0.0001,
        "latest_tick":        {"price": current_candle["close"], "qty": 1.0, "side": "Buy",
                               "timestamp": current_candle["open_time"]},
        "is_warmup":          False,   # selalu False saat backtest
    }


def mock_position(side="none"):
    """Return a mock position dict."""
    return {"side": side, "entry_price": 0.0, "qty": 0.0}


def run_backtest(candles):
    """
    Simulate the bot running bar by bar.
    For each bar i, treat candles[0..i-1] as closed history
    and candles[i] as the current live bar.
    """

    # --- Mock dependencies so strategy.py can be imported standalone ---
    mock_place_order   = MagicMock()
    mock_get_position  = MagicMock(return_value={"side": "none", "entry_price": 0.0, "qty": 0.0})
    mock_get_pnl       = MagicMock(return_value={})

    import sys
    sys.modules.setdefault("execution", MagicMock())
    sys.modules.setdefault("position_manager", MagicMock())

    import execution
    import position_manager

    execution.place_order         = mock_place_order
    position_manager.get_position = mock_get_position
    position_manager.get_pnl_summary = mock_get_pnl

    import strategy as strat

    if hasattr(strat, "bot_state"):
        strat.bot_state = {"in_position": False, "entry_price": 0.0, "entry_time": 0.0}

    MIN = getattr(strat, "MIN_CANDLES", 50)

    signals = []

    print("")
    print("%-22s  %10s  %8s  Signal" % ("Bar (UTC)", "Close", "Action"))
    print("-" * 60)

    for i in range(MIN, len(candles)):
        closed  = candles[:i]
        current = candles[i]

        data = build_data(closed, current)
        mock_get_position.return_value = {"side": "none", "entry_price": 0.0, "qty": 0.0}

        try:
            signal = strat.generate_signal(data)
        except Exception as e:
            signal = {"action": "error", "reason": str(e)}

        action = signal.get("action", "hold")
        t      = ts_str(int(current["open_time"] * 1000))
        close  = current["close"]

        label = ""
        if action == "buy":
            label = ">>> [BUY]"
            signals.append({"bar": t, "action": "BUY", "price": close})
        elif action == "sell":
            label = ">>> [SELL]"
            signals.append({"bar": t, "action": "SELL", "price": close})
        elif action == "close":
            label = "    [CLOSE]"
            signals.append({"bar": t, "action": "CLOSE", "price": close})

        if label or (i == len(candles) - 1):
            print("%-22s  %10.5f  %8s  %s" % (t, close, action.upper(), label))

    print("")
    print("=" * 60)
    print("TOTAL SIGNALS: %d" % len(signals))
    print("")
    for s in signals:
        print("  %-6s @ %.5f  [%s]" % (s["action"], s["price"], s["bar"]))

    print("")
    print("Cara perbandingan di TradingView:")
    print("  - Buka DOGEUSDT %s, aktifkan strategy" % TF.upper())
    print("  - Timestamp UTC+7 = jam di atas + 7 jam")
    print("  - Cocokkan tanda panah BUY/SELL dengan output di atas")
    print("")

def main():
    print("Backtest: %s | %s | last %d closed candles" % (SYMBOL, TF.upper(), LIMIT))
    print("Fetching data from Bybit...")
    candles = fetch_candles()
    print("Got %d closed candles (%s -> %s)" % (
        len(candles),
        ts_str(int(candles[0]["open_time"] * 1000)),
        ts_str(int(candles[-1]["open_time"] * 1000))
    ))
    run_backtest(candles)


if __name__ == "__main__":
    main()
