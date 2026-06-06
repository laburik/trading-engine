
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
from decimal import ROUND_DOWN
from typing import Any, Optional
import ccxt.pro as ccxt
from config import (
    SYMBOL, MODE,
    ORDER_SIZE_USDT, LEVERAGE, SLIPPAGE_TOLERANCE,
    MAX_RETRY, RETRY_DELAY_MS, CANCEL_ON_PARTIAL, FEE_RATE,
)
from ccxt_client import exchange
import ccxt_client
from ft_types import OrderResult, Signal
from precision import to_decimal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [EXECUTION] %(levelname)s: %(message)s"
)
logger = logging.getLogger("execution")

# BUG-05 FIX: Set untuk menyimpan referensi task fire-and-forget.
# Tanpa ini, Python GC bisa menghapus task sebelum selesai dieksekusi.
_background_tasks: set[asyncio.Task[None]] = set()

# Duplicate-fire guard (demo/live): True selama 1 order live masih in-flight
# (sudah di-dispatch tapi belum konfirmasi + state belum ke-update). Mencegah
# tick 100ms berikutnya men-dispatch order kembar saat state belum sync.
# Lihat place_order() & _live_place_order_async() (bug 2026-05-29).
_live_order_in_flight: bool = False

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
    """
    Floor `value` ke kelipatan `step` (konservatif: jangan over-order).

    Pakai Decimal fixed-point (precision.to_decimal), BUKAN float — bug lama:
    floor(0.3 / 0.1) di float = floor(2.9999...) = 2 -> 0.2 (kehilangan 1 step
    penuh). Decimal: 0.3 / 0.1 = 3 -> 0.3. Lihat tests/test_precision.py.
    """
    if step <= 0:
        return value
    d = to_decimal(value)
    s = to_decimal(step)
    steps = (d / s).to_integral_value(rounding=ROUND_DOWN)
    return float(steps * s)


# =============================================================================
# ORDER FILL PARSER
# =============================================================================
def _parse_order_fill(
    order: dict,
    requested_qty: float,
) -> tuple[float, Optional[OrderResult]]:
    """
    Parse fill info dari CCXT order response — distinguish missing field vs zero fill.

    Bug yang di-prevent: `float(order.get("filled", qty) or qty)` membuat zero fill
    (silent reject dari matching engine) terlihat seperti full fill.

    Logika:
      - filled=None  + status closed/filled → fallback ke requested_qty + warn
      - filled=None  + status lain          → treat zero, return failure
      - filled=0     (explicit)             → SILENT REJECT, return failure
      - filled>0                            → ok, return value

    Returns:
        (filled_amount, error_result):
          error_result = None  → order ok, lanjut partial-fill detection
          error_result = dict  → caller harus return ini langsung
    """
    order_id: str = str(order.get("id", ""))
    order_status: str = str(order.get("status", "unknown")).lower()
    filled_raw: Any = order.get("filled")

    if filled_raw is None:
        # CCXT kadang tidak isi 'filled' kalau matching async (Bybit specifically).
        # Status optimistic = anggap order ok, PnL sync nanti reconcile:
        #   - "closed"/"filled"  → order tidak active lagi (fully filled atau cancel)
        #   - "none"             → Bybit demo quirk untuk market order async ack.
        #                          Response balik instant sebelum fill konfirmasi datang.
        #                          Kalau treat as failure, state lokal desync dari exchange
        #                          (bug yang ke-detect saat smoke test demo 2026-05-29).
        OPTIMISTIC_STATUSES = ("closed", "filled", "none")
        if order_status in OPTIMISTIC_STATUSES:
            if order_status == "none":
                logger.warning(
                    f"[LIVE] Order {order_id}: status='none' (async ack dari exchange). "
                    f"Asumsi full fill={requested_qty}. PnL sync akan verifikasi state."
                )
            else:
                logger.warning(
                    f"[LIVE] Order {order_id}: 'filled' missing tapi status='{order_status}'. "
                    f"Asumsi full fill={requested_qty}. Verifikasi via position_manager sync."
                )
            return requested_qty, None
        logger.error(
            f"[LIVE] Order {order_id}: 'filled' missing & status='{order_status}' "
            f"(bukan closed/filled/none). Treating as zero-fill failure."
        )
        return 0.0, {
            "status": "failed",
            "reason": f"No fill info from exchange (orderId={order_id}, status={order_status})",
        }

    # Defensive float conversion — kalau exchange return tipe aneh (str non-numeric,
    # nested dict, dll), jangan crash di hot path. Treat sebagai parse failure.
    try:
        filled: float = float(filled_raw)
    except (TypeError, ValueError) as e:
        logger.error(
            f"[LIVE] Order {order_id}: 'filled' value tidak numeric ({filled_raw!r}): {e}"
        )
        return 0.0, {
            "status": "failed",
            "reason": f"Invalid 'filled' value type (orderId={order_id})",
        }

    if filled <= 0.0:
        # Bahaya: orderId valid tapi tidak ada eksekusi — matching engine silent reject.
        # Sebelumnya `0 or qty` membuat bot mengira full fill — bug ini yang di-fix.
        logger.error(
            f"[LIVE] SILENT REJECT detected: order {order_id} status='{order_status}' "
            f"filled=0 (requested={requested_qty}). Return failed agar state "
            f"di-rekonsiliasi via position_manager sync."
        )
        return 0.0, {
            "status": "failed",
            "reason": f"Zero-fill silent reject (orderId={order_id}, status={order_status})",
        }

    return filled, None


# =============================================================================
# STALE DATA DETECTION
# =============================================================================
# Maksimum umur data best_bid/best_ask sebelum dianggap basi.
# WebSocket Bybit update orderbook setiap ~100ms saat aktif.
# 2 detik = 20× normal interval — kalau lebih, WS pasti bermasalah (disconnect).
# Threshold ini intentional konservatif: lebih baik miss 1-2 trade daripada
# eksekusi pakai harga yang sudah jauh bergerak.
STALE_DATA_THRESHOLD_SEC: float = 2.0

# Ambang staleness berbasis ts_event (waktu EXCHANGE). Lebih longgar dari
# STALE_DATA_THRESHOLD_SEC karena membandingkan jam KITA dengan jam EXCHANGE —
# sedikit clock skew itu wajar. Tujuannya menangkap feed yang "lag parah" (pesan
# tetap masuk sehingga waktu-terima fresh, TAPI isinya tua di sisi exchange) —
# kasus yang luput dari cek waktu-terima. Pola dua-timestamp ala Nautilus.
EXCHANGE_STALE_THRESHOLD_SEC: float = 5.0


def _check_stale_data() -> tuple[bool, str]:
    """
    Cek apakah best_bid/best_ask masih fresh. Dua lapis:
      1. Waktu-terima (ts_init): tangkap WS disconnect — data berhenti masuk.
      2. Waktu-event exchange (ts_event): tangkap feed lag — data masuk tapi tua.
    Return (True, "") kalau OK, (False, reason) kalau basi.
    """
    now: float = time.time()
    bid_ts: float = data_stream.best_bid_updated_at
    ask_ts: float = data_stream.best_ask_updated_at

    if bid_ts == 0.0 or ask_ts == 0.0:
        return False, "Market data not yet received (bid/ask timestamp = 0)"

    bid_age: float = now - bid_ts
    ask_age: float = now - ask_ts

    if bid_age > STALE_DATA_THRESHOLD_SEC:
        return False, f"Stale bid data: {bid_age:.2f}s old (threshold {STALE_DATA_THRESHOLD_SEC}s)"
    if ask_age > STALE_DATA_THRESHOLD_SEC:
        return False, f"Stale ask data: {ask_age:.2f}s old (threshold {STALE_DATA_THRESHOLD_SEC}s)"

    # Lapis 2: cek berbasis ts_event (waktu exchange). Hanya aktif kalau exchange
    # menyertakan timestamp (>0). getattr default 0.0 supaya aman bila atribut
    # belum ada (mis. stub data_stream lama di test).
    bid_event_ts: float = getattr(data_stream, "best_bid_ts_event", 0.0)
    if bid_event_ts > 0.0:
        event_age: float = now - bid_event_ts
        if event_age > EXCHANGE_STALE_THRESHOLD_SEC:
            return False, (
                f"Stale exchange data: bid ts_event {event_age:.2f}s old "
                f"(threshold {EXCHANGE_STALE_THRESHOLD_SEC}s) — feed lag / clock skew?"
            )

    return True, ""


# =============================================================================
# ORDERBOOK DEPTH-AWARE VWAP
# =============================================================================
def _calculate_vwap(side: str, qty: float) -> tuple[Optional[float], float, str]:
    """
    Walk orderbook untuk hitung VWAP (Volume Weighted Average Price) fill price
    untuk order size `qty`. Mensimulasikan apa yang akan dibayar saat market order
    menelusuri beberapa level orderbook.

    Args:
        side: "buy" → walk ASKs (ascending price)
              "sell" → walk BIDs (descending price)
        qty:  total quantity yang mau di-eksekusi

    Returns:
        (vwap, qty_fillable, error)
          vwap          : harga rata-rata weighted by quantity per level
          qty_fillable  : total qty yang bisa ter-fill dari book (≤ qty)
          error         : reason string kalau gagal, "" kalau OK

        Return (None, 0.0, reason) kalau book kosong / tidak ada liquidity.
    """
    if qty <= 0:
        return None, 0.0, f"Invalid qty: {qty}"

    book = data_stream.orderbook_snapshot
    if side == "buy":
        # ASKs ascending (harga terendah dulu — kita beli dari penjual paling murah)
        levels = list(book["asks"].items())
    elif side == "sell":
        # BIDs descending (harga tertinggi dulu — kita jual ke pembeli paling tinggi)
        levels = list(reversed(book["bids"].items()))
    else:
        return None, 0.0, f"Invalid side: {side}"

    if not levels:
        return None, 0.0, f"Orderbook {side} side empty"

    total_cost: float = 0.0
    total_qty: float = 0.0
    remaining: float = qty

    for price, level_qty in levels:
        take: float = min(level_qty, remaining)
        total_cost += take * price
        total_qty += take
        remaining -= take
        if remaining <= 1e-9:
            break

    if total_qty <= 0:
        return None, 0.0, "No liquidity available"

    vwap: float = total_cost / total_qty
    return vwap, total_qty, ""


# =============================================================================
# PRICE VALIDATION (stale check + VWAP slippage + liquidity check)
# =============================================================================
# Toleransi liquidity: order anggap OK kalau book bisa fill minimal X% dari qty.
# 0.99 = book minimal punya 99% liquidity yang dibutuhkan.
LIQUIDITY_THRESHOLD: float = 0.99


def _validate_price(side: str, intended_price: float, qty: float = 0.0) -> tuple[bool, str]:
    """
    Validasi pre-order. Reject kalau:
      1. Data bid/ask sudah basi (WS disconnect / stale)
      2. Liquidity di orderbook tidak cukup untuk qty
      3. VWAP slippage > SLIPPAGE_TOLERANCE vs intended_price

    Kalau qty=0 (legacy/paper mode tanpa hitung qty), skip check #2 dan #3,
    fallback ke best-bid/ask drift check seperti versi sebelumnya.
    """
    # --- 1. Stale data check ---
    ok, err = _check_stale_data()
    if not ok:
        return False, err

    # --- Kalau qty tidak disediakan, pakai best-level drift check (legacy behavior) ---
    if qty <= 0:
        ask: float = data_stream.best_ask.get("price", 0.0)
        bid: float = data_stream.best_bid.get("price", 0.0)
        if side == "buy":
            drift: float = (ask - intended_price) / intended_price if intended_price else 0
            if drift > SLIPPAGE_TOLERANCE:
                return False, f"Slippage exceeded BUY: live_ask={ask} vs intended={intended_price} drift={drift:.5f}"
        elif side == "sell":
            drift = (intended_price - bid) / intended_price if intended_price else 0
            if drift > SLIPPAGE_TOLERANCE:
                return False, f"Slippage exceeded SELL: live_bid={bid} vs intended={intended_price} drift={drift:.5f}"
        return True, ""

    # --- 2. VWAP & liquidity check (depth-aware) ---
    vwap: Optional[float]
    fillable: float
    vwap_err: str
    vwap, fillable, vwap_err = _calculate_vwap(side, qty)
    if vwap is None:
        return False, f"VWAP calc failed: {vwap_err}"

    if fillable < qty * LIQUIDITY_THRESHOLD:
        return False, (
            f"Insufficient liquidity for {side} {qty:.6f}: book only has "
            f"{fillable:.6f} ({fillable/qty*100:.1f}% of needed)"
        )

    # --- 3. VWAP slippage vs intended ---
    if side == "buy":
        drift = (vwap - intended_price) / intended_price if intended_price else 0
        if drift > SLIPPAGE_TOLERANCE:
            return False, (
                f"VWAP slippage exceeded BUY: vwap={vwap:.6f} vs "
                f"intended={intended_price:.6f} drift={drift*100:.3f}% "
                f"(tolerance {SLIPPAGE_TOLERANCE*100:.3f}%)"
            )
    elif side == "sell":
        drift = (intended_price - vwap) / intended_price if intended_price else 0
        if drift > SLIPPAGE_TOLERANCE:
            return False, (
                f"VWAP slippage exceeded SELL: vwap={vwap:.6f} vs "
                f"intended={intended_price:.6f} drift={drift*100:.3f}% "
                f"(tolerance {SLIPPAGE_TOLERANCE*100:.3f}%)"
            )

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
        ok, err = _validate_price("buy", signal.get("price", exec_price), qty)
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
        ok, err = _validate_price("sell", signal.get("price", exec_price), qty)
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


# Kode status pasar/simbol Bybit: simbol sedang TIDAK menerima order baru (atau
# hanya boleh reduce-only). Diatur exchange, BUKAN transient jaringan → retry
# cepat sia-sia. CCXT 4.5.x memetakan kode-kode ini ke KELAS exception BERBEDA
# (110074/110063 → ExchangeError, 110023/110042 → InvalidOrder), jadi dideteksi
# by-CODE dan dipanggil dari beberapa blok except — supaya tahan kalau pemetaan
# kelas CCXT berubah antar versi.
_MARKET_STATUS_CODES: tuple[str, ...] = ("110074", "110063", "110023", "110042")


def _market_status_reject(err_str: str) -> Optional[OrderResult]:
    """Return OrderResult 'rejected' (no-retry) kalau error dari status pasar
    khusus (not-live/settlement/pre-delivery/reduce-only); selain itu None."""
    if any(code in err_str for code in _MARKET_STATUS_CODES):
        logger.error(
            f"[LIVE] Order ditolak: simbol status khusus "
            f"(not-live/settlement/pre-delivery/reduce-only) — tidak retry: {err_str[:120]}"
        )
        return {"status": "rejected", "reason": f"Market status restricted: {err_str[:100]}"}
    return None


async def _live_place_order_async(signal: Signal) -> OrderResult:
    """
    Execute order via CCXT (Bybit REST API, async).
    CCXT menangani signing, header, dan parsing response secara otomatis.

    Error handling kategorisasi:
      - reduceOnly rejected (Hedge mode) → retry tanpa flag
      - Insufficient balance/margin    → NO RETRY (fail fast, retry tidak akan fix)
      - Order size invalid (min/max)    → NO RETRY (config issue, butuh intervensi)
      - Insufficient liquidity          → NO RETRY (book tidak cukup, retry sia-sia)
      - Rate limit (429)                → RETRY dengan backoff lebih panjang
      - Network/timeout/generic        → RETRY normal (default 20ms delay)
      - Partial fill                    → kalau CANCEL_ON_PARTIAL=True, cancel sisa
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

    # Zero-qty guard (pola make_qty Nautilus: qty nol = invalid). Cegah kirim order
    # ukuran 0 ke exchange — fail fast dengan rejection bersih, bukan dipantulkan
    # exchange (atau lebih buruk: terkirim sebagai 0 dan bikin state aneh).
    if qty <= 0:
        logger.error(
            f"[LIVE] Order {action} di-reject: qty membulat ke nol (raw size terlalu "
            f"kecil untuk step). Cek ORDER_SIZE_USDT/LEVERAGE vs harga."
        )
        return {"status": "rejected", "reason": "Order size rounds to zero"}

    # Pre-order validation: stale data + VWAP slippage + liquidity (depth-aware)
    validate_side: str = action if action != "close" else ("sell" if str(pos["side"]) == "long" else "buy")
    ok: bool
    err: str
    ok, err = _validate_price(validate_side, price, qty)
    if not ok:
        logger.warning(f"[LIVE] Pre-order validation rejected: {err}")
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

            # Parse fill — return failure kalau silent-reject (filled=0) atau
            # status ambiguous. JANGAN pretend full-fill seperti bug lama
            # (`filled or qty` → 0 or qty → qty meskipun aslinya tidak ter-fill).
            filled: float
            parse_err: Optional[OrderResult]
            filled, parse_err = _parse_order_fill(order, qty)
            if parse_err is not None:
                return parse_err

            # Partial fill detection — Bybit market order biasanya full-fill,
            # tapi di market thin atau saat volatil bisa partial.
            if filled < qty * 0.999:  # toleransi 0.1% untuk rounding
                shortfall_pct: float = (1 - filled / qty) * 100
                logger.warning(
                    f"[LIVE] PARTIAL FILL: requested={qty:.6f}, "
                    f"filled={filled:.6f} ({shortfall_pct:.2f}% short) | orderId={order_id}"
                )
                if CANCEL_ON_PARTIAL:
                    # Untuk market order biasanya tidak ada sisa pending, tapi
                    # kalau ada (limit IOC future-proof), cancel sisa eksplisit.
                    try:
                        await exchange.cancel_order(order_id, ccxt_client.CCXT_SYMBOL)
                        logger.info(f"[LIVE] Cancelled unfilled remainder of order {order_id}")
                    except Exception as cancel_err:
                        logger.warning(f"[LIVE] Cancel remainder failed (likely already done): {cancel_err}")

            logger.info(f"[LIVE] Order placed: {ccxt_side} {filled} {SYMBOL} @ market | orderId={order_id}")

            # Tutup race window duplicate-fire: update state posisi lokal SEKARANG,
            # tanpa nunggu _sync_position (~PNL_SYNC_INTERVAL detik). Tanpa ini, tick
            # berikutnya masih lihat state lama → strategy fire order kembar. Money
            # accounting tetap milik sync loop (lihat position_manager.set_position_optimistic).
            if action == "close":
                # Hanya clear kalau fill efektif penuh. Partial close (jarang untuk
                # market reduce-only) dibiarkan agar _sync_position update qty sisa.
                if filled >= qty * 0.999:
                    position_manager.clear_position_optimistic()
            else:
                position_manager.set_position_optimistic(
                    "long" if action == "buy" else "short", price, filled
                )
            return {"status": "filled", "price": price, "qty": filled, "orderId": order_id}

        except ccxt.BadRequest as e:
            err_str: str = str(e)

            # Hedge mode reduceOnly fallback (sudah ada sebelumnya)
            if reduce_only and ("reduceOnly" in err_str or "110025" in err_str):
                logger.warning(
                    "[LIVE] reduceOnly ditolak Bybit (Hedge Mode terdeteksi). "
                    "Retry tanpa reduceOnly..."
                )
                reduce_only = False
                continue

            # NO RETRY errors — kondisi yang tidak akan fix dengan retry
            # Insufficient balance/margin: butuh tambah modal, bukan masalah transient
            if any(code in err_str for code in ("110007", "110021", "insufficient", "Insufficient")):
                logger.error(f"[LIVE] FATAL: Insufficient balance/margin — order tidak dieksekusi: {e}")
                return {"status": "rejected", "reason": f"Insufficient balance: {err_str[:100]}"}

            # Order size invalid: min/max size, leverage mismatch — butuh fix config
            if any(code in err_str for code in ("110013", "110014", "110028", "110045", "min order")):
                logger.error(f"[LIVE] FATAL: Order size/leverage invalid — cek config: {e}")
                return {"status": "rejected", "reason": f"Invalid order params: {err_str[:100]}"}

            # Insufficient liquidity untuk market order
            if "170131" in err_str or "liquidity" in err_str.lower():
                logger.warning(f"[LIVE] Bybit reject: insufficient liquidity — book thin")
                return {"status": "rejected", "reason": f"Insufficient liquidity at Bybit"}

            # Status pasar/simbol (lihat _market_status_reject). Dicek di sini juga
            # sebagai pengaman kalau CCXT versi lain memetakan kode ini ke BadRequest.
            market_reject: Optional[OrderResult] = _market_status_reject(err_str)
            if market_reject is not None:
                return market_reject

            # BadRequest lain — retry sekali, mungkin transient
            logger.warning(f"[LIVE] Order attempt {attempt} BadRequest: {e}")

        except ccxt.InvalidOrder as e:
            err_str = str(e)
            # Status pasar/simbol — CCXT memetakan 110023/110042 ke InvalidOrder.
            market_reject = _market_status_reject(err_str)
            if market_reject is not None:
                return market_reject
            # 110017 = "current position is zero, cannot fix reduce-only order qty"
            # → Bot lokal state desync: kira ada posisi, exchange bilang zero.
            #   Retry sia-sia karena state lokal gak berubah sampai PnL sync next cycle (~2s).
            #   Bug ke-detect saat smoke test demo 2026-05-29 — spam 3x retry tanpa hasil.
            if "110017" in err_str or "current position is zero" in err_str.lower():
                logger.warning(
                    f"[LIVE] Order rejected: position di exchange sudah zero (110017). "
                    f"Local state desync — clear optimistic + PnL sync reconcile. Tidak retry."
                )
                # Exchange konfirmasi posisi zero → clear state lokal sekarang juga,
                # supaya strategy tidak terus evaluasi posisi hantu sampai sync jalan.
                position_manager.clear_position_optimistic()
                return {"status": "rejected", "reason": "Position desync: exchange shows zero — handled by sync"}
            # InvalidOrder lain (110010 leverage not modified, dll) — config issue, no retry
            logger.error(f"[LIVE] FATAL: InvalidOrder — tidak retry: {e}")
            return {"status": "rejected", "reason": f"InvalidOrder: {err_str[:100]}"}

        except ccxt.RateLimitExceeded as e:
            # CCXT auto-throttle sudah on (enableRateLimit=True), kalau masih kena
            # berarti burst → tunggu lebih lama sebelum retry
            backoff: float = (RETRY_DELAY_MS / 1000) * (2 ** attempt)
            logger.warning(f"[LIVE] Rate limit hit (attempt {attempt}). Backoff {backoff:.2f}s: {e}")
            await asyncio.sleep(backoff)
            continue

        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, asyncio.TimeoutError) as e:
            logger.warning(f"[LIVE] Network/exchange unavailable (attempt {attempt}): {type(e).__name__}: {e}")
            # Retry normal — kemungkinan transient

        except ccxt.AuthenticationError as e:
            logger.error(f"[LIVE] FATAL: Authentication error — cek API key di .env: {e}")
            return {"status": "rejected", "reason": "Authentication failed — invalid API key"}

        except ccxt.ExchangeError as e:
            # ExchangeError MENTAH (bukan subclass spesifik di atas). CCXT memetakan
            # 110074/110063 (not-live/settlement) ke sini → fail-fast tanpa retry.
            # ExchangeError lain diperlakukan transient (retry) seperti sebelumnya.
            err_str = str(e)
            market_reject = _market_status_reject(err_str)
            if market_reject is not None:
                return market_reject
            logger.warning(f"[LIVE] Order attempt {attempt} ExchangeError: {type(e).__name__}: {e}")

        except Exception as e:
            logger.error(f"[LIVE] Order attempt {attempt} unhandled exception {type(e).__name__}: {e}")

        await asyncio.sleep(RETRY_DELAY_MS / 1000)

    return {"status": "failed", "reason": f"Max retries ({MAX_RETRY}) exceeded"}


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
        global _live_order_in_flight
        # Duplicate-fire guard: selama 1 order live masih in-flight (belum konfirmasi
        # + state belum ke-update), JANGAN dispatch order baru. Tanpa ini, tick 100ms
        # berikutnya masih lihat state lama → kirim order kembar (bug 2026-05-29).
        if _live_order_in_flight:
            logger.warning(
                f"[LIVE] Order {action} di-skip — order sebelumnya masih in-flight. "
                f"Mencegah duplicate fire (state belum sync)."
            )
            return

        # _live_place_order_async returns OrderResult; wrap in a None-returning coroutine
        # dan reset flag in-flight di finally (sukses, gagal, maupun exception).
        async def _fire_live(s: Signal = signal) -> None:
            global _live_order_in_flight
            try:
                await _live_place_order_async(s)
            finally:
                _live_order_in_flight = False

        try:
            loop = asyncio.get_running_loop()
            _live_order_in_flight = True
            task_live: asyncio.Task[None] = loop.create_task(_fire_live())
            _background_tasks.add(task_live)
            task_live.add_done_callback(_background_tasks.discard)
        except RuntimeError:
            # Tidak ada event loop berjalan (mis. unit test / sync context) — jalankan inline.
            _live_order_in_flight = True
            try:
                asyncio.run(_live_place_order_async(signal))
            finally:
                _live_order_in_flight = False
