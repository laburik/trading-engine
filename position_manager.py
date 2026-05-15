#██████╗ ██╗███████╗██╗  ██╗██╗   ██╗    ██╗  ██╗███████╗███╗   ██╗ ██████╗ ██╗  ██╗███████╗██████╗ ██████╗ ██████╗  ██████╗ 
#██╔══██╗██║╚══███╔╝██║ ██╔╝╚██╗ ██╔╝    ██║  ██║██╔════╝████╗  ██║██╔════╝ ██║ ██╔╝██╔════╝██╔══██╗██╔══██╗██╔══██╗██╔═══██╗
#██████╔╝██║  ███╔╝ █████╔╝  ╚████╔╝     ███████║█████╗  ██╔██╗ ██║██║  ███╗█████╔╝ █████╗  ██████╔╝██████╔╝██████╔╝██║   ██║
#██╔══██╗██║ ███╔╝  ██╔═██╗   ╚██╔╝      ██╔══██║██╔══╝  ██║╚██╗██║██║   ██║██╔═██╗ ██╔══╝  ██╔══██╗██╔═══╝ ██╔══██╗██║   ██║
#██║  ██║██║███████╗██║  ██╗   ██║       ██║  ██║███████╗██║ ╚████║╚██████╔╝██║  ██╗███████╗██║  ██║██║     ██║  ██║╚██████╔╝
#╚═╝  ╚═╝╚═╝╚══════╝╚═╝  ╚═╝   ╚═╝       ╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝  ╚═╝ ╚═════╝                                                                                                                             
# if you copy, haram, unless you ask permission from the author
# for personal use only, if you use it for commercial purposes, you will be responsible for your own actions
# =============================================================================
# position_manager.py — Realtime PnL Engine (Bid/Ask Based)
# =============================================================================
#
# Rules:
#   LONG  → unrealized_pnl = (bid_price - entry_price) * qty
#   SHORT → unrealized_pnl = (entry_price - ask_price) * qty
#   Equity = balance + unrealized_pnl
#
# Never uses mid-price.
# Updated every tick.
# =============================================================================
from __future__ import annotations

import time
import asyncio
import logging
from typing import Optional
from config import INITIAL_BALANCE, MODE, SYMBOL, PNL_SYNC_INTERVAL
from ccxt_client import exchange
import ccxt_client
from trade_logger import log_trade, log_equity
from ft_types import EquityLogRow, PnlSummary, PositionState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [POSITION] %(levelname)s: %(message)s"
)
logger = logging.getLogger("position_manager")

# =============================================================================
# STATE
# =============================================================================
_state: PositionState = {
    "side": "none",          # "long" | "short" | "none"
    "entry_price": 0.0,
    "qty": 0.0,
    "balance": INITIAL_BALANCE,
    "unrealized_pnl": 0.0,
    "equity": INITIAL_BALANCE,
    "realized_pnl_total": 0.0,
    "total_fees": 0.0,        # cumulative fees paid (both sides)
    "open_time": None,
}


# =============================================================================
# OPEN / CLOSE POSITION
# =============================================================================
def open_position(side: str, entry_price: float, qty: float, entry_fee: float = 0.0) -> None:
    """
    Record a new open position and deduct entry fee from balance.

    side:        "long" or "short"
    entry_price: fill price (ASK for long, BID for short)
    qty:         position size
    entry_fee:   fee = qty * entry_price * FEE_RATE  (passed from execution.py)
    """
    if _state["side"] != "none":
        logger.warning(f"Already in a {_state['side']} position. Ignoring open_position.")
        return

    _state["side"] = side
    _state["entry_price"] = entry_price
    _state["qty"] = qty
    _state["unrealized_pnl"] = 0.0
    _state["open_time"] = time.time()

    # Deduct entry fee immediately from balance
    _state["balance"] -= entry_fee
    _state["total_fees"] += entry_fee
    _state["equity"] = _state["balance"]

    logger.info(f"Position OPENED: {side.upper()} {qty} @ {entry_price} | Entry fee: {entry_fee:.6f}")


def close_position(exit_price: float, exit_fee: float = 0.0) -> tuple[float, float]:
    """
    Close the current position at exit_price, deducting exit fee.

    exit_price: fill price (BID for long exit, ASK for short exit)
    exit_fee:   fee = qty * exit_price * FEE_RATE  (passed from execution.py)

    Returns: (realized_pnl_gross, exit_fee)
    """
    if _state["side"] == "none":
        logger.warning("No open position to close.")
        return 0.0, 0.0

    side: str        = _state["side"]
    entry: float     = _state["entry_price"]
    qty: float       = _state["qty"]

    gross_pnl: float
    if side == "long":
        gross_pnl = (exit_price - entry) * qty
    else:  # short
        gross_pnl = (entry - exit_price) * qty

    net_pnl: float = gross_pnl - exit_fee

    _state["balance"] += net_pnl
    _state["realized_pnl_total"] += net_pnl
    _state["total_fees"] += exit_fee
    _state["unrealized_pnl"] = 0.0
    _state["equity"] = _state["balance"]
    _state["side"] = "none"
    _state["entry_price"] = 0.0
    _state["qty"] = 0.0
    _state["open_time"] = None

    logger.info(
        f"Position CLOSED: {side.upper()} @ {exit_price} | "
        f"Gross PnL: {gross_pnl:.4f} | Exit fee: {exit_fee:.6f} | "
        f"Net PnL: {net_pnl:.4f} | Balance: {_state['balance']:.4f}"
    )
    return net_pnl, exit_fee


_last_equity_log_time: float = 0.0

# =============================================================================
# TICK UPDATE (called every tick by main.py)
# =============================================================================
def update_pnl(bid_price: float, ask_price: float) -> None:
    """
    Paper mode: recalculate unrealized PnL from current bid/ask every tick.
    Demo/Live mode: no-op — Bybit data is synced by start_pnl_sync_loop().
    """
    global _last_equity_log_time
    if MODE != "paper":
        return  # Bybit handles PnL in demo/live

    if _state["side"] == "none":
        _state["unrealized_pnl"] = 0.0
        _state["equity"] = _state["balance"]
        return

    entry: float = _state["entry_price"]
    qty: float   = _state["qty"]

    if _state["side"] == "long":
        _state["unrealized_pnl"] = (bid_price - entry) * qty
    else:
        _state["unrealized_pnl"] = (entry - ask_price) * qty

    _state["equity"] = _state["balance"] + _state["unrealized_pnl"]

    # --- PENCEGAHAN SPAM MEMORY (CETAK MAKS 1 KALI PER MENIT) ---
    now: float = time.time()
    if now - _last_equity_log_time >= 60.0:
        equity_row: EquityLogRow = {
            "timestamp": now,
            "balance": _state["balance"],
            "equity": _state["equity"],
            "unrealized_pnl": _state["unrealized_pnl"],
        }
        log_equity(equity_row)
        _last_equity_log_time = now


# =============================================================================
# BYBIT PNL SYNC LOOP (demo / live mode)
# =============================================================================
async def _sync_position() -> None:
    """Fetch open position dari Bybit via CCXT dan update local state."""
    try:
        positions = await exchange.fetch_positions([ccxt_client.CCXT_SYMBOL])
        # CCXT returns list; filter posisi aktif (contracts > 0)
        active = [p for p in positions if float(p.get("contracts") or 0) > 0]
        if active:
            pos = active[0]
            side_raw: str = str(pos.get("side", "")).lower()  # "long" or "short"
            _state["side"] = side_raw if side_raw in ("long", "short") else "none"
            _state["entry_price"] = float(pos.get("entryPrice") or 0)
            _state["qty"]         = float(pos.get("contracts") or 0)
            _state["unrealized_pnl"] = float(pos.get("unrealizedPnl") or 0)
        else:
            _state["side"]           = "none"
            _state["qty"]            = 0.0
            _state["unrealized_pnl"] = 0.0
    except Exception as e:
        logger.warning(f"[PNL SYNC] Position fetch error: {e}")


async def _sync_balance() -> None:
    """Fetch wallet balance dari Bybit via CCXT dan update local state."""
    try:
        balance = await exchange.fetch_balance()
        usdt = balance.get("USDT", {})
        wallet = float(usdt.get("total") or _state["balance"])
        _state["balance"] = wallet
    except Exception as e:
        logger.warning(f"[PNL SYNC] Balance fetch error: {e}")


async def start_pnl_sync_loop() -> None:
    """
    Demo / Live mode only.
    Polls Bybit every PNL_SYNC_INTERVAL seconds for:
      - Open position (side, qty, entry_price, unrealized_pnl)
      - Wallet balance & equity
    Overwrites local _state with Bybit's authoritative data.
    Also logs equity curve to CSV.
    """
    global _last_equity_log_time

    if MODE == "paper":
        return  # not needed in paper mode

    logger.info(f"[PNL SYNC] Starting for MODE={MODE}, interval={PNL_SYNC_INTERVAL}s")
    while True:
        await _sync_position()
        await _sync_balance()

        # Recalculate equity from fetched data
        _state["equity"] = _state["balance"] + _state["unrealized_pnl"]

        now: float = time.time()
        if now - _last_equity_log_time >= 60.0:
            equity_row: EquityLogRow = {
                "timestamp": now,
                "balance": _state["balance"],
                "equity": _state["equity"],
                "unrealized_pnl": _state["unrealized_pnl"],
            }
            log_equity(equity_row)
            _last_equity_log_time = now

        await asyncio.sleep(PNL_SYNC_INTERVAL)


# =============================================================================
# OLD-09 FIX: INITIAL SYNC (dipanggil sekali oleh main.py sebelum strategy mulai)
# =============================================================================
async def initial_sync() -> None:
    """
    Lakukan satu kali sync balance dan posisi dari Bybit sebelum strategy loop
    dimulai (hanya untuk mode demo/live).
    """
    if MODE == "paper":
        return  # paper mode tidak butuh sync dari Bybit

    logger.info("[PNL SYNC] Running initial_sync() before strategy starts...")
    try:
        await _sync_position()
        await _sync_balance()
        _state["equity"] = _state["balance"] + _state["unrealized_pnl"]
        logger.info(
            f"[PNL SYNC] Initial sync done. "
            f"Balance={_state['balance']:.2f} USDT | "
            f"Side={_state['side']} | Qty={_state['qty']}"
        )
    except Exception as e:
        logger.warning(f"[PNL SYNC] initial_sync() failed (non-fatal): {e}")


# =============================================================================
# ACCESSORS
# =============================================================================
def get_position() -> dict[str, object]:
    """Return a copy of the current position state."""
    return dict(_state)  # type: ignore[return-value]


def get_pnl_summary() -> PnlSummary:
    """Return a summary dict for dashboard / logging."""
    return {
        "side": _state["side"],
        "entry_price": _state["entry_price"],
        "qty": _state["qty"],
        "balance": round(_state["balance"], 6),
        "unrealized_pnl": round(_state["unrealized_pnl"], 6),
        "equity": round(_state["equity"], 6),
        "realized_pnl_total": round(_state["realized_pnl_total"], 6),
        "total_fees": round(_state["total_fees"], 6),
    }
