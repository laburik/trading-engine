# =============================================================================
# data_stream.py — Real-Time Data Ingestion Layer (Bybit WebSocket + REST)
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
import aiohttp
from collections import deque
from sortedcontainers import SortedDict
from config import (
    SYMBOL, CATEGORY, WS_PUBLIC_URL, REST_URL,
    TICK_BUFFER_SIZE, ORDERBOOK_BUFFER_SIZE,
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
# FUNDING RATE (REST polling)
# =============================================================================
async def _fetch_funding_rate(session: aiohttp.ClientSession) -> None:
    """Fetch current funding rate from Bybit REST API."""
    url = f"{REST_URL}/v5/market/funding/history"
    params: dict[str, str | int] = {
        "category": CATEGORY,
        "symbol": SYMBOL,
        "limit": 1,
    }
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            data: dict[str, object] = await resp.json()
            if data.get("retCode") == 0:
                result_list = data["result"]  # type: ignore[index]
                entries: list[dict[str, str]] = result_list["list"]  # type: ignore[index]
                if entries:
                    fr: float = float(entries[0].get("fundingRate", 0.0))
                    funding_rate["value"] = fr
                    logger.info(f"Funding rate updated: {fr:.6f}")
    except Exception as e:
        logger.warning(f"Funding rate fetch error: {e}")


async def _funding_rate_loop(session: aiohttp.ClientSession) -> None:
    """Poll funding rate every FUNDING_RATE_INTERVAL_SEC."""
    while True:
        await _fetch_funding_rate(session)
        await asyncio.sleep(FUNDING_RATE_INTERVAL_SEC)


# =============================================================================
# ORDERBOOK PROCESSING
# =============================================================================
def _apply_orderbook_delta(side: str, updates: list[list[str]]) -> None:
    """Apply incremental orderbook updates (Bybit delta format)."""
    book: SortedDict = orderbook_snapshot[side]
    for price_str, qty_str in updates:
        price: float = float(price_str)
        qty: float = float(qty_str)
        if qty == 0:
            book.pop(price, None)
        else:
            book[price] = qty


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


def _process_orderbook_message(msg: dict[str, object]) -> None:
    """Handle orderbook snapshot and delta messages."""
    msg_type: str = str(msg.get("type", ""))
    data: dict[str, list[list[str]]] = msg.get("data", {})  # type: ignore[assignment]

    if msg_type == "snapshot":
        orderbook_snapshot["bids"].clear()
        orderbook_snapshot["bids"].update(
            {float(p): float(q) for p, q in data.get("b", [])}
        )
        orderbook_snapshot["asks"].clear()
        orderbook_snapshot["asks"].update(
            {float(p): float(q) for p, q in data.get("a", [])}
        )
    elif msg_type == "delta":
        _apply_orderbook_delta("bids", data.get("b", []))
        _apply_orderbook_delta("asks", data.get("a", []))

    _update_best_bid_ask()

    # Push to buffer
    snapshot_copy: OrderbookSnapshot = {
        "timestamp": time.time(),
        "bids": sorted(orderbook_snapshot["bids"].items(), reverse=True)[:ORDERBOOK_DEPTH],
        "asks": sorted(orderbook_snapshot["asks"].items())[:ORDERBOOK_DEPTH],
        "best_bid": dict(best_bid),  # type: ignore[typeddict-item]
        "best_ask": dict(best_ask),  # type: ignore[typeddict-item]
    }
    orderbook_buffer.append(snapshot_copy)


# =============================================================================
# TICK PROCESSING
# =============================================================================
def _process_trade_message(msg: dict[str, object]) -> None:
    """Handle publicTrade messages (tick-by-tick)."""
    data_list: list[dict[str, object]] = msg.get("data", [])  # type: ignore[assignment]
    has_new: bool = False
    for trade in data_list:
        tick: TickData = {
            "timestamp": float(trade.get("T", time.time() * 1000)) / 1000,  # type: ignore[arg-type]
            "price": float(trade.get("p", 0)),  # type: ignore[arg-type]
            "qty": float(trade.get("v", 0)),  # type: ignore[arg-type]
            "side": str(trade.get("S", "")),    # "Buy" or "Sell"
            "trade_id": str(trade.get("i", "")),
        }
        tick_buffer.append(tick)
        has_new = True

    if has_new and new_tick_event is not None:
        new_tick_event.set()


# =============================================================================
# WEBSOCKET HANDLER
# =============================================================================
async def _ws_handler(ws: aiohttp.ClientWebSocketResponse) -> None:
    """Main WebSocket message dispatcher."""
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            try:
                data: dict[str, object] = json.loads(msg.data)

                # --- TANGKAP ERROR DARI BYBIT ---
                if "success" in data and data["success"] is False:
                    # Cetak alasan penolakan dari Bybit ke terminal
                    logger.error(f"🚨 BYBIT ERROR: {data.get('ret_msg', 'Permintaan ditolak server/topic tidak valid')}")
                # --------------------------------

                topic: str = str(data.get("topic", ""))

                if topic.startswith("publicTrade"):
                    _process_trade_message(data)

                elif topic.startswith("orderbook"):
                    _process_orderbook_message(data)

            except json.JSONDecodeError:
                pass
            except Exception as e:
                logger.error(f"WS handler error: {e}")

        elif msg.type == aiohttp.WSMsgType.ERROR:
            logger.error(f"WebSocket error: {ws.exception()}")
            break
        elif msg.type == aiohttp.WSMsgType.CLOSED:
            logger.warning("WebSocket connection closed.")
            break


# =============================================================================
# WEBSOCKET CONNECTION WITH AUTO-RECONNECT
# =============================================================================
async def _connect_websocket(session: aiohttp.ClientSession) -> None:
    """Connect to Bybit public WebSocket with auto-reconnect."""
    reconnect_delay: int = 1
    max_delay: int = 60

    while True:
        try:
            logger.info(f"Connecting to WebSocket: {WS_PUBLIC_URL}")
            async with session.ws_connect(
                WS_PUBLIC_URL,
                heartbeat=20,
                receive_timeout=30,
            ) as ws:
                reconnect_delay = 1  # Reset on successful connect

                # Subscribe berdasarkan DATA_MODE
                if DATA_MODE == "tick":
                    # Mode tick: butuh trade stream + orderbook
                    subscribe_msg: dict[str, object] = {
                        "op": "subscribe",
                        "args": [
                            f"publicTrade.{SYMBOL}",
                            f"orderbook.{ORDERBOOK_DEPTH}.{SYMBOL}",
                        ]
                    }
                    await ws.send_str(json.dumps(subscribe_msg))
                    logger.info(f"Subscribed to: publicTrade.{SYMBOL}, orderbook.{ORDERBOOK_DEPTH}.{SYMBOL}")
                else:
                    # Mode kline: hanya butuh orderbook (kline diurus candle_stream.py)
                    subscribe_msg = {
                        "op": "subscribe",
                        "args": [f"orderbook.{ORDERBOOK_DEPTH}.{SYMBOL}"]
                    }
                    await ws.send_str(json.dumps(subscribe_msg))
                    logger.info(f"[data_stream] Subscribed to: orderbook.{ORDERBOOK_DEPTH}.{SYMBOL} (kline mode, trade stream handled by candle_stream)")

                await _ws_handler(ws)

        except asyncio.CancelledError:
            logger.info("WebSocket task cancelled.")
            break
        except Exception as e:
            logger.error(f"WebSocket connection error: {e}")

        logger.info(f"Reconnecting in {reconnect_delay}s...")
        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, max_delay)


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================
async def start_data_stream() -> None:
    """
    Start all data stream components:
    - WebSocket (tick + orderbook)
    - Funding rate REST loop
    - Heartbeat writer
    """
    global new_tick_event
    new_tick_event = asyncio.Event()

    logger.info(f"Starting data stream for {SYMBOL}...")

    connector = aiohttp.TCPConnector(limit=20, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Fetch initial funding rate
        await _fetch_funding_rate(session)

        # Run all loops concurrently
        await asyncio.gather(
            _connect_websocket(session),
            _funding_rate_loop(session),
            _heartbeat_loop(),
        )


if __name__ == "__main__":
    asyncio.run(start_data_stream())
