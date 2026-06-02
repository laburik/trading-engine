# =============================================================================
# strategy_example.py — CONTOH STRATEGI REFERENSI (EMA Trend + RSI)
# =============================================================================
# Tujuan file ini: TEMPLATE yang bisa kamu CONTEK saat menulis strategi sendiri.
# Sengaja ditulis dengan banyak komentar + tanpa library tambahan (cukup Python
# bawaan) supaya gampang dibaca dan dimodifikasi. Bukan strategi "sakti" untuk
# cari profit — fokusnya menunjukkan POLA yang benar.
#
# Cara pakai:
#   1. Pelajari struktur di bawah (guard → cek posisi → exit → entry).
#   2. Copy file ini jadi nama lain, mis. `strategy_punyaku.py`.
#   3. Arahkan bot ke sana lewat `STRATEGY_FILE = "strategy_punyaku"` di config.py.
#   4. `python user/main.py` akan otomatis memvalidasi lewat pre-flight check.
#
# KONTRAK WAJIB (yang dicek engine):
#   - Ada fungsi `generate_signal(data) -> dict`.
#   - Return dict dengan key "action" ∈ {"buy","sell","close","hold"}.
#   - Saat `data["is_warmup"]` True → WAJIB return "hold" (jangan entry saat
#     engine masih memuat candle historis).
#   - JANGAN panggil time.sleep(), sys.exit(), atau HTTP request di dalam sini —
#     fungsi ini dipanggil dari event loop bot, blocking = bot membeku.
#
# LOGIKA STRATEGI (contoh):
#   - Trend ditentukan EMA cepat vs EMA lambat (EMA_FAST > EMA_SLOW = uptrend).
#   - RSI dipakai sebagai filter momentum supaya tidak beli saat overbought /
#     jual saat oversold.
#   - Entry LONG  : uptrend  + RSI di zona sehat (50..overbought).
#   - Entry SHORT : downtrend + RSI di zona sehat (oversold..50).
#   - Exit        : kena TP% / SL% (pakai bid utk long, ask utk short), ATAU
#                   trend berbalik arah.
# =============================================================================
from __future__ import annotations

import logging
from typing import Any

# position_manager = sumber kebenaran posisi saat ini (side + entry_price).
# Saat pre-flight check, engine memasang versi mock-nya otomatis, jadi import ini
# aman walau bot belum jalan.
import position_manager

logger = logging.getLogger("strategy_example")

# ── Parameter strategi (ubah sesuai selera) ──────────────────────────────────
# Semua persen ditulis dalam desimal: 0.01 = 1%.
TF: str               = "15m"    # timeframe candle yang dipakai (harus ada di TIMEFRAMES config)
EMA_FAST: int         = 9        # periode EMA cepat
EMA_SLOW: int         = 21       # periode EMA lambat
RSI_PERIOD: int       = 14       # periode RSI
RSI_OVERBOUGHT: float = 70.0     # di atas ini = jangan beli (sudah kemahalan)
RSI_OVERSOLD: float   = 30.0     # di bawah ini = jangan jual (sudah kemurahan)
TREND_GAP: float      = 0.0015   # 0.15% — jarak minimum EMA agar trend dianggap valid (filter pasar flat)
RSI_LONG_MIN: float   = 55.0     # entry LONG butuh RSI di atas ini (momentum naik benar-benar ada)
RSI_SHORT_MAX: float  = 45.0     # entry SHORT butuh RSI di bawah ini (momentum turun benar-benar ada)
TP_PCT: float         = 0.012    # 1.2% take profit
SL_PCT: float         = 0.008    # 0.8% stop loss

# Butuh minimal sekian candle sebelum indikator stabil.
MIN_CANDLES: int = EMA_SLOW + 5


# =============================================================================
# HELPER INDIKATOR (murni Python — tidak perlu numpy/pandas)
# =============================================================================
def _ema(values: list[float], period: int) -> float:
    """
    Exponential Moving Average. Di-seed dengan SMA dari `period` nilai pertama,
    lalu diperhalus untuk sisanya. Return 0.0 kalau data kurang.
    """
    if len(values) < period:
        return 0.0
    k: float = 2.0 / (period + 1)
    ema: float = sum(values[:period]) / period   # seed = SMA awal
    for v in values[period:]:
        ema = v * k + ema * (1.0 - k)
    return ema


def _rsi(values: list[float], period: int) -> float:
    """
    Relative Strength Index (0..100) memakai rata-rata gain/loss sederhana atas
    `period` perubahan terakhir. Return 50.0 (netral) kalau data kurang.
    """
    if len(values) < period + 1:
        return 50.0
    recent: list[float] = values[-(period + 1):]
    gains: float = 0.0
    losses: float = 0.0
    for i in range(1, len(recent)):
        change: float = recent[i] - recent[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change   # losses dijadikan positif
    avg_gain: float = gains / period
    avg_loss: float = losses / period
    if avg_loss == 0.0:
        # Tidak ada penurunan sama sekali → momentum naik penuh.
        return 100.0 if avg_gain > 0.0 else 50.0
    rs: float = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# =============================================================================
# ENTRY POINT — dipanggil engine tiap tick/candle
# =============================================================================
def generate_signal(data: dict) -> dict[str, Any]:
    """
    Analisa snapshot pasar dan kembalikan satu sinyal.

    Returns:
        {"action": "buy"|"sell"|"close"|"hold", "reason": "..."}
    """
    # ── Guard 1: Warmup — engine masih memuat candle historis ─────────────────
    if data.get("is_warmup", True):
        return {"action": "hold", "reason": "Warmup data historis"}

    # ── Guard 2: Candle timeframe kita harus ada & cukup banyak ───────────────
    candles: list[dict] = data.get("candles", {}).get(TF, [])
    if len(candles) < MIN_CANDLES:
        return {"action": "hold", "reason": f"Tunggu {MIN_CANDLES} candle {TF} (sekarang {len(candles)})"}

    # ── Guard 3: Orderbook (best bid/ask) harus siap ──────────────────────────
    bid: float = float(data.get("best_bid", {}).get("price", 0.0))
    ask: float = float(data.get("best_ask", {}).get("price", 0.0))
    if bid <= 0.0 or ask <= 0.0:
        return {"action": "hold", "reason": "Orderbook belum siap"}

    # ── Hitung indikator dari harga close ─────────────────────────────────────
    closes: list[float] = [float(c.get("close", 0.0)) for c in candles]
    if any(c <= 0.0 for c in closes):
        return {"action": "hold", "reason": "Ada candle close invalid"}

    ema_fast: float = _ema(closes, EMA_FAST)
    ema_slow: float = _ema(closes, EMA_SLOW)
    rsi: float      = _rsi(closes, RSI_PERIOD)
    if ema_fast == 0.0 or ema_slow == 0.0:
        return {"action": "hold", "reason": "EMA belum bisa dihitung"}

    # Tentukan arah trend dengan ambang TREND_GAP (saring pasar flat).
    uptrend: bool   = ema_fast > ema_slow * (1.0 + TREND_GAP)
    downtrend: bool = ema_fast < ema_slow * (1.0 - TREND_GAP)

    # ── Cek posisi sekarang (sumber kebenaran = position_manager) ─────────────
    pos: dict[str, object] = position_manager.get_position()
    side: str = str(pos.get("side", "none"))

    # =========================================================================
    # CABANG A — SUDAH PUNYA POSISI → evaluasi EXIT (TP / SL / trend balik)
    # =========================================================================
    if side == "long":
        entry: float = float(pos.get("entry_price", 0.0))  # type: ignore[arg-type]
        if entry <= 0.0:
            return {"action": "hold", "reason": "entry_price=0, tunggu sync"}
        # Exit long = jual @ bid
        if bid >= entry * (1.0 + TP_PCT):
            return {"action": "close", "reason": f"LONG TP +{TP_PCT*100:.2f}%"}
        if bid <= entry * (1.0 - SL_PCT):
            return {"action": "close", "reason": f"LONG SL -{SL_PCT*100:.2f}%"}
        if downtrend:
            return {"action": "close", "reason": "LONG exit — trend berbalik turun"}
        return {"action": "hold", "reason": f"LONG hold | RSI={rsi:.1f}"}

    if side == "short":
        entry = float(pos.get("entry_price", 0.0))  # type: ignore[arg-type]
        if entry <= 0.0:
            return {"action": "hold", "reason": "entry_price=0, tunggu sync"}
        # Exit short = beli @ ask
        if ask <= entry * (1.0 - TP_PCT):
            return {"action": "close", "reason": f"SHORT TP +{TP_PCT*100:.2f}%"}
        if ask >= entry * (1.0 + SL_PCT):
            return {"action": "close", "reason": f"SHORT SL -{SL_PCT*100:.2f}%"}
        if uptrend:
            return {"action": "close", "reason": "SHORT exit — trend berbalik naik"}
        return {"action": "hold", "reason": f"SHORT hold | RSI={rsi:.1f}"}

    # =========================================================================
    # CABANG B — BELUM ADA POSISI → evaluasi ENTRY
    # =========================================================================
    # LONG: uptrend + momentum naik kuat (RSI di zona RSI_LONG_MIN..overbought).
    if uptrend and RSI_LONG_MIN < rsi < RSI_OVERBOUGHT:
        return {"action": "buy", "reason": f"Uptrend + RSI sehat ({rsi:.1f})"}

    # SHORT: downtrend + momentum turun kuat (RSI di zona oversold..RSI_SHORT_MAX).
    if downtrend and RSI_OVERSOLD < rsi < RSI_SHORT_MAX:
        return {"action": "sell", "reason": f"Downtrend + RSI sehat ({rsi:.1f})"}

    # Selain itu: tahan diri (tidak ada setup yang jelas).
    return {"action": "hold", "reason": f"No setup | EMA{EMA_FAST}={ema_fast:.5f} EMA{EMA_SLOW}={ema_slow:.5f} RSI={rsi:.1f}"}
