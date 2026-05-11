#██████╗ ██╗███████╗██╗  ██╗██╗   ██╗    ██╗  ██╗███████╗███╗   ██╗ ██████╗ ██╗  ██╗███████╗██████╗ ██████╗ ██████╗  ██████╗ 
#██╔══██╗██║╚══███╔╝██║ ██╔╝╚██╗ ██╔╝    ██║  ██║██╔════╝████╗  ██║██╔════╝ ██║ ██╔╝██╔════╝██╔══██╗██╔══██╗██╔══██╗██╔═══██╗
#██████╔╝██║  ███╔╝ █████╔╝  ╚████╔╝     ███████║█████╗  ██╔██╗ ██║██║  ███╗█████╔╝ █████╗  ██████╔╝██████╔╝██████╔╝██║   ██║
#██╔══██╗██║ ███╔╝  ██╔═██╗   ╚██╔╝      ██╔══██║██╔══╝  ██║╚██╗██║██║   ██║██╔═██╗ ██╔══╝  ██╔══██╗██╔═══╝ ██╔══██╗██║   ██║
#██║  ██║██║███████╗██║  ██╗   ██║       ██║  ██║███████╗██║ ╚████║╚██████╔╝██║  ██╗███████╗██║  ██║██║     ██║  ██║╚██████╔╝
#╚═╝  ╚═╝╚═╝╚══════╝╚═╝  ╚═╝   ╚═╝       ╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝  ╚═╝ ╚═════╝                                                                                                                             
# if you copy, haram, unless you ask permission from the author
# for personal use only, if you use it for commercial purposes, you will be responsible for your own actions
# =============================================================================
# candle_stream.py — Bybit Native Kline WebSocket (DATA_MODE = "kline")
# =============================================================================
#
# Aktif jika: DATA_MODE = "kline" di config.py
# Cara kerja:
#   1. Subscribe ke topic "kline.{interval}.{SYMBOL}" untuk setiap timeframe.
#   2. Saat Bybit mengirim candle dengan "confirm": true, candle tersebut sudah
#      RESMI (ditutup) dan langsung disimpan ke candle_buffers.
#   3. Sebelum itu, live candle (belum confirm) disimpan ke _current_candle.
#   4. Menyediakan get_live_data() yang IDENTIK dengan milik data_resampler.py
#      sehingga strategy.py tidak perlu diubah sama sekali.
# =============================================================================
from __future__ import annotations

import time
import json
import asyncio
import logging
from typing import Optional
import aiohttp
from collections import deque
from config import (
    SYMBOL, CATEGORY, WS_PUBLIC_URL, REST_BASE_URL,
    TIMEFRAMES, ORDERBOOK_DEPTH,
)
from ft_types import (
    BidAskLevel,
    Candle,
    MarketDataSnapshot,
)

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
# PETA BYBIT INTERVAL  (detik → string Bybit kline)
# =============================================================================
_BYBIT_INTERVAL_MAP: dict[int, str] = {
    # Catatan: Bybit Kline WS menggunakan satuan MENIT untuk interval string.
    # Interval sub-menit (1 detik, 5 detik) TIDAK tersedia di Bybit Kline WS public stream.
    # Entry 1s dan 5s dihapus karena konflik dengan 1m (keduanya map ke "1").
    # BUG-03 FIX: Removed 1:"1" and 5:"5" entries that caused collision with 60:"1" (1 minute)
    60:    "1",    # 1 menit
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
# CANDLE BUFFERS (interface identik dengan data_resampler)
# =============================================================================
candle_buffers: dict[str, deque[Candle]] = {
    tf: deque(maxlen=size)
    for tf, (_, size) in TIMEFRAMES.items()
}

# Candle berjalan (belum confirm / belum tutup) per timeframe
_current_candle: dict[str, Optional[Candle]] = {
    tf: None for tf in TIMEFRAMES
}

# Map label TF ke interval Bybit (untuk subscribe)
_tf_to_bybit: dict[str, str] = {}
for tf, (interval_sec, _) in TIMEFRAMES.items():
    _bi = _BYBIT_INTERVAL_MAP.get(interval_sec)
    if _bi:
        _tf_to_bybit[tf] = _bi

# Map "bybit_interval" kembali ke label TF (untuk routing saat terima data)
_bybit_to_tf: dict[str, str] = {v: k for k, v in _tf_to_bybit.items()}


# =============================================================================
# PRELOAD: Unduh histori dari REST, masukkan ke candle_buffers
# =============================================================================
def preload_candles(tf: str, raw_klines: list[list[str]]) -> int:
    """
    Inject historical candles (Bybit REST format) ke candle_buffers[tf].
    Format baris: [startTime_ms, open, high, low, close, volume, turnover]
    """
    if tf not in candle_buffers:
        logger.warning(f"[PRELOAD] TF tidak dikenal: {tf}")
        return 0

    ordered: list[list[str]] = sorted(raw_klines, key=lambda row: int(row[0]))
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
# PROSES PESAN KLINE DARI WEBSOCKET
# =============================================================================
def _process_kline_message(msg: dict[str, object]) -> None:
    """
    Tangani pesan kline dari Bybit WebSocket.
    Format topic: "kline.{interval}.{SYMBOL}"
    """
    topic: str = str(msg.get("topic", ""))
    # topic contoh: "kline.1.DOGEUSDT" → ambil interval "1"
    parts: list[str] = topic.split(".")
    if len(parts) < 2:
        return

    bybit_interval: str = parts[1]

    # Temukan label TF  (bisa ada banyak TF memetakan ke interval yang sama,
    # misal "1s" dan "1m" keduanya → "1". Kita cocokkan pakai open_time)
    data_list: list[dict[str, object]] = msg.get("data", [])  # type: ignore[assignment]
    for kline in data_list:
        # Cari TF yang cocok berdasarkan interval Bybit
        tf: Optional[str] = _bybit_to_tf.get(bybit_interval)
        if tf is None:
            continue

        confirm: bool   = bool(kline.get("confirm", False))
        start_ms: int   = int(str(kline.get("start", 0)))
        candle: Candle  = {
            "timeframe":   tf,
            "open_time":   start_ms / 1000.0,
            "open":        float(str(kline.get("open",   0))),
            "high":        float(str(kline.get("high",   0))),
            "low":         float(str(kline.get("low",    0))),
            "close":       float(str(kline.get("close",  0))),
            "volume":      float(str(kline.get("volume", 0))),
            "buy_volume":  0.0,
            "sell_volume": 0.0,
            "tick_count":  0,
        }

        if confirm:
            # Candle RESMI ditutup → simpan ke buffer historis
            candle_buffers[tf].append(dict(candle))  # type: ignore[arg-type]
            data_stream.last_prices[tf] = candle["close"]
            if not is_warmup:
                logger.info(
                    f"[NEW {tf} CANDLE] "
                    f"O:{candle['open']:.5f} | H:{candle['high']:.5f} | "
                    f"L:{candle['low']:.5f} | C:{candle['close']:.5f} | "
                    f"Vol:{candle['volume']:.2f}"
                )
        else:
            # Candle masih berjalan → simpan sebagai live candle
            _current_candle[tf] = candle
            data_stream.last_prices[tf] = candle["close"]


# =============================================================================
# WEBSOCKET HANDLER
# =============================================================================
async def _ws_handler(ws: aiohttp.ClientWebSocketResponse) -> None:
    """Dispatcher pesan WebSocket untuk kline + orderbook."""
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            try:
                data: dict[str, object] = json.loads(msg.data)

                # Tangkap error dari Bybit
                if "success" in data and data["success"] is False:
                    logger.error(
                        f"BYBIT ERROR: {data.get('ret_msg', 'Topic ditolak server')}"
                    )

                topic: str = str(data.get("topic", ""))

                if topic.startswith("kline."):
                    _process_kline_message(data)
                # BUG-02 FIX: Orderbook diproses hanya oleh data_stream.py.
                # Routing ke data_stream._process_orderbook_message() dari sini
                # dihapus untuk menghindari duplikat write ke orderbook_snapshot.

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
# WEBSOCKET CONNECTION + AUTO-RECONNECT
# =============================================================================
async def _connect_websocket(session: aiohttp.ClientSession) -> None:
    reconnect_delay: int = 1
    max_delay: int       = 60

    # Bangun daftar kline topics berdasarkan TIMEFRAMES
    kline_args: list[str]    = []
    seen_intervals: set[str] = set()
    for tf, (interval_sec, _) in TIMEFRAMES.items():
        bi: Optional[str] = _BYBIT_INTERVAL_MAP.get(interval_sec)
        if bi and bi not in seen_intervals:
            kline_args.append(f"kline.{bi}.{SYMBOL}")
            seen_intervals.add(bi)

    # BUG-02 FIX: Orderbook TIDAK disubscribe di sini.
    # data_stream.py sudah subscribe dan memproses orderbook secara independen.
    # Subscribe ganda menyebabkan dua WS connection menulis ke orderbook_snapshot
    # yang sama secara bersamaan (race condition).

    while True:
        try:
            logger.info(f"Connecting to WebSocket: {WS_PUBLIC_URL}")
            async with session.ws_connect(
                WS_PUBLIC_URL,
                heartbeat=20,
                receive_timeout=30,
            ) as ws:
                reconnect_delay = 1

                subscribe_msg: dict[str, object] = {
                    "op": "subscribe",
                    "args": kline_args,
                }
                await ws.send_str(json.dumps(subscribe_msg))
                logger.info(f"Subscribed kline topics: {kline_args}")

                await _ws_handler(ws)

        except asyncio.CancelledError:
            logger.info("Candle stream task cancelled.")
            break
        except Exception as e:
            logger.error(f"WebSocket connection error: {e}")

        logger.info(f"Reconnecting in {reconnect_delay}s...")
        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, max_delay)


# =============================================================================
# GET LIVE DATA (identik dengan data_resampler.get_live_data)
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
        "orderbook_imbalance": _get_orderbook_imbalance(),  # BUG-11 FIX: hitung dari orderbook_snapshot
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
    BUG-11 FIX: Hitung orderbook imbalance dari data_stream.orderbook_snapshot.
    Sebelumnya selalu return 0.0 di kline mode.
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
    Jalankan kline + orderbook WebSocket stream.
    Dipanggil oleh main.py jika DATA_MODE = "kline".
    """
    logger.info(f"Candle stream started (kline mode) — Symbol: {SYMBOL}")
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        await _connect_websocket(session)
