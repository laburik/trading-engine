# =============================================================================
# strategy.py — Trading Strategy (USER ONLY EDITS THIS FILE)
# =============================================================================
#
# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  THIS IS THE ONLY FILE YOU NEED TO MODIFY.                              ║
# ║  All data, execution, logging, and monitoring are handled automatically. ║
# ╚══════════════════════════════════════════════════════════════════════════╝
#
# STRATEGI : Squeeze Momentum Strategy [LazyBear] — Ported from Pine Script v6
# COCOK UNTUK  : DOGEUSDT.P  |  Timeframe: 2h (7200 detik)
#
# LOGIKA:
#   - Hitung Bollinger Bands (BB) dan Keltner Channel (KC)
#   - sqzOn  = BB berada di dalam KC → pasar sedang squeeze (low volatility)
#   - sqzOff = BB keluar dari KC     → volatilitas meledak
#   - val (momentum) = linear regression dari (close - midpoint KC)
#   - LONG  : val memotong 0 dari bawah ke atas (crossover)
#   - SHORT : val memotong 0 dari atas ke bawah (crossunder)
#
# INPUTS: (via data argument dari get_live_data())
#   data["candles"]["2h"]         → List of 2-hour closed candles (WAJIB)
#   data["current"]["2h"]         → Live (open) 2-hour candle, updated every tick
#   data["best_bid"]              → {"price": float, "qty": float}
#   data["best_ask"]              → {"price": float, "qty": float}
#   data["bid_ask_spread"]        → ask - bid (float)
#   data["funding_rate"]          → Current funding rate (float)
#
# CATATAN config.py:
#   Pastikan timeframe "2h" sudah didaftarkan di TIMEFRAMES:
#       TIMEFRAMES = {
#           "2h": (7200, 100),   # 2 jam = 7200 detik, simpan 100 candle
#       }
#
# SIGNAL FORMAT:
#   {"action": "buy" | "sell" | "close" | "hold", "reason": "str"}
# =============================================================================

import logging
import numpy as np
from execution import place_order
from position_manager import get_position, get_pnl_summary

logger = logging.getLogger("strategy")

# =============================================================================
# USER PARAMETERS
# =============================================================================
# Squeeze Momentum parameters (sesuai Pine Script asli)
BB_LENGTH   = 20     # Bollinger Band period
BB_MULT     = 2.0    # Bollinger Band multiplier (std dev)
KC_LENGTH   = 20     # Keltner Channel period
KC_MULT     = 1.5    # Keltner Channel multiplier (ATR/range)
USE_TRUE_RANGE = True  # True = pakai True Range untuk KC; False = pakai High-Low

# Risk management
STOP_LOSS_PCT   = 0.008   # 0.8% stop-loss dari entry
TAKE_PROFIT_PCT = 0.020   # 2.0% take-profit dari entry
MAX_SPREAD_USDT = 0.0005  # Maksimum bid-ask spread yang masih diterima

# Minimum candle history yang dibutuhkan sebelum sinyal dihitung
MIN_CANDLES = KC_LENGTH + 5

# Timeframe yang dipakai strategi ini
TF = "2h"


# =============================================================================
# HELPER: Hitung nilai linear regression (titik terakhir)
# Setara dengan ta.linreg(source, length, 0) di Pine Script
# =============================================================================
def _linreg(series: list[float], length: int) -> float:
    """Return the last value of a linear regression line fitted over `length` bars."""
    if len(series) < length:
        return 0.0
    y = np.array(series[-length:], dtype=float)
    x = np.arange(length, dtype=float)
    # Least-squares fit
    coeffs = np.polyfit(x, y, 1)
    # Value at last index (x = length - 1)
    return float(coeffs[0] * (length - 1) + coeffs[1])


# =============================================================================
# HELPER: Ambil field dari list candle
# Setiap candle adalah dict: {open, high, low, close, volume, timestamp}
# =============================================================================
def _field(candles: list[dict], key: str) -> list[float]:
    return [float(c[key]) for c in candles]


# =============================================================================
# CORE: Hitung Squeeze Momentum value (val) untuk seluruh candle history
# Return: (val_now, val_prev, sqzOn, sqzOff, noSqz)
# =============================================================================
def _compute_sqzmom(candles: list[dict]) -> tuple:
    if len(candles) < MIN_CANDLES:
        return None

    closes = _field(candles, "close")
    highs  = _field(candles, "high")
    lows   = _field(candles, "low")

    n = len(candles)

    # ---------- Bollinger Bands ----------
    bb_src  = np.array(closes, dtype=float)
    bb_sma  = np.mean(bb_src[-BB_LENGTH:])
    bb_std  = np.std(bb_src[-BB_LENGTH:], ddof=1)   # Pine pakai stdev sample
    upper_bb = bb_sma + BB_MULT * bb_std
    lower_bb = bb_sma - BB_MULT * bb_std

    # ---------- Keltner Channel ----------
    kc_src  = np.array(closes, dtype=float)
    kc_ma   = np.mean(kc_src[-KC_LENGTH:])

    # True Range atau High-Low
    if USE_TRUE_RANGE:
        tr_list = []
        for i in range(max(1, n - KC_LENGTH), n):
            prev_close = closes[i - 1] if i > 0 else closes[i]
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - prev_close),
                abs(lows[i]  - prev_close)
            )
            tr_list.append(tr)
        rangema = np.mean(tr_list)
    else:
        hl_list = [highs[i] - lows[i] for i in range(max(0, n - KC_LENGTH), n)]
        rangema = np.mean(hl_list)

    upper_kc = kc_ma + rangema * KC_MULT
    lower_kc = kc_ma - rangema * KC_MULT

    # ---------- Squeeze State ----------
    sqzOn  = (lower_bb > lower_kc) and (upper_bb < upper_kc)
    sqzOff = (lower_bb < lower_kc) and (upper_bb > upper_kc)
    noSqz  = (not sqzOn) and (not sqzOff)

    # ---------- Momentum (val) ----------
    # avgValue = average( average(highestHigh, lowestLow), sma(close, KC_LENGTH) )
    highest_high = max(highs[-KC_LENGTH:])
    lowest_low   = min(lows[-KC_LENGTH:])
    sma_close    = np.mean(closes[-KC_LENGTH:])
    avg_value    = ((highest_high + lowest_low) / 2 + sma_close) / 2

    # source - avgValue → apply linear regression over KC_LENGTH bars
    delta = [c - avg_value for c in closes]
    val_now  = _linreg(delta, KC_LENGTH)

    # val satu bar sebelumnya (butuh satu bar lebih)
    if len(candles) >= MIN_CANDLES + 1:
        closes_p = closes[:-1]
        highs_p  = highs[:-1]
        lows_p   = lows[:-1]
        hh_p = max(highs_p[-KC_LENGTH:])
        ll_p = min(lows_p[-KC_LENGTH:])
        avg_p = ((hh_p + ll_p) / 2 + np.mean(closes_p[-KC_LENGTH:])) / 2
        delta_p = [c - avg_p for c in closes_p]
        val_prev = _linreg(delta_p, KC_LENGTH)
    else:
        val_prev = val_now  # tidak ada crossover pada bar pertama

    return val_now, val_prev, sqzOn, sqzOff, noSqz


# =============================================================================
# 1. GENERATE SIGNAL
# =============================================================================
def generate_signal(data: dict) -> dict:
    """
    Analyse market data using Squeeze Momentum logic and return a trading signal.

    Long  → val crossover  0 (momentum berubah positif)
    Short → val crossunder 0 (momentum berubah negatif)
    """
    # --- Guard: tidak boleh trading selama preload historis ---
    if data.get("is_warmup", False):
        return {"action": "hold", "reason": "Warmup — historical candles loading, no trade allowed"}

    candles = data.get("candles", {}).get(TF, [])
    current = data.get("current", {}).get(TF)
    best_bid = data.get("best_bid", {})
    best_ask = data.get("best_ask", {})
    spread   = data.get("bid_ask_spread", 0.0)
    funding  = data.get("funding_rate", 0.0)

    # --- Guard: butuh cukup candle ---
    if len(candles) < MIN_CANDLES or current is None:
        return {"action": "hold", "reason": f"Not enough {TF} candles ({len(candles)}/{MIN_CANDLES})"}

    # --- Sertakan live candle dalam kalkulasi (sama seperti TradingView) ---
    # val dihitung termasuk harga live saat ini sehingga sinyal muncul
    # di bar yang sedang berjalan, bukan menunggu bar tutup (delay max 2h).
    calc_candles = candles + [current]

    # --- Guard: spread terlalu lebar ---
    if spread > MAX_SPREAD_USDT:
        return {"action": "hold", "reason": f"Spread too wide: {spread:.6f}"}

    bid_price = best_bid.get("price", 0.0)
    ask_price = best_ask.get("price", 0.0)

    # --- Hitung Squeeze Momentum ---
    result = _compute_sqzmom(calc_candles)
    if result is None:
        return {"action": "hold", "reason": "Insufficient data for SQZ MOM"}

    val_now, val_prev, sqzOn, sqzOff, noSqz = result

    # ---------- Tentukan state label (untuk logging) ----------
    if sqzOn:
        sqz_label = "SQZ_ON"
    elif sqzOff:
        sqz_label = "SQZ_OFF"
    else:
        sqz_label = "NO_SQZ"

    logger.debug(
        f"[SQZ MOM] val={val_now:.6f}  prev={val_prev:.6f}  state={sqz_label}"
    )

    # --- Kelola posisi terbuka terlebih dahulu ---
    position = get_position()

    if position["side"] == "long":
        entry = position["entry_price"]
        if bid_price >= entry * (1 + TAKE_PROFIT_PCT):
            return {"action": "close", "reason": f"Take profit (long) | val={val_now:.5f}"}
        if bid_price <= entry * (1 - STOP_LOSS_PCT):
            return {"action": "close", "reason": f"Stop loss (long) | val={val_now:.5f}"}
        # Exit tambahan: momentum berbalik negatif (crossunder 0)
        if val_now < 0 and val_prev >= 0:
            return {"action": "close", "reason": f"Momentum crossunder 0 — exit long | val={val_now:.5f}"}
        return {"action": "hold", "reason": f"Holding long | {sqz_label} | val={val_now:.5f}"}

    if position["side"] == "short":
        entry = position["entry_price"]
        if ask_price <= entry * (1 - TAKE_PROFIT_PCT):
            return {"action": "close", "reason": f"Take profit (short) | val={val_now:.5f}"}
        if ask_price >= entry * (1 + STOP_LOSS_PCT):
            return {"action": "close", "reason": f"Stop loss (short) | val={val_now:.5f}"}
        # Exit tambahan: momentum berbalik positif (crossover 0)
        if val_now > 0 and val_prev <= 0:
            return {"action": "close", "reason": f"Momentum crossover 0 — exit short | val={val_now:.5f}"}
        return {"action": "hold", "reason": f"Holding short | {sqz_label} | val={val_now:.5f}"}

    # --- Tidak ada posisi: cari sinyal entry ---

    # LONG: val crossover 0 (dari negatif/nol ke positif)
    if val_now > 0 and val_prev <= 0:
        if funding > 0.001:
            return {"action": "hold", "reason": f"Funding rate terlalu tinggi untuk long: {funding:.6f}"}
        return {
            "action": "buy",
            "reason": f"SQZ MOM crossover 0 → LONG | {sqz_label} | val={val_now:.5f}"
        }

    # SHORT: val crossunder 0 (dari positif/nol ke negatif)
    if val_now < 0 and val_prev >= 0:
        if funding < -0.001:
            return {"action": "hold", "reason": f"Funding rate menghukum short: {funding:.6f}"}
        return {
            "action": "sell",
            "reason": f"SQZ MOM crossunder 0 → SHORT | {sqz_label} | val={val_now:.5f}"
        }

    return {"action": "hold", "reason": f"No crossover | {sqz_label} | val={val_now:.5f}"}


# =============================================================================
# 2. EXECUTE TRADE
# =============================================================================
def execute_trade(signal: dict):
    """
    Forward signal ke execution layer.
    Jangan tambahkan API call langsung di sini — gunakan place_order saja.
    """
    if signal.get("action") in ("buy", "sell", "close"):
        logger.info(f"[EXECUTE] {signal['action'].upper()} | {signal.get('reason', '')}")
        place_order(signal)


# =============================================================================
# 3. MANAGE POSITION (dipanggil setiap tick)
# =============================================================================
def manage_position(data: dict):
    """
    Dipanggil setiap tick. Digunakan untuk trailing stop, scaling, dll.
    Saat ini tidak dipakai — logika exit sudah ada di generate_signal.
    """
    pass  # Extend as needed


# =============================================================================
# MAIN STRATEGY LOOP (dipanggil oleh main.py setiap tick)
# =============================================================================
def on_tick(data: dict):
    """
    Entry point yang dipanggil main.py setiap tick.
    1. Generate signal
    2. Execute jika actionable
    3. Kelola posisi terbuka
    """
    signal = generate_signal(data)
    execute_trade(signal)
    manage_position(data)
