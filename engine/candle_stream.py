# =============================================================================
# candle_stream.py — Bybit Kline Stream via CCXT Pro (DATA_MODE = "kline")
# =============================================================================
#██████╗ ██╗███████╗██╗  ██╗██╗   ██╗    ██╗  ██╗███████╗███╗   ██╗ ██████╗ ██╗  ██╗███████╗██████╗ ██████╗ ██████╗  ██████╗
#██╔══██╗██║╚══███╔╝██║ ██╔╝╚██╗ ██╔╝    ██║  ██║██╔════╝████╗  ██║██╔════╝ ██║ ██╔╝██╔════╝██╔══██╗██╔══██╗██╔══██╗██╔═══██╗
#██████╔╝██║  ███╔╝ █████╔╝  ╚████╔╝     ███████║█████╗  ██╔██╗ ██║██║  ███╗█████╔╝ █████╗  ██████╔╝██████╔╝██████╔╝██║   ██║
#██╔══██╗██║ ███╔╝  ██╔═██╗   ╚██╔╝      ██╔══██║██╔══╝  ██║╚██╗██║██║   ██║██╔═██╗ ██╔══╝  ██╔══██╗██╔═══╝ ██╔══██╗██║   ██║
#██║  ██║██║███████╗██║  ██╗   ██║       ██║  ██║███████╗██║ ╚████║╚██████╔╝██║  ██╗███████╗██║  ██║██║     ██║  ██║╚██████╔╝
#╚═╝  ╚═╝╚═╝╚══════╝╚═╝  ╚═╝   ╚═╝       ╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝  ╚═╝ ╚═════╝
# if you copy, haram, unless you ask permission from the author
# for personal use only, if you use it for commercial purposes, you will be responsible for your own actions
# =============================================================================
#
# Aktif jika: DATA_MODE = "kline" di config.py
# Cara kerja (CCXT Pro):
#   1. exchange.watch_ohlcv(symbol, tf) mengembalikan list OHLCV terbaru.
#   2. Entry terakhir = candle yang sedang berjalan (live/open).
#   3. Jika open_time entry terakhir BERUBAH dari sebelumnya → candle lama
#      resmi ditutup dan disimpan ke candle_buffers.
#   4. Interface get_live_data() identik dengan data_resampler.py —
#      strategy.py tidak perlu diubah.
# =============================================================================
from __future__ import annotations

import time
import asyncio
import logging
from typing import Optional
from collections import deque
from config import SYMBOL, TIMEFRAMES
from ft_types import (
    BidAskLevel,
    Candle,
    MarketDataSnapshot,
)
from ccxt_client import exchange
import ccxt_client

# Import orderbook state dari data_stream (tetap dipakai untuk bid/ask/spread)
import data_stream

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [CANDLE_STREAM] %(levelname)s: %(message)s"
)
logger = logging.getLogger("candle_stream")

# =============================================================================
# WARMUP FLAG
# True  = historis masih loading → strategi tidak boleh trading
# False = live, strategi sudah boleh beraksi
# =============================================================================
is_warmup: bool = True

# =============================================================================
# CANDLE BUFFERS (interface identik dengan data_resampler)
# =============================================================================
candle_buffers: dict[str, deque[Candle]] = {
    tf: deque(maxlen=size)
    for tf, (_, size) in TIMEFRAMES.items()
}

# Candle berjalan (belum tutup) per timeframe
_current_candle: dict[str, Optional[Candle]] = {
    tf: None for tf in TIMEFRAMES
}

# Hanya subscribe TF yang interval >= 60 detik (sub-menit tidak tersedia di Bybit kline)
_valid_tfs: list[str] = [
    tf for tf, (interval_sec, _) in TIMEFRAMES.items() if interval_sec >= 60
]


# =============================================================================
# PRELOAD: Inject historical candles dari REST (dipanggil main.py)
# =============================================================================
def preload_candles(tf: str, raw_klines: list) -> int:
    """
    Inject historical candles ke candle_buffers[tf].
    Format input kompatibel dengan CCXT ohlcv: [[ts_ms, o, h, l, c, v], ...]
    maupun Bybit REST: [[ts_ms_str, o_str, h_str, l_str, c_str, v_str], ...]
    """
    if tf not in candle_buffers:
        logger.warning(f"[PRELOAD] TF tidak dikenal: {tf}")
        return 0

    ordered: list = sorted(raw_klines, key=lambda row: int(row[0]))
    loaded: int = 0
    for row in ordered:
        try:
            candle: Candle = {
                "timeframe":   tf,
                "open_time":   int(row[0]) / 1000.0,
                "open":        float(row[1]),
                "high":        float(row[2]),
                "low":         float(row[3]),
                "close":       float(row[4]),
                "volume":      float(row[5]),
                "buy_volume":  0.0,
                "sell_volume": 0.0,
                "tick_count":  0,
            }
            candle_buffers[tf].append(candle)
            loaded += 1
        except (IndexError, ValueError) as e:
            logger.warning(f"[PRELOAD] Baris kline rusak: {row} | {e}")

    logger.info(f"[PRELOAD] [{tf}] {loaded} candle historis dimuat")
    return loaded


# =============================================================================
# PROSES UPDATE OHLCV DARI CCXT PRO
# =============================================================================
def _process_ohlcv_update(tf: str, row: list) -> None:
    """
    Process satu baris OHLCV dari CCXT Pro: [ts_ms, open, high, low, close, volume].
    Deteksi candle tutup: jika open_time berubah dari _current_candle[tf],
    candle lama disimpan ke buffer sebagai candle yang resmi ditutup.
    """
    open_time_sec: float = float(row[0]) / 1000.0

    candle: Candle = {
        "timeframe":   tf,
        "open_time":   open_time_sec,
        "open":        float(row[1]),
        "high":        float(row[2]),
        "low":         float(row[3]),
        "close":       float(row[4]),
        "volume":      float(row[5]),
        "buy_volume":  0.0,
        "sell_volume": 0.0,
        "tick_count":  0,
    }

    prev: Optional[Candle] = _current_candle[tf]

    if prev is not None and prev["open_time"] != open_time_sec:
        # open_time berubah → candle sebelumnya resmi ditutup
        candle_buffers[tf].append(dict(prev))  # type: ignore[arg-type]
        data_stream.last_prices[tf] = prev["close"]
        if not is_warmup:
            logger.info(
                f"[NEW {tf} CANDLE] "
                f"O:{prev['open']:.5f} | H:{prev['high']:.5f} | "
                f"L:{prev['low']:.5f} | C:{prev['close']:.5f} | "
                f"Vol:{prev['volume']:.2f}"
            )

    # Update candle yang sedang berjalan
    _current_candle[tf] = candle
    data_stream.last_prices[tf] = candle["close"]


# =============================================================================
# CCXT PRO WATCH LOOP (satu per timeframe)
# Auto-reconnect ditangani CCXT Pro secara internal.
# =============================================================================
async def _watch_ohlcv_loop(tf: str) -> None:
    """Watch kline untuk satu timeframe via CCXT Pro."""
    logger.info(f"[{tf}] Starting OHLCV watch for {ccxt_client.CCXT_SYMBOL}...")
    while True:
        try:
            # CCXT Pro mengembalikan list OHLCV; entry terakhir = candle live
            ohlcv = await exchange.watch_ohlcv(ccxt_client.CCXT_SYMBOL, timeframe=tf)
            if ohlcv:
                _process_ohlcv_update(tf, ohlcv[-1])
        except asyncio.CancelledError:
            logger.info(f"[{tf}] OHLCV watch cancelled.")
            break
        except Exception as e:
            logger.error(f"[{tf}] OHLCV watch error: {e}. Retrying in 1s...")
            await asyncio.sleep(1)


# =============================================================================
# GET LIVE DATA (interface identik dengan data_resampler.get_live_data)
# Dipanggil oleh main.py strategy_loop setiap tick
# =============================================================================
def get_live_data() -> MarketDataSnapshot:
    """
    Agregat data untuk strategi. Interface identik dengan data_resampler.get_live_data().
    """
    best_bid_snap: BidAskLevel = dict(data_stream.best_bid)  # type: ignore[assignment]
    best_ask_snap: BidAskLevel = dict(data_stream.best_ask)  # type: ignore[assignment]

    candles_snapshot: dict[str, list[Candle]] = {
        tf: list(buf) for tf, buf in candle_buffers.items()
    }
    current_snapshot: dict[str, Optional[Candle]] = {}
    for tf, c in _current_candle.items():
        current_snapshot[tf] = dict(c) if c else None  # type: ignore[assignment]

    return {
        "candles":             candles_snapshot,
        "current":             current_snapshot,
        "best_bid":            best_bid_snap,
        "best_ask":            best_ask_snap,
        "bid_ask_spread":      _get_spread(),
        "orderbook_imbalance": _get_orderbook_imbalance(),
        "volume_delta":        0.0,   # tidak tersedia di mode kline
        "funding_rate":        data_stream.funding_rate.get("value", 0.0),
        "latest_tick":         None,  # tidak ada tick individu di mode kline
        "is_warmup":           is_warmup,
    }


def _get_spread() -> float:
    bid: float = data_stream.best_bid.get("price", 0.0)
    ask: float = data_stream.best_ask.get("price", 0.0)
    if ask > 0 and bid > 0:
        return round(ask - bid, 8)
    return 0.0


def _get_orderbook_imbalance() -> float:
    """
    Hitung orderbook imbalance dari data_stream.orderbook_snapshot.
    Range: -1.0 (tekanan jual penuh) sampai +1.0 (tekanan beli penuh).
    """
    snap = data_stream.orderbook_snapshot
    total_bid: float = sum(snap["bids"].values())
    total_ask: float = sum(snap["asks"].values())
    total: float     = total_bid + total_ask
    if total == 0:
        return 0.0
    return round((total_bid - total_ask) / total, 6)


# =============================================================================
# MAIN START FUNCTION (dipanggil oleh main.py)
# =============================================================================
async def start_candle_stream() -> None:
    """
    Jalankan kline WebSocket untuk semua timeframe yang valid via CCXT Pro.
    Menggantikan: manual WS connect + subscribe + message handler + auto-reconnect.
    """
    logger.info(f"Candle stream started (kline mode via CCXT Pro) — Symbol: {SYMBOL}")
    logger.info(f"Watching timeframes: {_valid_tfs}")

    if not _valid_tfs:
        logger.warning("Tidak ada timeframe valid (>= 1m) — candle stream tidak aktif.")
        return

    # Satu loop per timeframe, dijalankan bersamaan
    await asyncio.gather(*[_watch_ohlcv_loop(tf) for tf in _valid_tfs])
