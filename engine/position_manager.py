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
import math
import asyncio
import logging
from typing import Any, Literal, Union, cast
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


# =============================================================================
# OPTIMISTIC LOCAL STATE (demo/live) — tutup race window sebelum _sync_position
# =============================================================================
# Di mode demo/live, _state hanya di-update tiap PNL_SYNC_INTERVAL detik oleh
# start_pnl_sync_loop(). execution._live_place_order_async() TIDAK menyentuh
# state (beda dari paper mode yang panggil open_position/close_position langsung).
# Akibatnya ada window (sampai ~PNL_SYNC_INTERVAL detik) di mana strategy tick
# berikutnya masih lihat side="none" walaupun order sudah ter-fill → bot kirim
# order DUPLIKAT (bug ke-detect smoke test demo 2026-05-29).
#
# Dua fungsi di bawah dipanggil execution.py SETELAH fill terkonfirmasi untuk
# update HANYA field posisi (side/entry/qty) secara optimistic. Akunting uang
# (balance, realized_pnl_total, total_fees) tetap MILIK sync loop dari data
# exchange yang authoritative — sengaja TIDAK disentuh di sini supaya tidak
# double-count. _sync_position() merekonsiliasi entry_price & qty exact nanti.
def set_position_optimistic(side: str, entry_price: float, qty: float) -> None:
    """
    Demo/live only: catat posisi baru secara optimistic setelah fill konfirmasi,
    tanpa mengubah balance/pnl/fee (itu domain sync loop).

    Idempotent terhadap sync: kalau _sync_position sudah update state lebih dulu,
    pemanggilan ini hanya menimpa field posisi dengan nilai yang konsisten.
    """
    _state["side"] = side
    _state["entry_price"] = entry_price
    _state["qty"] = qty
    _state["unrealized_pnl"] = 0.0  # baru di-fill @ entry → unrealized ~0; sync recompute
    if _state["open_time"] is None:
        _state["open_time"] = time.time()
    logger.info(
        f"[OPTIMISTIC] Position set: {side.upper()} {qty} @ {entry_price} "
        f"(sync akan verifikasi state ke exchange)"
    )


def clear_position_optimistic() -> None:
    """
    Demo/live only: bersihkan posisi secara optimistic setelah close fill penuh
    terkonfirmasi (atau saat exchange konfirmasi posisi sudah zero, mis. reject
    110017). Tidak menyentuh balance/realized_pnl — sync loop yang catat PnL
    realized dari data exchange.
    """
    if _state["side"] == "none":
        return  # sudah bersih (mungkin sync sudah keburu jalan)
    logger.info(
        f"[OPTIMISTIC] Position cleared (was {_state['side']}) — sync akan verifikasi"
    )
    _state["side"] = "none"
    _state["entry_price"] = 0.0
    _state["qty"] = 0.0
    _state["unrealized_pnl"] = 0.0
    _state["open_time"] = None


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
# Konstanta untuk tracking sync health
SYNC_FAIL_WARN_THRESHOLD: int = 3   # log error setelah N kali gagal berturut

# Counter konsekutif failure (state visible untuk dashboard nanti)
_consecutive_position_sync_failures: int = 0
_consecutive_balance_sync_failures: int = 0


# Parser output: 3 outcomes mutually exclusive.
# Pure function — pisahkan parsing dari side-effect supaya unit-testable.
ParsedPositionUpdate = tuple[
    Literal["update"],
    tuple[Literal["long", "short"], float, float, float],  # side, entry, qty, unrealized
]
ParsedPositionClear = tuple[Literal["clear"], None]
ParsedPositionSkip = tuple[Literal["skip"], str]  # reason
ParsedPositionResult = Union[ParsedPositionUpdate, ParsedPositionClear, ParsedPositionSkip]


def _parse_position_response(positions: list) -> ParsedPositionResult:
    """
    Parse CCXT fetch_positions response — validate sebelum overwrite state lokal.

    Return value (status, payload):
      ("clear", None)
          Tidak ada posisi aktif di exchange — caller harus reset state ke none.
      ("update", (side, entry_price, qty, unrealized_pnl))
          Posisi valid — caller harus overwrite state secara atomic.
      ("skip", reason)
          Data exchange invalid/incomplete — caller TIDAK BOLEH ubah state lokal
          (mencegah corruption seperti qty>0 + entry_price=0 yang bikin PnL infinite).
    """
    # Kumpulkan posisi yang punya 'contracts' valid > 0.
    # Skip record dengan contracts None/non-numeric (incomplete dari API).
    active: list[dict] = []
    for p in positions:
        if not isinstance(p, dict):
            continue
        contracts_raw: Any = p.get("contracts")
        if contracts_raw is None:
            continue
        try:
            contracts_val: float = float(contracts_raw)
        except (TypeError, ValueError):
            continue
        if contracts_val > 0:
            active.append(p)

    if not active:
        return ("clear", None)

    # Take first active position (kita asumsi 1-symbol-1-side; untuk hedge mode
    # multi-sided perlu logic terpisah — di luar scope).
    pos: dict = active[0]
    side_raw: str = str(pos.get("side", "")).lower()
    if side_raw not in ("long", "short"):
        return ("skip", f"Invalid 'side'={side_raw!r} on active position")

    # Validasi entryPrice WAJIB ada — kalau missing/None, state lama lebih aman
    # daripada entry_price=0 yang bikin PnL calc rusak total.
    entry_raw: Any = pos.get("entryPrice")
    if entry_raw is None:
        return ("skip", "Active position missing 'entryPrice' field — keeping previous state")
    try:
        entry_price: float = float(entry_raw)
    except (TypeError, ValueError):
        return ("skip", f"'entryPrice' not numeric: {entry_raw!r}")
    # isfinite WAJIB: float('inf'/'nan') lolos cek `<= 0` (inf>0, nan banding apa pun
    # False) → entry_price non-finite bisa corrupt state + bikin PnL inf/nan.
    if not math.isfinite(entry_price) or entry_price <= 0:
        return ("skip", f"'entryPrice'={entry_price} not finite/positive")

    # contracts sudah divalidasi di filter di atas
    contracts_raw = pos.get("contracts")
    try:
        qty: float = float(contracts_raw)
    except (TypeError, ValueError):
        return ("skip", f"'contracts' not numeric: {contracts_raw!r}")
    if not math.isfinite(qty) or qty <= 0:
        return ("skip", f"'contracts'={qty} not finite/positive")

    # unrealizedPnl optional — default 0 kalau tidak ada / invalid (cosmetic field)
    unrealized_raw: Any = pos.get("unrealizedPnl")
    try:
        unrealized: float = float(unrealized_raw) if unrealized_raw is not None else 0.0
    except (TypeError, ValueError):
        unrealized = 0.0

    side_lit: Literal["long", "short"] = "long" if side_raw == "long" else "short"
    return ("update", (side_lit, entry_price, qty, unrealized))


# Parser balance: distinguish "tidak ada field" (skip) vs "field = 0" (update).
# Bug lama: `usdt.get("total") or _state["balance"]` membuat balance=0 (user kehabisan
# modal) jatuh ke balance lama → loss tersembunyi.
ParsedBalanceUpdate = tuple[Literal["update"], float]
ParsedBalanceSkip = tuple[Literal["skip"], str]
ParsedBalanceResult = Union[ParsedBalanceUpdate, ParsedBalanceSkip]


def _parse_balance_response(balance: dict) -> ParsedBalanceResult:
    """
    Parse CCXT fetch_balance response — distinguish missing field vs explicit 0.

    Bug yang di-prevent: `float(usdt.get("total") or fallback)` membuat balance=0
    (user habis margin) jatuh ke fallback → kerugian tersembunyi dari dashboard.
    """
    if not isinstance(balance, dict):
        return ("skip", f"balance response bukan dict: {type(balance).__name__}")

    usdt: Any = balance.get("USDT")
    if not isinstance(usdt, dict):
        return ("skip", "USDT entry missing in balance response")

    total_raw: Any = usdt.get("total")
    if total_raw is None:
        return ("skip", "'total' field missing in USDT balance")

    try:
        total: float = float(total_raw)
    except (TypeError, ValueError):
        return ("skip", f"'total' not numeric: {total_raw!r}")

    if total < 0:
        # Balance negatif tidak masuk akal (margin call?) — minta investigasi manual
        return ("skip", f"'total'={total} negative (margin call?), not updating state")

    return ("update", total)


async def _sync_position() -> None:
    """
    Fetch open position dari Bybit via CCXT dan update local state.

    Error policy:
      - API/network error  → state TIDAK disentuh, counter naik, log warning,
                              eskalasi ke error log kalau > SYNC_FAIL_WARN_THRESHOLD
      - Response invalid    → state TIDAK disentuh, log error (data quality)
      - No active position  → clear state ke none
      - Valid active        → atomic update semua field sekaligus
    """
    global _consecutive_position_sync_failures
    try:
        positions = await exchange.fetch_positions([ccxt_client.CCXT_SYMBOL])
    except Exception as e:
        _consecutive_position_sync_failures += 1
        msg = (f"[PNL SYNC] Position fetch failed (#{_consecutive_position_sync_failures}): "
               f"{type(e).__name__}: {e}. State NOT updated — keeping previous values.")
        if _consecutive_position_sync_failures >= SYNC_FAIL_WARN_THRESHOLD:
            logger.error(msg + " Check connectivity/auth — local view may be stale.")
        else:
            logger.warning(msg)
        return

    if not isinstance(positions, list):
        logger.error(f"[PNL SYNC] fetch_positions() returned {type(positions).__name__}, expected list. State unchanged.")
        return

    status, payload = _parse_position_response(positions)

    if status == "skip":
        logger.error(f"[PNL SYNC] Position response invalid: {payload}. State unchanged.")
        return

    if status == "clear":
        # Reset semua field posisi (termasuk entry_price yang sebelumnya tidak di-clear).
        if _state["side"] != "none":
            logger.info("[PNL SYNC] No open position at exchange — clearing local state.")
        _state["side"]           = "none"
        _state["entry_price"]    = 0.0
        _state["qty"]            = 0.0
        _state["unrealized_pnl"] = 0.0
        _consecutive_position_sync_failures = 0
        return

    # status == "update" — cast karena pyright tidak narrow union dari status check
    update_tuple = cast("tuple[Literal['long', 'short'], float, float, float]", payload)
    side_lit, entry_price, qty, unrealized = update_tuple
    _state["side"]           = side_lit
    _state["entry_price"]    = entry_price
    _state["qty"]            = qty
    _state["unrealized_pnl"] = unrealized
    _consecutive_position_sync_failures = 0


async def _sync_balance() -> None:
    """
    Fetch wallet balance dari Bybit via CCXT dan update local state.

    Error policy sama dengan _sync_position — API error tidak overwrite,
    response 0 yang valid dipakai apa adanya (jangan disembunyikan).
    """
    global _consecutive_balance_sync_failures
    try:
        balance = await exchange.fetch_balance()
    except Exception as e:
        _consecutive_balance_sync_failures += 1
        msg = (f"[PNL SYNC] Balance fetch failed (#{_consecutive_balance_sync_failures}): "
               f"{type(e).__name__}: {e}. Keeping previous balance.")
        if _consecutive_balance_sync_failures >= SYNC_FAIL_WARN_THRESHOLD:
            logger.error(msg + " Check connectivity/auth.")
        else:
            logger.warning(msg)
        return

    status, payload = _parse_balance_response(balance)
    if status == "skip":
        logger.error(f"[PNL SYNC] Balance response invalid: {payload}. State unchanged.")
        return

    # status == "update" — termasuk total=0 (user habis margin) dipakai apa adanya
    _state["balance"] = cast(float, payload)
    _consecutive_balance_sync_failures = 0


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
