# =============================================================================
# main.py — Bot Entry Point (ties all modules together)
# =============================================================================
#
# Run:  python main.py
#
# This orchestrates:
#   1. data_stream     — WebSocket orderbook + funding rate + heartbeat
#   2. candle_stream   — Bybit native Kline WebSocket  (if DATA_MODE="kline")
#      OR data_resampler — Tick-by-tick candle builder (if DATA_MODE="tick")
#   3. strategy        — Signal generation + trade execution
#   4. position_manager — PnL updates per tick
# =============================================================================
from __future__ import annotations

import asyncio
import logging
import signal as _signal
import sys
import time as _time

# Force UTF-8 output on Windows terminals (prevents UnicodeEncodeError from emoji)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from typing import TYPE_CHECKING, Optional
from config import SYMBOL, MODE, TIMEFRAMES, DATA_MODE
from ccxt_client import init_exchange, close_exchange
from ft_types import MarketDataSnapshot

# --- Selalu import ---
import data_stream
import position_manager
import strategy

# --- Import modul data sesuai mode ---
# We use an untyped alias; mypy ignores the conditional re-import via type: ignore.
if DATA_MODE == "kline":
    import candle_stream as active_data  # type: ignore[assignment]
    _data_label: str = "KLINE"
else:
    import data_resampler as active_data  # type: ignore[assignment,no-redef]
    _data_label = "TICK"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MAIN] %(levelname)s: %(message)s"
)
logger = logging.getLogger("main")

# (Interval map dihapus — CCXT menerima label timeframe langsung, mis. "15m", "2h")


# =============================================================================
# PRELOAD: Fetch historical klines via CCXT
# =============================================================================
async def preload_all_timeframes() -> None:
    """
    Unduh histori candle via CCXT fetch_ohlcv, masukkan ke buffer modul aktif
    (candle_stream atau data_resampler, tergantung DATA_MODE).
    Selesai → set is_warmup = False di modul aktif.
    """
    from ccxt_client import exchange, CCXT_SYMBOL
    logger.info(f"Preloading historical candles (DATA_MODE={DATA_MODE})...")

    for tf, (interval_sec, max_candles) in TIMEFRAMES.items():
        if interval_sec < 60:
            logger.info(f"[PRELOAD] [{tf}] Skipped (sub-minute tidak tersedia via kline API)")
            continue

        try:
            # CCXT menerima label TF langsung ("1m", "15m", "2h") — tidak perlu mapping manual
            limit: int = min(max_candles + 1, 200)  # +1 cadangan untuk strip open candle
            ohlcv: list[list] = await exchange.fetch_ohlcv(CCXT_SYMBOL, timeframe=tf, limit=limit)

            if not ohlcv:
                logger.warning(f"[PRELOAD] [{tf}] Empty response — skipped")
                continue

            # Strip candle yang masih terbuka (candle terbaru belum ditutup)
            # CCXT mengembalikan data oldest-first, jadi candle terbuka ada di indeks [-1]
            newest_ts_ms: int = int(ohlcv[-1][0])
            if newest_ts_ms + interval_sec * 1000 > int(_time.time() * 1000):
                ohlcv = ohlcv[:-1]
                logger.info(f"[PRELOAD] [{tf}] Stripped currently open candle (not yet closed)")

            # CCXT format: [[ts_ms, open, high, low, close, volume], ...]
            # Kompatibel langsung dengan preload_candles() di kedua modul
            loaded: int = active_data.preload_candles(tf, ohlcv)
            logger.info(f"[PRELOAD] [{tf}] {loaded} closed candles loaded (timeframe={tf})")

        except asyncio.TimeoutError:
            logger.error(f"[PRELOAD] [{tf}] Request timed out — skipped")
        except Exception as e:
            logger.error(f"[PRELOAD] [{tf}] Unexpected error: {e} — skipped")

    # Lepas flag warmup di modul aktif
    active_data.is_warmup = False
    logger.info(f"Preload complete. Bot is now LIVE [{_data_label} mode] — strategy may generate signals.")


# =============================================================================
# STRATEGY LOOP
# =============================================================================
async def strategy_loop() -> None:
    """
    Loop utama strategi.
    - Mode TICK : trigger setiap ada tick baru di tick_buffer.
    - Mode KLINE: trigger setiap ada candle baru (cek perubahan buffer ukuran).
    """
    logger.info(f"Strategy loop started. Mode: {MODE.upper()} | Symbol: {SYMBOL} | Data: {_data_label}")

    if DATA_MODE == "tick":
        # ---- TICK MODE: trigger per tick ----
        while True:
            if data_stream.new_tick_event is not None:
                await data_stream.new_tick_event.wait()
                data_stream.new_tick_event.clear()
            else:
                await asyncio.sleep(0.001)

            bid: float = data_stream.best_bid.get("price", 0.0)
            ask: float = data_stream.best_ask.get("price", 0.0)
            if bid > 0 and ask > 0:
                position_manager.update_pnl(bid, ask)

            live_data: MarketDataSnapshot = active_data.get_live_data()  # type: ignore[assignment]
            try:
                strategy.on_tick(live_data)
            except Exception as e:
                logger.error(f"Strategy error: {e}", exc_info=True)

            await asyncio.sleep(0)

    else:
        # ---- KLINE MODE: trigger saat candle baru tutup ATAU live candle berubah ----
        # BUG-07 FIX: total=sum(len(buf)) tidak berubah saat buffer maxlen tercapai
        # (deque buang yang lama, panjang tetap). Ganti ke tracking open_time candle
        # terakhir per TF agar trigger tetap aktif setelah preload penuh.
        _last_closed_times: dict[str, float] = {
            tf: (buf[-1]["open_time"] if buf else 0.0)
            for tf, buf in active_data.candle_buffers.items()
        }
        last_current_hash: int = -1  # -1 agar iterasi pertama selalu trigger
        while True:
            # Deteksi candle baru tutup: cek open_time candle terakhir per TF
            new_candle_closed = False
            for tf, buf in active_data.candle_buffers.items():
                if buf:
                    t: float = buf[-1]["open_time"]
                    if t != _last_closed_times.get(tf, 0.0):
                        _last_closed_times[tf] = t
                        new_candle_closed = True

            # Deteksi live candle berubah (update harga intra-candle)
            current_hash: int = hash(tuple(
                (tf, c["open_time"] if c else 0, round(c["close"], 8) if c else 0)
                for tf, c in active_data._current_candle.items()
            ) if hasattr(active_data, "_current_candle") else 0)

            bid = data_stream.best_bid.get("price", 0.0)
            ask = data_stream.best_ask.get("price", 0.0)

            if bid > 0 and ask > 0:
                position_manager.update_pnl(bid, ask)

            if new_candle_closed or current_hash != last_current_hash:
                last_current_hash = current_hash
                live_data_kline: MarketDataSnapshot = active_data.get_live_data()  # type: ignore[assignment]
                try:
                    strategy.on_tick(live_data_kline)
                except Exception as e:
                    logger.error(f"Strategy error: {e}", exc_info=True)

            await asyncio.sleep(0.1)   # polling tiap 100ms (ringan)


# =============================================================================
# GRACEFUL SHUTDOWN
# =============================================================================
_shutdown_event: asyncio.Event = asyncio.Event()


def _handle_signal(sig: int, frame: object) -> None:
    logger.info(f"Received signal {sig}. Shutting down gracefully...")
    _shutdown_event.set()


# =============================================================================
# MAIN
# =============================================================================
async def main() -> None:
    logger.info("=" * 60)
    logger.info(f"  TRADING BOT STARTING")
    logger.info(f"  Symbol    : {SYMBOL}")
    logger.info(f"  Mode      : {MODE.upper()}")
    logger.info(f"  Data Mode : {_data_label} ({'Bybit Kline WS' if DATA_MODE == 'kline' else 'Tick-by-Tick Resampler'})")
    if MODE == "paper":
        logger.info(f"  PnL       : Simulasi lokal (bid/ask based)")
    else:
        logger.info(f"  PnL       : Sync dari Bybit setiap {__import__('config').PNL_SYNC_INTERVAL}s")
    logger.info("=" * 60)

    # ── CCXT INIT ──────────────────────────────────────────────────────────────
    # Muat market info dari Bybit (diperlukan oleh execution, position_manager, preload)
    await init_exchange()
    # ───────────────────────────────────────────────────────────────────────────

    # ── PRE-FLIGHT CHECK ────────────────────────────────────────────────────
    # Validasi sintaks, file, library, config, dan strategy sebelum bot jalan.
    # Jika ada error apapun, bot langsung berhenti + tampilkan daftar masalah.
    import preflight_check
    if not preflight_check.run_and_print():
        sys.exit(1)
    # ────────────────────────────────────────────────────────────────────────

    # ── WATCHDOG ─────────────────────────────────────────────────────────────
    # Mulai watchdog thread yang memantau apakah strategy masih dipanggil rutin.
    # Jika tidak ada aktivitas > 2 menit, status bot berubah ke FROZEN di dashboard.
    import bot_monitor
    bot_monitor.start_watchdog()
    logger.info("Bot monitor watchdog started.")
    # ─────────────────────────────────────────────────────────────────────────

    for sig in (getattr(_signal, "SIGTERM", None), getattr(_signal, "SIGINT", None)):
        if sig:
            _signal.signal(sig, _handle_signal)

    # Preload candle historis
    await preload_all_timeframes()

    # OLD-09 FIX: Lakukan initial sync balance/posisi dari Bybit sebelum strategy loop
    # dimulai. Tanpa ini, balance di paper mode = INITIAL_BALANCE (1000 USDT) tapi di
    # demo/live balance asli Bybit bisa sangat berbeda — kalkulasi order size jadi salah
    # di window pertama sebelum sync pertama dari start_pnl_sync_loop() selesai.
    if MODE in ("demo", "live"):
        await position_manager.initial_sync()
        logger.info("Initial balance/position sync completed before strategy starts.")

    # Susun task berdasarkan DATA_MODE
    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(data_stream.start_data_stream(), name="data_stream"),
        asyncio.create_task(strategy_loop(), name="strategy"),
    ]

    if DATA_MODE == "kline":
        import candle_stream
        tasks.append(asyncio.create_task(candle_stream.start_candle_stream(), name="candle_stream"))
    else:
        import data_resampler
        tasks.append(asyncio.create_task(data_resampler.start_resampler(), name="resampler"))

    if MODE in ("demo", "live"):
        tasks.append(asyncio.create_task(position_manager.start_pnl_sync_loop(), name="pnl_sync"))

    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for t in done:
            if t.exception():
                logger.error(f"Task '{t.get_name()}' raised: {t.exception()}")
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Cancelling remaining tasks...")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        from trade_logger import shutdown as logger_shutdown
        logger_shutdown()
        await close_exchange()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[MAIN] Keyboard interrupt — bot stopped.")
        sys.exit(0)
