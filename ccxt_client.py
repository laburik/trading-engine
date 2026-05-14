# =============================================================================
# ccxt_client.py — Centralized CCXT Exchange Instance
# =============================================================================
# Diimport oleh execution.py, position_manager.py, main.py, dan data_stream.py.
# Inisialisasi exchange sekali, semua modul pakai objek yang sama.
# =============================================================================
from __future__ import annotations

import logging
import ccxt.pro as ccxt
from config import ACTIVE_API_KEY, ACTIVE_API_SECRET, MODE, SYMBOL

logger = logging.getLogger("ccxt_client")

# --- Inisialisasi exchange (belum load_markets) ---
exchange: ccxt.bybit = ccxt.bybit({
    "apiKey":          ACTIVE_API_KEY,
    "secret":          ACTIVE_API_SECRET,
    "enableRateLimit": True,
    "options": {
        "defaultType":             "linear",   # USDT-perp
        "adjustForTimeDifference": False,       # Nonaktifkan panggilan /market/time
        # Hanya load linear markets saat load_markets() — skip spot/inverse/option
        "fetchMarkets": {
            "types":   ["linear"],
            "options": [],
        },
    },
})

# Demo mode pakai hostname berbeda
if MODE == "demo":
    exchange.set_sandbox_mode(False)
    exchange.hostname = "api-demo.bybit.com"

# Unified CCXT symbol (diisi saat init_exchange() dipanggil)
# Contoh: "XRPUSDT" → "XRP/USDT:USDT"
# PENTING: Selalu akses via ccxt_client.CCXT_SYMBOL (bukan from ccxt_client import CCXT_SYMBOL)
_ccxt_symbol: str = ""


def get_symbol() -> str:
    """Getter yang selalu return nilai terbaru setelah init_exchange()."""
    if not _ccxt_symbol:
        raise RuntimeError(
            "[CCXT] get_symbol() dipanggil sebelum init_exchange() selesai."
        )
    return _ccxt_symbol


CCXT_SYMBOL: str = _ccxt_symbol


async def init_exchange() -> None:
    """
    Muat daftar market dari Bybit dan resolve unified symbol.
    HARUS dipanggil sekali di main() sebelum modul lain pakai CCXT.

    Catatan DNS:
    ccxt menginstall aiodns sebagai dependency. aiodns mengoverride DNS resolver
    aiohttp secara default dan tidak bisa kontak DNS server di environment ini.
    Solusi: uninstall aiodns (lihat requirements.txt) — tanpa aiodns, aiohttp
    otomatis pakai resolver Python standar yang bekerja dengan benar.
    """
    global _ccxt_symbol, CCXT_SYMBOL
    try:
        await exchange.load_markets()
        market_info = exchange.markets_by_id.get(SYMBOL)
        if isinstance(market_info, list):
            market_info = market_info[0]
        if market_info:
            _ccxt_symbol = market_info["symbol"]
            CCXT_SYMBOL  = _ccxt_symbol
            logger.info(f"[CCXT] Symbol resolved: {SYMBOL} → {CCXT_SYMBOL}")
        else:
            logger.error(f"[CCXT] Symbol '{SYMBOL}' tidak ditemukan di Bybit markets!")
    except Exception as e:
        logger.error(f"[CCXT] init_exchange() gagal: {e}")


async def close_exchange() -> None:
    """Tutup koneksi CCXT saat bot berhenti."""
    try:
        await exchange.close()
    except Exception:
        pass
