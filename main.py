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
import aiohttp
from config import SYMBOL, MODE, REST_MARKET_URL, TIMEFRAMES, DATA_MODE
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

# =============================================================================
# BYBIT INTERVAL LABEL MAP
# =============================================================================
_BYBIT_INTERVAL_MAP: dict[int, str] = {
    60:    "1",
    180:   "3",
    300:   "5",
    900:   "15",
    1800:  "30",
    3600:  "60",
    7200:  "120",
    14400: "240",
    21600: "360",
    43200: "720",
    86400: "D",
}


# =============================================================================
# PRELOAD: Fetch historical klines from Bybit REST
# =============================================================================
async def preload_all_timeframes() -> None:
    """
    Unduh histori candle dari Bybit REST, masukkan ke buffer modul aktif
    (candle_stream atau data_resampler, tergantung DATA_MODE).
    Selesai → set is_warmup = False di modul aktif.
    """
    logger.info(f"Preloading historical candles (DATA_MODE={DATA_MODE})...")

    base_url: str  = REST_MARKET_URL  # OLD-08 FIX: pakai REST_MARKET_URL (selalu live) bukan REST_BASE_URL
    endpoint: str  = f"{base_url}/v5/market/kline"
    timeout        = aiohttp.ClientTimeout(total=15)
    connector      = aiohttp.TCPConnector(ssl=False)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        for tf, (interval_sec, max_candles) in TIMEFRAMES.items():
            if interval_sec < 60:
                logger.info(f"[PRELOAD] [{tf}] Skipped (sub-minute not available via Bybit kline API)")
                continue

            bybit_interval: Optional[str] = _BYBIT_INTERVAL_MAP.get(interval_sec)
            if bybit_interval is None:
                logger.warning(f"[PRELOAD] [{tf}] No Bybit interval mapping for {interval_sec}s — skipped")
                continue

            params: dict[str, str | int] = {
                "category": "linear",
                "symbol":   SYMBOL,
                "interval": bybit_interval,
                "limit":    min(max_candles, 200),
            }

            try:
                async with session.get(endpoint, params=params) as resp:
                    if resp.status != 200:
                        logger.error(f"[PRELOAD] [{tf}] HTTP {resp.status} — skipped")
                        continue
                    body: dict[str, object] = await resp.json()

                if body.get("retCode") != 0:
                    logger.error(f"[PRELOAD] [{tf}] API error: {body.get('retMsg')} — skipped")
                    continue

                result_data: dict[str, object] = body.get("result", {})  # type: ignore[assignment]
                raw_klines: list[list[str]] = result_data.get("list", [])  # type: ignore[assignment]
                if not raw_klines:
                    logger.warning(f"[PRELOAD] [{tf}] Empty kline response — skipped")
                    continue

                newest_start_ms: int = int(raw_klines[0][0])
                if newest_start_ms + interval_sec * 1000 > int(_time.time() * 1000):
                    raw_klines = raw_klines[1:]
                    logger.info(f"[PRELOAD] [{tf}] Stripped currently open candle (not yet closed)")

                loaded: int = active_data.preload_candles(tf, raw_klines)
                logger.info(f"[PRELOAD] [{tf}] {loaded} closed candles loaded (interval={bybit_interval})")

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
        # ---- KLINE MODE: trigger saat buffer berubah ATAU candle live update ----
        # BUG-06 FIX: Sebelumnya hanya trigger saat candle_buffers bertambah (candle tutup).
        # Sekarang juga trigger saat _current_candle (live candle) berubah.
        # Ini memastikan strategy.on_tick() dipanggil setiap ada data baru dari Bybit,
        # bukan hanya setiap 15 menit (di timeframe 15m) saat candle confirm.
        last_total: int = 0
        last_current_hash: int = 0
        while True:
            total: int = sum(len(buf) for buf in active_data.candle_buffers.values())

            # Hitung hash sederhana dari open_time semua current candle
            # untuk mendeteksi apakah ada live candle yang berubah
            current_hash: int = hash(tuple(
                (tf, c["open_time"] if c else 0, round(c["close"], 8) if c else 0)
                for tf, c in active_data._current_candle.items()
            ) if hasattr(active_data, "_current_candle") else 0)

            bid = data_stream.best_bid.get("price", 0.0)
            ask = data_stream.best_ask.get("price", 0.0)

            if bid > 0 and ask > 0:
                position_manager.update_pnl(bid, ask)

            if total != last_total or current_hash != last_current_hash:
                last_total = total
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
        logger.info("Bot stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[MAIN] Keyboard interrupt — bot stopped.")
        sys.exit(0)
