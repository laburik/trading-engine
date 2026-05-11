# -*- coding: utf-8 -*-
"""
test_strategy.py -- Standalone SQZ Momentum Diagnostic
=======================================================
Fetch 100 candle 2H DOGEUSDT dari Bybit, jalankan kalkulasi SQZ MOM
persis seperti di strategy.py, dan print hasilnya.

Run: python test_strategy.py
"""

import requests
import numpy as np
from datetime import datetime, timezone

# -- Parameters (sama persis dengan strategy.py) --------------------------------
BB_LENGTH = 20
BB_MULT   = 2.0
KC_LENGTH = 20
KC_MULT   = 1.5
USE_TRUE_RANGE = True

SYMBOL   = "DOGEUSDT"
INTERVAL = "120"   # 2H
LIMIT    = 100
# -------------------------------------------------------------------------------


def fetch_candles():
    url = "https://api.bybit.com/v5/market/kline"
    params = {"category": "linear", "symbol": SYMBOL, "interval": INTERVAL, "limit": LIMIT}
    resp = requests.get(url, params=params, timeout=10)
    data = resp.json()
    if data.get("retCode") != 0:
        raise RuntimeError("Bybit error: " + data.get("retMsg", ""))
    rows = sorted(data["result"]["list"], key=lambda r: int(r[0]))
    candles = []
    for r in rows:
        candles.append({
            "ts":    int(r[0]),
            "open":  float(r[1]),
            "high":  float(r[2]),
            "low":   float(r[3]),
            "close": float(r[4]),
        })
    return candles


def ts_str(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def linreg(series, length):
    y = np.array(series[-length:], dtype=float)
    x = np.arange(length, dtype=float)
    c = np.polyfit(x, y, 1)
    return float(c[0] * (length - 1) + c[1])


def compute_val(candles):
    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    n = len(candles)

    # BB
    bb     = np.array(closes[-BB_LENGTH:])
    bb_sma = np.mean(bb)
    bb_std = np.std(bb, ddof=1)
    upper_bb = bb_sma + BB_MULT * bb_std
    lower_bb = bb_sma - BB_MULT * bb_std

    # KC
    kc_ma = np.mean(closes[-KC_LENGTH:])
    if USE_TRUE_RANGE:
        tr_list = []
        for i in range(max(1, n - KC_LENGTH), n):
            pc = closes[i - 1]
            tr = max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc))
            tr_list.append(tr)
        rangema = np.mean(tr_list)
    else:
        rangema = np.mean([highs[i] - lows[i] for i in range(max(0, n - KC_LENGTH), n)])
    upper_kc = kc_ma + rangema * KC_MULT
    lower_kc = kc_ma - rangema * KC_MULT

    sqzOn  = (lower_bb > lower_kc) and (upper_bb < upper_kc)
    sqzOff = (lower_bb < lower_kc) and (upper_bb > upper_kc)
    noSqz  = not sqzOn and not sqzOff

    # Momentum val
    hh  = max(highs[-KC_LENGTH:])
    ll  = min(lows[-KC_LENGTH:])
    avg = ((hh + ll) / 2 + np.mean(closes[-KC_LENGTH:])) / 2
    delta = [c - avg for c in closes]
    val   = linreg(delta, KC_LENGTH)

    sqz_label = "SQZ_ON " if sqzOn else ("SQZ_OFF" if sqzOff else "NO_SQZ ")
    return val, sqz_label


def main():
    print("")
    print("Fetching %d x %s-min candles for %s..." % (LIMIT, INTERVAL, SYMBOL))
    candles = fetch_candles()
    print("Got %d candles. Range: %s -> %s" % (len(candles), ts_str(candles[0]["ts"]), ts_str(candles[-1]["ts"])))
    print("")

    MIN = KC_LENGTH + 5   # sama dengan strategy.py

    prev_val = None
    signals  = []

    print("%-20s  %10s  %10s  %9s  %s" % ("Time (UTC)", "Close", "Val", "State", "Signal"))
    print("-" * 75)

    for i in range(MIN, len(candles) + 1):
        subset = candles[:i]
        val, sqz = compute_val(subset)
        bar   = subset[-1]
        t     = ts_str(bar["ts"])
        close = bar["close"]

        signal = ""
        if prev_val is not None:
            if val > 0 and prev_val <= 0:
                signal = "[BUY]  crossover 0"
                signals.append((t, close, val, "BUY"))
            elif val < 0 and prev_val >= 0:
                signal = "[SELL] crossunder 0"
                signals.append((t, close, val, "SELL"))

        print("%-20s  %10.5f  %10.6f  %9s  %s" % (t, close, val, sqz, signal))
        prev_val = val

    print("")
    print("=" * 75)
    print("TOTAL CROSSOVER SIGNALS: %d" % len(signals))
    for s in signals:
        print("  %-6s @ %.5f  val=%.6f  [%s]" % (s[3], s[1], s[2], s[0]))

    print("")
    print("-- DIAGNOSIS --")
    if signals:
        print("OK: Kalkulasi SQZ MOM benar -- sinyal terdeteksi dari data historis.")
        print("    Jika bot tidak entry, masalah kemungkinan di engine (lihat bawah).")
        print("")
        print("    Cek 1: Apakah last signal ada di bar terakhir?")
        last_sig = signals[-1]
        last_bar_ts = ts_str(candles[-1]["ts"])
        if last_sig[0] == last_bar_ts:
            print("    >> YA -- sinyal ada di bar terbaru. Bot harusnya sudah entry.")
            print("       Kemungkinan: is_warmup masih True, atau current candle = None.")
        else:
            print("    >> Sinyal terakhir di: %s" % last_sig[0])
            print("    >> Bar terbaru di    : %s" % last_bar_ts)
            print("    >> Bot tidak entry karena crossover sudah lewat.")
            print("       Solusi: tambahkan current live candle ke kalkulasi.")
    else:
        print("INFO: Tidak ada crossover dalam %d candle terakhir." % LIMIT)
        print("      Artinya strategi memang belum trigger -- bukan bug.")
    print("")


if __name__ == "__main__":
    main()
