
#██████╗ ██╗███████╗██╗  ██╗██╗   ██╗    ██╗  ██╗███████╗███╗   ██╗ ██████╗ ██╗  ██╗███████╗██████╗ ██████╗ ██████╗  ██████╗ 
#██╔══██╗██║╚══███╔╝██║ ██╔╝╚██╗ ██╔╝    ██║  ██║██╔════╝████╗  ██║██╔════╝ ██║ ██╔╝██╔════╝██╔══██╗██╔══██╗██╔══██╗██╔═══██╗
#██████╔╝██║  ███╔╝ █████╔╝  ╚████╔╝     ███████║█████╗  ██╔██╗ ██║██║  ███╗█████╔╝ █████╗  ██████╔╝██████╔╝██████╔╝██║   ██║
#██╔══██╗██║ ███╔╝  ██╔═██╗   ╚██╔╝      ██╔══██║██╔══╝  ██║╚██╗██║██║   ██║██╔═██╗ ██╔══╝  ██╔══██╗██╔═══╝ ██╔══██╗██║   ██║
#██║  ██║██║███████╗██║  ██╗   ██║       ██║  ██║███████╗██║ ╚████║╚██████╔╝██║  ██╗███████╗██║  ██║██║     ██║  ██║╚██████╔╝
#╚═╝  ╚═╝╚═╝╚══════╝╚═╝  ╚═╝   ╚═╝       ╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝  ╚═╝ ╚═════╝                                                                                                                             
# if you copy, haram, unless you ask permission from the author
# for personal use only, if you use it for commercial purposes, you will be responsible for your own actions
# =============================================================================
# execution.py — Order Execution Layer
# =============================================================================
#
# Responsibilities:
#   - Place paper or live orders via Bybit API
#   - Enforce BUY@ASK / SELL@BID pricing
#   - Handle slippage tolerance
#   - Handle partial fills with retry or cancel
#   - Retry logic (max 3x, millisecond delay)
#
# Strategy MUST NOT import aiohttp or Bybit API directly.
# All order flow goes through place_order().
# =============================================================================
from __future__ import annotations

import asyncio
import time
import logging
import math
from typing import Optional
import ccxt.pro as ccxt
from config import (
    SYMBOL, MODE,
    ORDER_SIZE_USDT, LEVERAGE, SLIPPAGE_TOLERANCE,
    MAX_RETRY, RETRY_DELAY_MS, CANCEL_ON_PARTIAL, FEE_RATE,
)
from ccxt_client import exchange
import ccxt_client
from ft_types import OrderResult, Signal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [EXECUTION] %(levelname)s: %(message)s"
)
logger = logging.getLogger("execution")

# BUG-05 FIX: Set untuk menyimpan referensi task fire-and-forget.
# Tanpa ini, Python GC bisa menghapus task sebelum selesai dieksekusi.
_background_tasks: set[asyncio.Task[None]] = set()

# Import live prices and position manager
import data_stream
import position_manager
from trade_logger import log_trade


# =============================================================================
# QTY STEP CACHE & HELPER
# =============================================================================
_qty_step_cache: Optional[float] = None


async def _get_qty_step() -> float:
    """Ambil qtyStep dari cache CCXT market info (tidak butuh HTTP request ulang)."""
    global _qty_step_cache
    if _qty_step_cache is not None:
        return _qty_step_cache
    try:
        # exchange.market() membaca dari cache load_markets() — tidak ada network call
        market = exchange.market(ccxt_client.CCXT_SYMBOL)
        step: float = float(market["precision"]["amount"])
        _qty_step_cache = step
        logger.info(f"Loaded qtyStep for {SYMBOL}: {step}")
        return step
    except Exception as e:
        logger.error(f"Failed to fetch qtyStep from CCXT market info: {e}")
    return 0.001


def _round_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    precision: int = max(0, int(round(-math.log10(step))))
    rounded: float = math.floor(value / step) * step
    return round(rounded, precision)


# =============================================================================
# PRICE VALIDATION (slippage check)
# =============================================================================
def _validate_price(side: str, intended_price: float) -> tuple[bool, str]:
    """
    Check whether the current market price is within slippage tolerance.

    BUY  → execute at ASK; reject if ASK has drifted > tolerance above intended
    SELL → execute at BID; reject if BID has drifted > tolerance below intended
    """
    ask: float = data_stream.best_ask.get("price", 0.0)
    bid: float = data_stream.best_bid.get("price", 0.0)

    if side == "buy":
        live_price: float = ask
        drift: float = (live_price - intended_price) / intended_price if intended_price else 0
        if drift > SLIPPAGE_TOLERANCE:
            return False, f"Slippage exceeded on BUY: live_ask={live_price} vs intended={intended_price} drift={drift:.5f}"

    elif side == "sell":
        live_price = bid
        drift = (intended_price - live_price) / intended_price if intended_price else 0
        if drift > SLIPPAGE_TOLERANCE:
            return False, f"Slippage exceeded on SELL: live_bid={live_price} vs intended={intended_price} drift={drift:.5f}"

    return True, ""


# =============================================================================
# PAPER TRADING EXECUTION
# =============================================================================
def _paper_execute(signal: Signal) -> OrderResult:
    """
    Simulate order execution in paper mode.
    Returns order result dict.
    """
    action: str = signal["action"]
    pos: dict[str, object] = position_manager.get_position()

    ask: float = data_stream.best_ask.get("price", 0.0)
    bid: float = data_stream.best_bid.get("price", 0.0)

    if ask == 0 or bid == 0:
        result: OrderResult = {"status": "rejected", "reason": "No market data available"}
        return result

    # Determine trade qty (computed by loop wrapper)
    qty: float = signal.get("qty", 0.0)

    if action == "buy":
        exec_price: float = ask
        ok: bool
        err: str
        ok, err = _validate_price("buy", signal.get("price", exec_price))
        if not ok:
            logger.warning(err)
            return {"status": "rejected", "reason": err}

        # Fee = qty × exec_price × FEE_RATE  (leveraged position size already)
        entry_fee: float = round(qty * exec_price * FEE_RATE, 8)
        position_manager.open_position("long", exec_price, qty, entry_fee)
        log_trade({
            "timestamp": time.time(),
            "symbol": SYMBOL,
            "side": "buy",
            "entry_price": exec_price,
            "exit_price": None,
            "qty": qty,
            "fee": entry_fee,
            "pnl": None,
            "reason": signal.get("reason", ""),
        })
        logger.info(f"[PAPER] BUY {qty} {SYMBOL} @ {exec_price} | Fee: {entry_fee:.6f} | {signal.get('reason')}")
        return {"status": "filled", "price": exec_price, "qty": qty, "fee": entry_fee}

    elif action == "sell":
        exec_price = bid
        ok, err = _validate_price("sell", signal.get("price", exec_price))
        if not ok:
            logger.warning(err)
            return {"status": "rejected", "reason": err}

        entry_fee = round(qty * exec_price * FEE_RATE, 8)
        position_manager.open_position("short", exec_price, qty, entry_fee)
        log_trade({
            "timestamp": time.time(),
            "symbol": SYMBOL,
            "side": "sell",
            "entry_price": exec_price,
            "exit_price": None,
            "qty": qty,
            "fee": entry_fee,
            "pnl": None,
            "reason": signal.get("reason", ""),
        })
        logger.info(f"[PAPER] SELL {qty} {SYMBOL} @ {exec_price} | Fee: {entry_fee:.6f} | {signal.get('reason')}")
        return {"status": "filled", "price": exec_price, "qty": qty, "fee": entry_fee}

    elif action == "close":
        pos_side: str = str(pos["side"])
        if pos_side == "none":
            return {"status": "skipped", "reason": "No open position to close"}

        close_side: str = "sell" if pos_side == "long" else "buy"
        close_price: float = bid if close_side == "sell" else ask

        pos_qty: float = float(str(pos["qty"]))
        exit_fee: float = round(pos_qty * close_price * FEE_RATE, 8)
        net_pnl: float
        net_pnl, _ = position_manager.close_position(close_price, exit_fee)

        pos_entry: float = float(str(pos["entry_price"]))
        log_trade({
            "timestamp": time.time(),
            "symbol": SYMBOL,
            "side": f"close_{pos_side}",
            "entry_price": pos_entry,
            "exit_price": close_price,
            "qty": pos_qty,
            "fee": exit_fee,
            "pnl": net_pnl,
            "reason": signal.get("reason", ""),
        })
        logger.info(f"[PAPER] CLOSE {pos_side} @ {close_price} | Fee: {exit_fee:.6f} | Net PnL: {net_pnl:.4f} | {signal.get('reason')}")
        return {"status": "closed", "price": close_price, "pnl": net_pnl, "fee": exit_fee}

    return {"status": "hold"}


# =============================================================================
# LIVE TRADING EXECUTION (Bybit REST API)
# =============================================================================
async def _paper_execute_loop(signal: Signal) -> None:
    """
    Async wrapper for paper trading to simulate latency and prevent blocking the thread.
    """
    # 1. Capture intended price instantly before network delay simulates slippage
    if "price" not in signal:
        action: str = signal.get("action", "hold")
        if action == "buy":
            signal["price"] = data_stream.best_ask.get("price", 0.0)
        elif action == "sell":
            signal["price"] = data_stream.best_bid.get("price", 0.0)
        elif action == "close":
            pos: dict[str, object] = position_manager.get_position()
            if pos["side"] == "long":
                signal["price"] = data_stream.best_bid.get("price", 0.0)
            elif pos["side"] == "short":
                signal["price"] = data_stream.best_ask.get("price", 0.0)

    # 1.5 Fetch QTY step scale
    step: float = await _get_qty_step()
    if "qty" not in signal:
        action = signal.get("action", "hold")
        if action == "close":
            pos = position_manager.get_position()
            signal["qty"] = float(str(pos["qty"]))
        else:
            price_val: float = signal.get("price", 0.0)
            raw_qty: float = (ORDER_SIZE_USDT * LEVERAGE) / price_val if price_val > 0 else 0
            signal["qty"] = _round_to_step(raw_qty, step)

    # 2. Simulate 50ms artificial network latency
    await asyncio.sleep(0.05)

    for attempt in range(1, MAX_RETRY + 1):
        result: OrderResult = _paper_execute(signal)
        if result.get("status") in ("filled", "closed", "hold", "skipped"):
            return
        logger.warning(f"Paper order attempt {attempt} failed: {result.get('reason')}")
        await asyncio.sleep(RETRY_DELAY_MS / 1000)


async def _live_place_order_async(signal: Signal) -> OrderResult:
    """
    Execute order via CCXT (Bybit REST API, async).
    CCXT menangani signing, header, dan parsing response secara otomatis.
    """
    action: str = signal["action"]
    pos: dict[str, object] = position_manager.get_position()

    ask: float = data_stream.best_ask.get("price", 0.0)
    bid: float = data_stream.best_bid.get("price", 0.0)

    # Tentukan side dan harga eksekusi
    ccxt_side: str
    price: float
    if action == "buy":
        ccxt_side, price = "buy", ask
    elif action == "sell":
        ccxt_side, price = "sell", bid
    elif action == "close":
        pos_side: str = str(pos["side"])
        if pos_side == "long":
            ccxt_side, price = "sell", bid
        elif pos_side == "short":
            ccxt_side, price = "buy", ask
        else:
            return {"status": "skipped", "reason": "No position to close"}
    else:
        return {"status": "hold"}

    step: float = await _get_qty_step()
    qty: float
    qty_val: Optional[float] = signal.get("qty")
    if qty_val is None:
        if action == "close":
            qty = float(str(pos["qty"]))
        else:
            raw_qty: float = (ORDER_SIZE_USDT * LEVERAGE) / price if price > 0 else 0
            qty = _round_to_step(raw_qty, step)
    else:
        qty = qty_val

    # Slippage check
    validate_side: str = action if action != "close" else ("sell" if str(pos["side"]) == "long" else "buy")
    ok: bool
    err: str
    ok, err = _validate_price(validate_side, price)
    if not ok:
        logger.warning(err)
        return {"status": "rejected", "reason": err}

    reduce_only: bool = (action == "close")

    for attempt in range(1, MAX_RETRY + 1):
        try:
            order = await exchange.create_order(
                symbol=ccxt_client.CCXT_SYMBOL,
                type="market",
                side=ccxt_side,
                amount=qty,
                params={
                    "reduceOnly":    reduce_only,
                    "timeInForce":   "GoodTillCancel",
                    "closeOnTrigger": False,
                },
            )
            order_id: str = str(order.get("id", ""))
            logger.info(f"[LIVE] Order placed: {ccxt_side} {qty} {SYMBOL} @ market | orderId={order_id}")
            return {"status": "filled", "price": price, "qty": qty, "orderId": order_id}

        except ccxt.BadRequest as e:
            # Fix 4: Bybit Hedge Mode menolak reduceOnly=True
            # → Retry tanpa flag agar order close tetap berhasil.
            if reduce_only and ("reduceOnly" in str(e) or "110025" in str(e)):
                logger.warning(
                    "[LIVE] reduceOnly ditolak Bybit (Hedge Mode terdeteksi). "
                    "Retry tanpa reduceOnly..."
                )
                reduce_only = False
                continue
            logger.warning(f"[LIVE] Order attempt {attempt} BadRequest: {e}")

        except Exception as e:
            logger.error(f"[LIVE] Order attempt {attempt} exception: {e}")

        await asyncio.sleep(RETRY_DELAY_MS / 1000)

    return {"status": "failed", "reason": "Max retries exceeded"}


# =============================================================================
# PUBLIC INTERFACE
# =============================================================================
def place_order(signal: Signal) -> None:
    """
    Main entry point for strategy to place orders.
    Routes to paper or live execution based on MODE config.
    Strategy MUST only call this function — no direct API access.

    signal keys:
        action: "buy" | "sell" | "close" | "hold"
        reason: str (optional description)
        qty:    float (optional, uses config default if omitted)
    """
    action: str = signal.get("action", "hold")
    if action == "hold":
        return

    logger.info(f"place_order → action={action} reason={signal.get('reason', '')}")

    if MODE == "paper":
        coro_paper = _paper_execute_loop(signal)
        # BUG-05 FIX: Simpan referensi task ke _background_tasks agar tidak di-GC
        # sebelum selesai. Callback `discard` otomatis membersihkan referensi setelah done.
        # Pattern ini adalah rekomendasi resmi Python asyncio docs untuk fire-and-forget tasks.
        try:
            loop = asyncio.get_running_loop()
            task_paper: asyncio.Task[None] = loop.create_task(coro_paper)
            _background_tasks.add(task_paper)
            task_paper.add_done_callback(_background_tasks.discard)
        except RuntimeError:
            asyncio.run(coro_paper)
    else:
        coro_live = _live_place_order_async(signal)
        try:
            loop = asyncio.get_running_loop()
            # _live_place_order_async returns OrderResult; wrap in a None-returning coroutine
            async def _fire_live(s: Signal = signal) -> None:
                await _live_place_order_async(s)
            task_live: asyncio.Task[None] = loop.create_task(_fire_live())
            _background_tasks.add(task_live)
            task_live.add_done_callback(_background_tasks.discard)
        except RuntimeError:
            asyncio.run(coro_live)
