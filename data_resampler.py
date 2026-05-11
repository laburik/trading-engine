from __future__ import annotations

import time
import asyncio
import logging
from collections import deque
from typing import Optional
from config import TIMEFRAMES

# Import shared buffers from data_stream
import data_stream
from ft_types import (
    BidAskLevel,
    Candle,
    MarketDataSnapshot,
    TickData,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [RESAMPLER] %(levelname)s: %(message)s"
)
logger = logging.getLogger("data_resampler")

# =============================================================================
# WARMUP FLAG
# True  = historical candles still loading → strategy must not trade
# False = preload done, live tick data is active
# =============================================================================
is_warmup: bool = True

# =============================================================================
# AUTO-BUILT CANDLE STORAGE (from config.TIMEFRAMES)
# =============================================================================
# candle_buffers["1m"] = deque(maxlen=200)  ← built automatically
candle_buffers: dict[str, deque[Candle]] = {
    tf: deque(maxlen=size)
    for tf, (_, size) in TIMEFRAMES.items()
}

# Current open (in-progress) candle per timeframe
_current_candle: dict[str, Optional[Candle]] = {
    tf: None for tf in TIMEFRAMES
}

# Interval in seconds per timeframe
_INTERVALS: dict[str, int] = {
    tf: interval for tf, (interval, _) in TIMEFRAMES.items()
}

logger.info(f"Resampler configured for timeframes: {list(TIMEFRAMES.keys())}")


def _new_candle(tf: str, price: float, qty: float, ts: float) -> Candle:
    """Create a fresh candle for the given timeframe."""
    bucket: float = _bucket_start(tf, ts)
    return {
        "timeframe": tf,
        "open_time": bucket,
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "volume": qty,
        "buy_volume": 0.0,
        "sell_volume": 0.0,
        "tick_count": 1,
    }


def _bucket_start(tf: str, ts: float) -> float:
    """Calculate the start of the current bucket for a given timeframe."""
    interval: int = _INTERVALS[tf]
    return float(int(ts // interval) * interval)


def _update_candle(candle: Candle, price: float, qty: float, side: str) -> None:
    """Update an existing open candle with a new tick."""
    candle["close"] = price
    if price > candle["high"]:
        candle["high"] = price
    if price < candle["low"]:
        candle["low"] = price
    candle["volume"] += qty
    candle["tick_count"] += 1
    if side == "Buy":
        candle["buy_volume"] += qty
    else:
        candle["sell_volume"] += qty


def _close_candle(tf: str, candle: Candle) -> None:
    """Finalize and push a candle to the buffer."""
    candle_buffers[tf].append(dict(candle))  # type: ignore[arg-type]

    # Hanya cetak ke terminal jika data historis sudah selesai dimuat (live)
    if not is_warmup:
        # Hanya log untuk timeframe 2h (sesuai timeframe strategi)
        if tf == "2h":
            logger.info(
                f"📊 [NEW {tf} CANDLE] O: {candle['open']:.5f} | H: {candle['high']:.5f} | "
                f"L: {candle['low']:.5f} | C: {candle['close']:.5f} | Vol: {candle['volume']:.2f}"
            )


# =============================================================================
# PRELOAD: Inject historical Bybit kline data into a buffer
# =============================================================================
def preload_candles(tf: str, raw_klines: list[list[str]]) -> int:
    """
    Inject historical candles (from Bybit REST kline API) into candle_buffers[tf].

    Bybit kline row format (v5):
        [startTime(ms), open, high, low, close, volume, turnover]

    Candles are sorted oldest → newest before inserting.
    Returns number of candles loaded.
    """
    if tf not in candle_buffers:
        logger.warning(f"[PRELOAD] Unknown timeframe: {tf} — skipped")
        return 0

    # Bybit returns newest first — reverse to oldest first
    ordered: list[list[str]] = sorted(raw_klines, key=lambda row: int(row[0]))

    loaded: int = 0
    for row in ordered:
        try:
            start_ms: int = int(row[0])
            candle: Candle = {
                "timeframe":   tf,
                "open_time":   start_ms / 1000.0,
                "open":        float(row[1]),
                "high":        float(row[2]),
                "low":         float(row[3]),
                "close":       float(row[4]),
                "volume":      float(row[5]),
                "buy_volume":  0.0,   # not available from kline endpoint
                "sell_volume": 0.0,
                "tick_count":  0,
            }
            candle_buffers[tf].append(candle)
            loaded += 1
        except (IndexError, ValueError) as e:
            logger.warning(f"[PRELOAD] Skipping malformed kline row: {row} | {e}")

    logger.info(f"[PRELOAD] [{tf}] Loaded {loaded} historical candles into buffer")
    return loaded


def _process_tick_for_tf(tf: str, price: float, qty: float, side: str, ts: float) -> None:
    """Process a single tick for a given timeframe."""
    bucket: float = _bucket_start(tf, ts)
    current: Optional[Candle] = _current_candle[tf]

    if current is None:
        # Initialize first candle
        new_c: Candle = _new_candle(tf, price, qty, ts)
        if side == "Buy":
            new_c["buy_volume"] = qty
        else:
            new_c["sell_volume"] = qty
        _current_candle[tf] = new_c
        data_stream.last_prices[tf] = price
        return

    if bucket > current["open_time"]:
        # New candle period started — close old, open new
        _close_candle(tf, current)
        new_c = _new_candle(tf, price, qty, ts)
        if side == "Buy":
            new_c["buy_volume"] = qty
        else:
            new_c["sell_volume"] = qty
        _current_candle[tf] = new_c
        data_stream.last_prices[tf] = price
    else:
        # Update existing candle
        _update_candle(current, price, qty, side)
        data_stream.last_prices[tf] = price


# =============================================================================
# DERIVED METRICS
# =============================================================================
def get_bid_ask_spread() -> float:
    """Return current bid-ask spread from orderbook."""
    bid: float = data_stream.best_bid.get("price", 0.0)
    ask: float = data_stream.best_ask.get("price", 0.0)
    if ask > 0 and bid > 0:
        return round(ask - bid, 8)
    return 0.0


def get_orderbook_imbalance() -> float:
    """
    Orderbook imbalance: (bid_qty - ask_qty) / (bid_qty + ask_qty)
    Range: -1.0 (full ask pressure) to +1.0 (full bid pressure)
    """
    snap = data_stream.orderbook_snapshot
    total_bid: float = sum(snap["bids"].values())
    total_ask: float = sum(snap["asks"].values())
    total: float = total_bid + total_ask
    if total == 0:
        return 0.0
    return round((total_bid - total_ask) / total, 6)


def get_volume_delta_last_n(n: int = 50) -> float:
    """
    Volume delta from last N ticks: buy_vol - sell_vol
    Positive = net buy pressure.
    """
    ticks: list[TickData] = list(data_stream.tick_buffer)[-n:]
    buy_vol: float = sum(t["qty"] for t in ticks if t["side"] == "Buy")
    sell_vol: float = sum(t["qty"] for t in ticks if t["side"] == "Sell")
    return round(buy_vol - sell_vol, 8)


def get_live_data() -> MarketDataSnapshot:
    """
    Aggregate view for strategy — called every tick.
    Returns candle snapshots, orderbook metrics, and funding rate
    for ALL timeframes defined in config.TIMEFRAMES.

    Access pattern in strategy.py:
        data["candles"]["1m"]      ← list of closed candles
        data["current"]["1m"]     ← live candle (updates every tick)
    """
    latest_tick: Optional[TickData] = data_stream.tick_buffer[-1] if data_stream.tick_buffer else None
    best_bid_snap: BidAskLevel = dict(data_stream.best_bid)  # type: ignore[assignment]
    best_ask_snap: BidAskLevel = dict(data_stream.best_ask)  # type: ignore[assignment]

    candles_snapshot: dict[str, list[Candle]] = {
        tf: list(buf) for tf, buf in candle_buffers.items()
    }
    current_snapshot: dict[str, Optional[Candle]] = {}
    for tf, c in _current_candle.items():
        current_snapshot[tf] = dict(c) if c else None  # type: ignore[assignment]

    return {
        "candles":            candles_snapshot,
        "current":            current_snapshot,
        "best_bid":           best_bid_snap,
        "best_ask":           best_ask_snap,
        "bid_ask_spread":     get_bid_ask_spread(),
        "orderbook_imbalance": get_orderbook_imbalance(),
        "volume_delta":       get_volume_delta_last_n(50),
        "funding_rate":       data_stream.funding_rate.get("value", 0.0),
        "latest_tick":        latest_tick,
        "is_warmup":          is_warmup,
    }


# =============================================================================
# MAIN RESAMPLER LOOP
# =============================================================================
async def start_resampler() -> None:
    """
    Continuously consume tick_buffer and update candles for all timeframes
    defined in config.TIMEFRAMES.
    """
    logger.info("Resampler started.")
    _initial_price_printed: bool = False

    # BUG-07 FIX: Ganti tracking berbasis _processed_count (tidak stabil saat buffer overflow)
    # dengan tracking berbasis timestamp tick terakhir yang diproses.
    # Deque bisa kehilangan item lama saat maxlen tercapai, menggeser index,
    # sehingga _processed_count tidak lagi valid. Timestamp adalah identitas
    # intrinsik setiap tick dan tidak berubah walau buffer overflow.
    _last_processed_ts: float = 0.0

    while True:
        buf_snapshot: list[TickData] = list(data_stream.tick_buffer)

        if not buf_snapshot:
            await asyncio.sleep(0.001)
            continue

        # Cari semua tick baru yang belum diproses (timestamp > _last_processed_ts)
        new_ticks: list[TickData] = [t for t in buf_snapshot if t["timestamp"] > _last_processed_ts]

        if new_ticks:
            # Update pointer ke timestamp tick terbaru yang akan diproses
            _last_processed_ts = new_ticks[-1]["timestamp"]

            for tick in new_ticks:
                price: float = tick["price"]
                qty: float   = tick["qty"]
                side: str    = tick["side"]
                ts: float    = tick["timestamp"]

                if not _initial_price_printed:
                    logger.info(f"[INITIAL PRICE] Harga saat bot mulai ({list(TIMEFRAMES.keys())[-1]}): {price:.5f}")
                    _initial_price_printed = True

                for tf in TIMEFRAMES:   # iterates whatever is in config
                    _process_tick_for_tf(tf, price, qty, side, ts)

        await asyncio.sleep(0)
