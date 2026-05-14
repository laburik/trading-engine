# =============================================================================
# data_stream.py — Real-Time Data Ingestion Layer (CCXT Pro WebSocket)
# =============================================================================
#██████╗ ██╗███████╗██╗  ██╗██╗   ██╗    ██╗  ██╗███████╗███╗   ██╗ ██████╗ ██╗  ██╗███████╗██████╗ ██████╗ ██████╗  ██████╗
#██╔══██╗██║╚══███╔╝██║ ██╔╝╚██╗ ██╔╝    ██║  ██║██╔════╝████╗  ██║██╔════╝ ██║ ██╔╝██╔════╝██╔══██╗██╔══██╗██╔══██╗██╔═══██╗
#██████╔╝██║  ███╔╝ █████╔╝  ╚████╔╝     ███████║█████╗  ██╔██╗ ██║██║  ███╗█████╔╝ █████╗  ██████╔╝██████╔╝██████╔╝██║   ██║
#██╔══██╗██║ ███╔╝  ██╔═██╗   ╚██╔╝      ██╔══██║██╔══╝  ██║╚██╗██║██║   ██║██╔═██╗ ██╔══╝  ██╔══██╗██╔═══╝ ██╔══██╗██║   ██║
#██║  ██║██║███████╗██║  ██╗   ██║       ██║  ██║███████╗██║ ╚████║╚██████╔╝██║  ██╗███████╗██║  ██║██║     ██║  ██║╚██████╔╝
#╚═╝  ╚═╝╚═╝╚══════╝╚═╝  ╚═╝   ╚═╝       ╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝  ╚═╝ ╚═════╝
# if you copy, haram, unless you ask permission from the author
# for personal use only, if you use it for commercial purposes, you will be responsible for your own actions
from __future__ import annotations

import asyncio
import json
import time
import logging
from typing import Optional
from collections import deque
from sortedcontainers import SortedDict
from config import (
    SYMBOL, TICK_BUFFER_SIZE, ORDERBOOK_BUFFER_SIZE,
    ORDERBOOK_DEPTH, FUNDING_RATE_INTERVAL_SEC,
    HEARTBEAT_FILE, HEARTBEAT_INTERVAL_SEC,
    DATA_MODE,
)
from ft_types import (
    BidAskLevel,
    FundingRateState,
    HeartbeatPayload,
    OrderbookSnapshot,
    TickData,
)
from ccxt_client import exchange
import ccxt_client

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DATA_STREAM] %(levelname)s: %(message)s"
)
logger = logging.getLogger("data_stream")

# =============================================================================
# SHARED BUFFERS (imported by other modules)
# =============================================================================
tick_buffer: deque[TickData] = deque(maxlen=TICK_BUFFER_SIZE)
orderbook_buffer: deque[OrderbookSnapshot] = deque(maxlen=ORDERBOOK_BUFFER_SIZE)
funding_rate: FundingRateState = {"value": 0.0, "next_funding_time": 0, "predicted": 0.0}

# Latest best bid/ask (updated from orderbook stream)
best_bid: BidAskLevel = {"price": 0.0, "qty": 0.0}
best_ask: BidAskLevel = {"price": 0.0, "qty": 0.0}
last_prices: dict[str, float] = {}  # Injected for real-time dashboard display per timeframe

# Orderbook snapshot (SortedDict keyed by price level → qty)
orderbook_snapshot: dict[str, SortedDict] = {
    "bids": SortedDict(),
    "asks": SortedDict(),
}

# Internal event loop reference
_loop: Optional[asyncio.AbstractEventLoop] = None
new_tick_event: Optional[asyncio.Event] = None


# =============================================================================
# HEARTBEAT
# =============================================================================
async def _heartbeat_loop() -> None:
    """Update heartbeat.json every HEARTBEAT_INTERVAL_SEC seconds."""
    while True:
        try:
            payload: HeartbeatPayload = {
                "last_update": time.time(),
                "last_prices": last_prices,
            }
            with open(HEARTBEAT_FILE, "w") as f:
                json.dump(payload, f)
        except Exception as e:
            logger.warning(f"Heartbeat write failed: {e}")
        await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)


# =============================================================================
# FUNDING RATE (via CCXT Pro REST)
# =============================================================================
async def _fetch_funding_rate() -> None:
    """Fetch current funding rate dari Bybit via CCXT."""
    try:
        fr_data = await exchange.fetch_funding_rate(ccxt_client.CCXT_SYMBOL)
        fr: float = float(fr_data.get("fundingRate") or 0.0)
        funding_rate["value"] = fr
        logger.info(f"Funding rate updated: {fr:.6f}")
    except Exception as e:
        logger.warning(f"Funding rate fetch error: {e}")


async def _funding_rate_loop() -> None:
    """Poll funding rate every FUNDING_RATE_INTERVAL_SEC."""
    while True:
        await _fetch_funding_rate()
        await asyncio.sleep(FUNDING_RATE_INTERVAL_SEC)


# =============================================================================
# ORDERBOOK PROCESSING (CCXT Pro — snapshot + delta merge ditangani internal)
# =============================================================================
def _update_best_bid_ask() -> None:
    """Recalculate best bid and best ask from snapshot."""
    bids: SortedDict = orderbook_snapshot["bids"]
    asks: SortedDict = orderbook_snapshot["asks"]

    if bids:
        best_price, qty = bids.peekitem(-1)
        best_bid["price"] = best_price
        best_bid["qty"] = qty

    if asks:
        best_price, qty = asks.peekitem(0)
        best_ask["price"] = best_price
        best_ask["qty"] = qty


def _process_ccxt_orderbook(ob: dict) -> None:
    """
    Update orderbook_snapshot, best_bid, best_ask dari CCXT Pro orderbook object.
    CCXT Pro sudah menggabungkan snapshot + delta secara internal — tidak perlu
    manual delta apply lagi.
    ob["bids"] = [[price, qty], ...] sorted descending
    ob["asks"] = [[price, qty], ...] sorted ascending
    """
    orderbook_snapshot["bids"].clear()
    orderbook_snapshot["asks"].clear()

    for price, qty in (ob.get("bids") or [])[:ORDERBOOK_DEPTH]:
        if float(qty) > 0:
            orderbook_snapshot["bids"][float(price)] = float(qty)

    for price, qty in (ob.get("asks") or [])[:ORDERBOOK_DEPTH]:
        if float(qty) > 0:
            orderbook_snapshot["asks"][float(price)] = float(qty)

    _update_best_bid_ask()

    # Push ke buffer (format identik dengan versi lama)
    snapshot_copy: OrderbookSnapshot = {
        "timestamp": time.time(),
        "bids": list(reversed(orderbook_snapshot["bids"].items()))[:ORDERBOOK_DEPTH],
        "asks": list(orderbook_snapshot["asks"].items())[:ORDERBOOK_DEPTH],
        "best_bid": dict(best_bid),  # type: ignore[typeddict-item]
        "best_ask": dict(best_ask),  # type: ignore[typeddict-item]
    }
    orderbook_buffer.append(snapshot_copy)


# =============================================================================
# TICK PROCESSING (CCXT Pro)
# =============================================================================
def _process_ccxt_trades(trades: list) -> None:
    """
    Convert CCXT trade list ke TickData dan push ke tick_buffer.
    CCXT side: "buy"/"sell" → dipetakan ke "Buy"/"Sell" sesuai format internal.
    """
    has_new: bool = False
    for trade in trades:
        tick: TickData = {
            "timestamp": float(trade.get("timestamp") or time.time() * 1000) / 1000,
            "price":     float(trade.get("price", 0)),
            "qty":       float(trade.get("amount", 0)),
            "side":      "Buy" if trade.get("side") == "buy" else "Sell",
            "trade_id":  str(trade.get("id", "")),
        }
        tick_buffer.append(tick)
        has_new = True

    if has_new and new_tick_event is not None:
        new_tick_event.set()


# =============================================================================
# CCXT PRO WATCH LOOPS
# Auto-reconnect ditangani CCXT Pro secara internal — tidak perlu manual retry.
# =============================================================================
async def _watch_orderbook_loop() -> None:
    """
    Watch orderbook via CCXT Pro.
    Menggantikan: manual WS connect + subscribe + snapshot/delta handler + auto-reconnect.
    """
    logger.info(f"Starting orderbook watch for {ccxt_client.CCXT_SYMBOL} (depth={ORDERBOOK_DEPTH})...")
    while True:
        try:
            ob = await exchange.watch_order_book(ccxt_client.CCXT_SYMBOL, limit=ORDERBOOK_DEPTH)
            _process_ccxt_orderbook(ob)
        except asyncio.CancelledError:
            logger.info("Orderbook watch cancelled.")
            break
        except Exception as e:
            logger.error(f"Orderbook watch error: {e}. Retrying in 1s...")
            await asyncio.sleep(1)


async def _watch_trades_loop() -> None:
    """
    Watch tick-by-tick trades via CCXT Pro.
    Hanya aktif di DATA_MODE = 'tick'.
    """
    logger.info(f"Starting trades watch for {ccxt_client.CCXT_SYMBOL}...")
    while True:
        try:
            trades = await exchange.watch_trades(ccxt_client.CCXT_SYMBOL)
            _process_ccxt_trades(trades)
        except asyncio.CancelledError:
            logger.info("Trades watch cancelled.")
            break
        except Exception as e:
            logger.error(f"Trades watch error: {e}. Retrying in 1s...")
            await asyncio.sleep(1)


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================
async def start_data_stream() -> None:
    """
    Start all data stream components via CCXT Pro:
    - Orderbook watch (always active)
    - Trades watch  (only in DATA_MODE = 'tick')
    - Funding rate poll loop
    - Heartbeat writer
    """
    global new_tick_event
    new_tick_event = asyncio.Event()

    logger.info(f"Starting data stream for {SYMBOL} (DATA_MODE={DATA_MODE})...")

    # Fetch initial funding rate
    await _fetch_funding_rate()

    loops = [
        _watch_orderbook_loop(),
        _funding_rate_loop(),
        _heartbeat_loop(),
    ]

    if DATA_MODE == "tick":
        loops.append(_watch_trades_loop())
        logger.info(f"Tick mode: trades watch enabled for {SYMBOL}")
    else:
        logger.info("Kline mode: trades handled by candle_stream — trades watch skipped")

    await asyncio.gather(*loops)


if __name__ == "__main__":
    asyncio.run(start_data_stream())
