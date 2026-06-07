# =============================================================================
# strategy.py — TREND BREAKOUT (Donchian + EMA filter)  [LONG & SHORT]
# =============================================================================
# Didesain untuk modal kecil (10 USDT, LEVERAGE 3x, ORDER_SIZE 10 → notional 30)
# dengan target: PROFIT positif sambil menjaga max drawdown < 65%.
#
# FILOSOFI (kenapa breakout trend-following untuk XRP):
#   Riset data XRPUSDT (Jan–Jun 2026) menunjukkan: tren turun kuat lalu ranging.
#   Strategi yang menang di sini harus (1) bisa SHORT, (2) ikut tren saja,
#   (3) DIAM saat pasar sideways (filter tren) supaya tidak digerogoti fee.
#
# LOGIKA:
#   1. Filter tren  : EMA cepat vs EMA lambat dengan jarak minimum (TREND_GAP).
#                     Hanya entry searah tren. Saat EMA rapat (sideways) → diam.
#   2. Entry        : breakout Donchian — harga tembus high/low N-bar terakhir
#                     SEARAH tren (momentum konfirmasi, bukan menebak balik arah).
#   3. Exit         : (a) hard STOP LOSS  → batasi drawdown,
#                     (b) hard TAKE PROFIT → kunci profit besar,
#                     (c) channel exit     → harga balik tembus kanal pendek
#                        (turtle-style trailing tanpa perlu simpan state), ATAU
#                     (d) tren berbalik arah.
#
# KONTRAK ENGINE (sama untuk live & backtest):
#   - generate_signal(data) -> {"action": "buy"|"sell"|"close"|"hold", "reason": ...}
#   - Saat data["is_warmup"] True → WAJIB "hold".
#   - Posisi saat ini dibaca dari position_manager.get_position() (sumber kebenaran).
#   - Semua persen ditulis desimal (0.01 = 1%).
#
# Parameter di-expose sebagai variabel modul (gaya input MQL5) → bisa dituning
# lewat tuning_config.py / hypertune.py tanpa ubah kode.
# =============================================================================
from __future__ import annotations

import logging
from typing import Any

import position_manager

logger = logging.getLogger("strategy_breakout")

# ── Parameter strategi (semua persen dalam desimal) ──────────────────────────
TF: str               = "15m"     # timeframe candle (di-override backtest_config saat backtest)

# Nilai default di bawah = hasil tuning untuk MEMAKSIMALKAN PROFIT di rezim
# pasar sekarang (window 2026-01-01 → sekarang, TF 15m). Profil pada window itu:
#   Net PnL ≈ +166% (modal 10 → ~26 USDT), max drawdown ≈ 22% (jauh di bawah
#   batas 65%), profit factor ≈ 1.44, Sharpe ≈ 1.8, ~175 trade.
# CATATAN: ini setelan AGRESIF yang dioptimasi ke rezim terkini (tren turun +
# volatil). Saat rezim berubah jadi sideways ketat, profit bisa turun → lakukan
# RE-TUNING via tuning_config.py + hypertune.py (lihat catatan di bawah file).
EMA_FAST: int         = 10        # EMA cepat (arah tren jangka pendek)
EMA_SLOW: int         = 30        # EMA lambat (arah tren jangka menengah)
TREND_GAP: float      = 0.003     # jarak minimum EMA (fraksi harga) agar tren valid → filter sideways

BREAKOUT_N: int       = 36        # entry: tembus high/low N-bar terakhir (Donchian masuk, 36×15m = 9 jam)
EXIT_N: int           = 20        # exit channel: balik tembus low/high M-bar (turtle exit, M < N)

SL_PCT: float         = 0.022     # 2.2% hard stop loss (pembatas drawdown)
TP_PCT: float         = 0.040     # 4.0% hard take profit (kunci profit cepat di pasar volatil)

# Butuh minimal sekian candle tertutup sebelum semua indikator stabil.
MIN_CANDLES: int = max(EMA_SLOW, BREAKOUT_N, EXIT_N) + 5


# =============================================================================
# HELPER INDIKATOR (murni Python — tanpa numpy/pandas, ringan di event loop)
# =============================================================================
def _ema(values: list[float], period: int) -> float:
    """Exponential Moving Average. Seed = SMA `period` nilai pertama. 0.0 kalau kurang data."""
    if len(values) < period:
        return 0.0
    k: float = 2.0 / (period + 1)
    ema: float = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1.0 - k)
    return ema


# =============================================================================
# ENTRY POINT — dipanggil engine tiap tick/candle
# =============================================================================
def generate_signal(data: dict) -> dict[str, Any]:
    # ── Guard 1: warmup (engine masih preload candle historis) ───────────────
    if data.get("is_warmup", True):
        return {"action": "hold", "reason": "Warmup data historis"}

    # ── Guard 2: candle TF cukup ──────────────────────────────────────────────
    candles: list[dict] = data.get("candles", {}).get(TF, [])
    if len(candles) < MIN_CANDLES:
        return {"action": "hold", "reason": f"Tunggu {MIN_CANDLES} candle {TF} (now {len(candles)})"}

    # ── Guard 3: orderbook siap ───────────────────────────────────────────────
    bid: float = float(data.get("best_bid", {}).get("price", 0.0))
    ask: float = float(data.get("best_ask", {}).get("price", 0.0))
    if bid <= 0.0 or ask <= 0.0:
        return {"action": "hold", "reason": "Orderbook belum siap"}

    # ── Ekstrak OHLC ──────────────────────────────────────────────────────────
    closes: list[float] = [float(c.get("close", 0.0)) for c in candles]
    highs:  list[float] = [float(c.get("high", 0.0))  for c in candles]
    lows:   list[float] = [float(c.get("low", 0.0))   for c in candles]
    if any(x <= 0.0 for x in closes):
        return {"action": "hold", "reason": "Candle close invalid"}

    last_close: float = closes[-1]

    # ── Filter tren (EMA cepat vs lambat dengan jarak minimum) ────────────────
    ema_fast: float = _ema(closes, EMA_FAST)
    ema_slow: float = _ema(closes, EMA_SLOW)
    if ema_fast == 0.0 or ema_slow == 0.0:
        return {"action": "hold", "reason": "EMA belum bisa dihitung"}
    uptrend:   bool = ema_fast > ema_slow * (1.0 + TREND_GAP)
    downtrend: bool = ema_fast < ema_slow * (1.0 - TREND_GAP)

    # ── Kanal Donchian (entry & exit) dari bar SEBELUM bar terakhir ───────────
    # Pakai bar [-(N+1):-1] supaya breakout = harga terakhir menembus kanal lama,
    # bukan membandingkan bar dengan dirinya sendiri (cegah sinyal palsu).
    hi_entry: float = max(highs[-(BREAKOUT_N + 1):-1])
    lo_entry: float = min(lows[-(BREAKOUT_N + 1):-1])
    hi_exit:  float = max(highs[-(EXIT_N + 1):-1])
    lo_exit:  float = min(lows[-(EXIT_N + 1):-1])

    # ── Posisi sekarang (sumber kebenaran) ────────────────────────────────────
    pos: dict[str, object] = position_manager.get_position()
    side: str = str(pos.get("side", "none"))

    # =========================================================================
    # CABANG A — PUNYA POSISI → evaluasi EXIT
    # =========================================================================
    if side == "long":
        entry: float = float(pos.get("entry_price", 0.0))  # type: ignore[arg-type]
        if entry <= 0.0:
            return {"action": "hold", "reason": "entry_price=0, tunggu sync"}
        if bid <= entry * (1.0 - SL_PCT):
            return {"action": "close", "reason": f"LONG SL -{SL_PCT*100:.1f}%"}
        if bid >= entry * (1.0 + TP_PCT):
            return {"action": "close", "reason": f"LONG TP +{TP_PCT*100:.1f}%"}
        if last_close < lo_exit:
            return {"action": "close", "reason": f"LONG channel exit (close<{lo_exit:.5f})"}
        if downtrend:
            return {"action": "close", "reason": "LONG exit — tren berbalik turun"}
        return {"action": "hold", "reason": f"LONG hold | close={last_close:.5f} entry={entry:.5f}"}

    if side == "short":
        entry = float(pos.get("entry_price", 0.0))  # type: ignore[arg-type]
        if entry <= 0.0:
            return {"action": "hold", "reason": "entry_price=0, tunggu sync"}
        if ask >= entry * (1.0 + SL_PCT):
            return {"action": "close", "reason": f"SHORT SL -{SL_PCT*100:.1f}%"}
        if ask <= entry * (1.0 - TP_PCT):
            return {"action": "close", "reason": f"SHORT TP +{TP_PCT*100:.1f}%"}
        if last_close > hi_exit:
            return {"action": "close", "reason": f"SHORT channel exit (close>{hi_exit:.5f})"}
        if uptrend:
            return {"action": "close", "reason": "SHORT exit — tren berbalik naik"}
        return {"action": "hold", "reason": f"SHORT hold | close={last_close:.5f} entry={entry:.5f}"}

    # =========================================================================
    # CABANG B — FLAT → evaluasi ENTRY (breakout searah tren)
    # =========================================================================
    if uptrend and last_close > hi_entry:
        return {"action": "buy",
                "reason": f"Breakout UP searah tren (close={last_close:.5f} > {hi_entry:.5f})"}
    if downtrend and last_close < lo_entry:
        return {"action": "sell",
                "reason": f"Breakout DOWN searah tren (close={last_close:.5f} < {lo_entry:.5f})"}

    return {"action": "hold", "reason": f"No setup | EMA{EMA_FAST}={ema_fast:.5f} EMA{EMA_SLOW}={ema_slow:.5f}"}
