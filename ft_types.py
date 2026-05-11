# =============================================================================
# ft_types.py — Shared Type Definitions (Single Source of Truth)
# =============================================================================
#
# All complex data structures used across the trading engine are defined here.
# Import from this module instead of using bare `dict`, `list`, or `tuple`.
#
# Mypy compliance target: strict mode
# =============================================================================

from __future__ import annotations

from typing import Literal, Optional
from typing_extensions import TypedDict


# ---------------------------------------------------------------------------
# CANDLE
# ---------------------------------------------------------------------------
class Candle(TypedDict):
    """A single OHLCV candle as stored in candle_buffers and _current_candle."""
    timeframe: str
    open_time: float          # Unix timestamp (seconds) of candle open
    open: float
    high: float
    low: float
    close: float
    volume: float
    buy_volume: float
    sell_volume: float
    tick_count: int


# ---------------------------------------------------------------------------
# TICK (publicTrade)
# ---------------------------------------------------------------------------
class TickData(TypedDict):
    """A single trade tick received from Bybit publicTrade WebSocket."""
    timestamp: float          # Unix seconds
    price: float
    qty: float
    side: str                 # "Buy" or "Sell"
    trade_id: str


# ---------------------------------------------------------------------------
# BID / ASK LEVEL
# ---------------------------------------------------------------------------
class BidAskLevel(TypedDict):
    """Best bid or best ask snapshot."""
    price: float
    qty: float


# ---------------------------------------------------------------------------
# ORDERBOOK SNAPSHOT (pushed into orderbook_buffer)
# ---------------------------------------------------------------------------
class OrderbookSnapshot(TypedDict):
    """Snapshot of the orderbook pushed into orderbook_buffer."""
    timestamp: float
    bids: list[tuple[float, float]]   # [(price, qty), ...] descending
    asks: list[tuple[float, float]]   # [(price, qty), ...] ascending
    best_bid: BidAskLevel
    best_ask: BidAskLevel


# ---------------------------------------------------------------------------
# FUNDING RATE STATE
# ---------------------------------------------------------------------------
class FundingRateState(TypedDict):
    """Funding rate dict shared via data_stream.funding_rate."""
    value: float
    next_funding_time: int
    predicted: float


# ---------------------------------------------------------------------------
# MARKET DATA SNAPSHOT  (returned by get_live_data)
# ---------------------------------------------------------------------------
class MarketDataSnapshot(TypedDict):
    """
    Full aggregated market snapshot returned by data_resampler.get_live_data()
    (and candle_stream.get_live_data()).  Consumed by strategy.on_tick().
    """
    candles: dict[str, list[Candle]]          # closed candles per timeframe
    current: dict[str, Optional[Candle]]      # live (open) candle per timeframe
    best_bid: BidAskLevel
    best_ask: BidAskLevel
    bid_ask_spread: float
    orderbook_imbalance: float
    volume_delta: float
    funding_rate: float
    latest_tick: Optional[TickData]
    is_warmup: bool


# ---------------------------------------------------------------------------
# SIGNAL  (produced by strategy.generate_signal, consumed by execution.place_order)
# ---------------------------------------------------------------------------
class Signal(TypedDict, total=False):
    """
    Trading signal dict.  `action` is the only required key.
    `qty` and `price` are optional — execution fills them if absent.
    """
    action: Literal["buy", "sell", "hold", "close"]
    reason: str
    qty: float
    price: float


# ---------------------------------------------------------------------------
# ORDER RESULT  (returned by _paper_execute / _live_place_order_async)
# ---------------------------------------------------------------------------
class OrderResult(TypedDict, total=False):
    """Result dict returned by paper or live order execution functions."""
    status: str       # "filled" | "closed" | "rejected" | "skipped" | "hold" | "failed"
    price: float
    qty: float
    fee: float
    pnl: float
    reason: str
    orderId: str


# ---------------------------------------------------------------------------
# POSITION STATE  (the internal _state dict in position_manager)
# ---------------------------------------------------------------------------
class PositionState(TypedDict):
    """Internal mutable state dict in position_manager."""
    side: str                    # "long" | "short" | "none"
    entry_price: float
    qty: float
    balance: float
    unrealized_pnl: float
    equity: float
    realized_pnl_total: float
    total_fees: float
    open_time: Optional[float]   # Unix timestamp of position open, or None


# ---------------------------------------------------------------------------
# PNL SUMMARY  (returned by get_pnl_summary)
# ---------------------------------------------------------------------------
class PnlSummary(TypedDict):
    """Snapshot of account PnL for dashboard/logging."""
    side: str
    entry_price: float
    qty: float
    balance: float
    unrealized_pnl: float
    equity: float
    realized_pnl_total: float
    total_fees: float


# ---------------------------------------------------------------------------
# TRADE LOG ROW  (queued to CSV by trade_logger.log_trade)
# ---------------------------------------------------------------------------
class TradeLogRow(TypedDict, total=False):
    """A row written to trade_history.csv."""
    timestamp: float
    symbol: str
    side: str
    entry_price: float
    exit_price: Optional[float]
    qty: float
    fee: float
    pnl: Optional[float]
    reason: str


# ---------------------------------------------------------------------------
# EQUITY LOG ROW  (queued to CSV by trade_logger.log_equity)
# ---------------------------------------------------------------------------
class EquityLogRow(TypedDict):
    """A row written to equity_curve.csv."""
    timestamp: float
    balance: float
    equity: float
    unrealized_pnl: float


# ---------------------------------------------------------------------------
# BOT STATE  (internal state in strategy.py)
# ---------------------------------------------------------------------------
class BotState(TypedDict):
    """Internal position-tracking state dict in strategy.py."""
    in_position: bool
    side: str          # "long" | "short" | "none"
    entry_price: float
    entry_time: float


# ---------------------------------------------------------------------------
# HEARTBEAT PAYLOAD  (written to heartbeat.json)
# ---------------------------------------------------------------------------
class HeartbeatPayload(TypedDict):
    """Content written to heartbeat.json by data_stream._heartbeat_loop."""
    last_update: float
    last_prices: dict[str, float]
