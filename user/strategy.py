# =============================================================================
# strategy.py — TEST STRATEGY (MA Crossover)
# =============================================================================
# TUJUAN: Smoke test untuk validasi end-to-end bot flow di mode demo.
# BUKAN strategi untuk live trading — sengaja sederhana supaya:
#   1. Sering trigger sinyal (5-15 trade per jam di market normal)
#   2. Exit cepat (TP 0.15% / SL 0.10%) → full lifecycle tervalidasi
#   3. Behavior predictable & repeatable
#
# Logika:
#   - Pakai candle 1m (cukup cepat trigger di demo)
#   - MA pendek 3 vs MA panjang 10
#   - Entry: cross dengan threshold 0.02% (filter noise)
#   - Exit: TP 0.15% ATAU SL 0.10% ATAU reverse cross
# =============================================================================
from __future__ import annotations

import logging
from typing import Any

import position_manager

logger = logging.getLogger("strategy_test")

# ── Parameter strategi (semua persen dalam desimal) ─────────────────────────
TF: str               = "1m"     # timeframe untuk MA
MA_SHORT: int         = 3        # MA pendek
MA_LONG: int          = 10       # MA panjang
CROSS_THRESHOLD: float = 0.0002  # 0.02% — filter noise (gak entry tiap tick)
TP_PCT: float         = 0.0015   # 0.15% take profit
SL_PCT: float         = 0.0010   # 0.10% stop loss


def _moving_average(values: list[float], n: int) -> float:
    """Simple MA dari N nilai terakhir."""
    if len(values) < n:
        return 0.0
    return sum(values[-n:]) / n


def generate_signal(data: dict) -> dict[str, Any]:
    """
    Entry point yang dipanggil engine setiap tick.

    Returns:
        {"action": "buy"|"sell"|"close"|"hold", "reason": "..."}
    """
    # ── Guard 1: Warmup (engine masih preload historis) ─────────────────────
    if data.get("is_warmup", True):
        return {"action": "hold", "reason": "Warmup data historis"}

    # ── Guard 2: Pastikan candle TF kita ada & cukup ─────────────────────────
    candles = data.get("candles", {}).get(TF, [])
    if len(candles) < MA_LONG:
        return {"action": "hold", "reason": f"Tunggu {MA_LONG} candle (now {len(candles)})"}

    # ── Guard 3: Pastikan best_bid & best_ask ada (orderbook ready) ──────────
    bid = data.get("best_bid", {}).get("price", 0.0)
    ask = data.get("best_ask", {}).get("price", 0.0)
    if bid <= 0 or ask <= 0:
        return {"action": "hold", "reason": "Orderbook belum siap"}

    # ── Ekstrak harga close untuk hitung MA ──────────────────────────────────
    closes = [float(c.get("close", 0)) for c in candles]
    if any(c <= 0 for c in closes):
        return {"action": "hold", "reason": "Candle close invalid"}

    ma_short = _moving_average(closes, MA_SHORT)
    ma_long  = _moving_average(closes, MA_LONG)
    if ma_short == 0 or ma_long == 0:
        return {"action": "hold", "reason": "MA belum bisa dihitung"}

    # ── Posisi sekarang via position_manager (sumber kebenaran) ──────────────
    pos: dict[str, object] = position_manager.get_position()
    side: str = str(pos.get("side", "none"))

    # =========================================================================
    # CABANG A — sudah punya posisi: evaluasi EXIT (TP / SL / reverse)
    # =========================================================================
    if side == "long":
        entry = float(pos.get("entry_price", 0.0))  # type: ignore[arg-type]
        if entry <= 0:
            # State aneh — biar sync loop yang benerin
            return {"action": "hold", "reason": "entry_price=0, tunggu sync"}

        # Hit TP (pakai bid karena exit long = jual @ bid)
        if bid >= entry * (1 + TP_PCT):
            return {"action": "close", "reason": f"LONG TP +{TP_PCT*100:.2f}% (bid {bid:.5f} >= {entry*(1+TP_PCT):.5f})"}
        # Hit SL
        if bid <= entry * (1 - SL_PCT):
            return {"action": "close", "reason": f"LONG SL -{SL_PCT*100:.2f}% (bid {bid:.5f} <= {entry*(1-SL_PCT):.5f})"}
        # Reverse signal (MA cross down)
        if ma_short < ma_long * (1 - CROSS_THRESHOLD):
            return {"action": "close", "reason": "LONG exit by reverse MA cross"}

        return {"action": "hold", "reason": f"LONG hold | bid={bid:.5f} entry={entry:.5f}"}

    if side == "short":
        entry = float(pos.get("entry_price", 0.0))  # type: ignore[arg-type]
        if entry <= 0:
            return {"action": "hold", "reason": "entry_price=0, tunggu sync"}

        # SHORT TP saat harga TURUN (pakai ask karena exit short = beli @ ask)
        if ask <= entry * (1 - TP_PCT):
            return {"action": "close", "reason": f"SHORT TP +{TP_PCT*100:.2f}% (ask {ask:.5f} <= {entry*(1-TP_PCT):.5f})"}
        # SHORT SL saat harga NAIK
        if ask >= entry * (1 + SL_PCT):
            return {"action": "close", "reason": f"SHORT SL -{SL_PCT*100:.2f}% (ask {ask:.5f} >= {entry*(1+SL_PCT):.5f})"}
        # Reverse signal (MA cross up)
        if ma_short > ma_long * (1 + CROSS_THRESHOLD):
            return {"action": "close", "reason": "SHORT exit by reverse MA cross"}

        return {"action": "hold", "reason": f"SHORT hold | ask={ask:.5f} entry={entry:.5f}"}

    # =========================================================================
    # CABANG B — belum ada posisi: evaluasi ENTRY (MA cross)
    # =========================================================================
    # Cross up dengan threshold → BUY
    if ma_short > ma_long * (1 + CROSS_THRESHOLD):
        return {
            "action": "buy",
            "reason": f"MA cross UP (short={ma_short:.5f} > long={ma_long:.5f})",
        }
    # Cross down dengan threshold → SELL (short)
    if ma_short < ma_long * (1 - CROSS_THRESHOLD):
        return {
            "action": "sell",
            "reason": f"MA cross DOWN (short={ma_short:.5f} < long={ma_long:.5f})",
        }

    return {"action": "hold", "reason": f"MA flat (short={ma_short:.5f} ≈ long={ma_long:.5f})"}
