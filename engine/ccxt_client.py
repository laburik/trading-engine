#██████╗ ██╗███████╗██╗  ██╗██╗   ██╗    ██╗  ██╗███████╗███╗   ██╗ ██████╗ ██╗  ██╗███████╗██████╗ ██████╗ ██████╗  ██████╗ 
#██╔══██╗██║╚══███╔╝██║ ██╔╝╚██╗ ██╔╝    ██║  ██║██╔════╝████╗  ██║██╔════╝ ██║ ██╔╝██╔════╝██╔══██╗██╔══██╗██╔══██╗██╔═══██╗
#██████╔╝██║  ███╔╝ █████╔╝  ╚████╔╝     ███████║█████╗  ██╔██╗ ██║██║  ███╗█████╔╝ █████╗  ██████╔╝██████╔╝██████╔╝██║   ██║
#██╔══██╗██║ ███╔╝  ██╔═██╗   ╚██╔╝      ██╔══██║██╔══╝  ██║╚██╗██║██║   ██║██╔═██╗ ██╔══╝  ██╔══██╗██╔═══╝ ██╔══██╗██║   ██║
#██║  ██║██║███████╗██║  ██╗   ██║       ██║  ██║███████╗██║ ╚████║╚██████╔╝██║  ██╗███████╗██║  ██║██║     ██║  ██║╚██████╔╝
#╚═╝  ╚═╝╚═╝╚══════╝╚═╝  ╚═╝   ╚═╝       ╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝  ╚═╝ ╚═════╝
# =============================================================================
# ccxt_client.py — Centralized CCXT Exchange Instance
# =============================================================================
# Diimport oleh execution.py, position_manager.py, main.py, dan data_stream.py.
# Inisialisasi exchange sekali, semua modul pakai objek yang sama.
# =============================================================================
from __future__ import annotations

import logging
import ccxt.pro as ccxt
from config import ACTIVE_API_KEY, ACTIVE_API_SECRET, MODE, SYMBOL, EXCHANGE, LEVERAGE

logger = logging.getLogger("ccxt_client")

# --- Resolve kelas exchange dari nama (CCXT Pro) ---
# `getattr(ccxt, "bybit")` ≡ `ccxt.bybit`. Hanya exchange yang tersedia di CCXT
# Pro yang bisa dipakai (sebagian besar major exchanges mendukung WebSocket).
try:
    _ExchangeClass = getattr(ccxt, EXCHANGE)
except AttributeError as _e:
    raise RuntimeError(
        f"[CCXT] Exchange '{EXCHANGE}' tidak tersedia di ccxt.pro. "
        f"Cek nama di https://docs.ccxt.com/#/?id=exchanges"
    ) from _e

# --- Inisialisasi exchange (belum load_markets) ---
exchange = _ExchangeClass({
    "apiKey":          ACTIVE_API_KEY,
    "secret":          ACTIVE_API_SECRET,
    "enableRateLimit": True,
    "options": {
        "defaultType":             "linear",   # USDT-perp (Bybit/Binance); exchange lain auto-pilih default
        # Auto-koreksi selisih jam lokal vs server: CCXT tanya jam exchange sekali,
        # lalu tempel offset ke setiap timestamp request bertanda-tangan. Cegah
        # InvalidNonce/10002 saat jam OS drift (lihat recv_window di bawah).
        "adjustForTimeDifference": True,
        # Toleransi selisih timestamp request vs server (default 5000ms). 10s memberi
        # margin terhadap drift jam OS / delay event-loop tanpa melemahkan anti-replay
        # secara berarti untuk trading ritel.
        "recvWindow":              10000,
    },
})

# Demo mode: behavior berbeda per exchange.
#   - Bybit  → pakai endpoint demo (data live REAL + fill simulasi)
#   - Lain   → pakai testnet via set_sandbox_mode(True) (data testnet, kurang akurat)
if MODE == "demo":
    if EXCHANGE == "bybit":
        # Bybit Demo Trading. CCXT versi baru punya helper khusus yang handle
        # REST URL + WebSocket URL sekaligus — pakai itu kalau ada.
        # Fallback untuk CCXT versi lama: patch URL REST tanpa replace whole
        # dict (supaya kunci WebSocket 'ws', 'public', dll tetap aman).
        exchange.set_sandbox_mode(False)
        if hasattr(exchange, "enable_demo_trading"):
            exchange.enable_demo_trading(True)
        else:
            api_urls = exchange.urls.get("api", {})
            if isinstance(api_urls, dict):
                api_urls["public"]  = "https://api-demo.bybit.com"
                api_urls["private"] = "https://api-demo.bybit.com"
                exchange.urls["api"] = api_urls
            logger.warning(
                "[CCXT] enable_demo_trading() tidak tersedia di versi CCXT ini. "
                "REST diarahkan ke api-demo, tapi WebSocket mungkin tetap ke live. "
                "Upgrade CCXT: pip install --upgrade ccxt"
            )
    else:
        try:
            exchange.set_sandbox_mode(True)
            logger.info(f"[CCXT] {EXCHANGE}: testnet/sandbox mode active.")
        except Exception as _e:
            logger.warning(
                f"[CCXT] {EXCHANGE} tidak support sandbox mode: {_e}. "
                f"Mode demo akan jalan di live endpoint (risiko: API call ke live)."
            )

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
        # Beberapa exchange (terutama Bybit) punya 1 raw symbol "XRPUSDT" yang
        # mapping ke 2 market unified: spot (XRP/USDT) DAN perpetual (XRP/USDT:USDT).
        # Bot ini config untuk perpetual (defaultType=linear, fetch_funding_rate, dll),
        # jadi WAJIB pilih yang swap+linear. Tanpa filter, kadang yang ke-pick spot
        # → fetchFundingRate gagal & order routing salah.
        if isinstance(market_info, list):
            picked = None
            for m in market_info:
                if isinstance(m, dict) and m.get("swap") and m.get("linear"):
                    picked = m
                    break
            market_info = picked or market_info[0]
        if market_info:
            _ccxt_symbol = market_info["symbol"]
            CCXT_SYMBOL  = _ccxt_symbol
            logger.info(f"[CCXT] Symbol resolved: {SYMBOL} → {CCXT_SYMBOL}")
        else:
            logger.error(f"[CCXT] Symbol '{SYMBOL}' tidak ditemukan di Bybit markets!")
    except Exception as e:
        logger.error(f"[CCXT] init_exchange() gagal: {e}")


async def sync_leverage() -> None:
    """Samakan leverage AKUN di exchange dengan config.LEVERAGE untuk SYMBOL aktif.

    Bot menentukan ukuran order sebagai notional = ORDER_SIZE_USDT × LEVERAGE.
    Supaya margin yang dikunci exchange benar-benar = ORDER_SIZE_USDT (sesuai
    maksud config), leverage akun di exchange HARUS sama dengan config.LEVERAGE.
    Tanpa sync ini, config & exchange bisa mismatch (mis. config 20x tapi akun
    masih 1x → bot kirim order besar yang lalu ditolak "insufficient margin").

    - Hanya untuk mode demo/live (paper tidak punya konsep leverage exchange).
    - HARUS dipanggil SETELAH init_exchange() (butuh unified symbol ter-resolve).
    - Idempoten: kalau leverage sudah sama, Bybit balas "leverage not modified"
      (retCode 110043/110010) → diperlakukan SUKSES, bukan error.
    - Gagal set (mis. melebihi max leverage simbol) → WARNING, bot tetap jalan.
    """
    if MODE == "paper":
        return

    try:
        sym = get_symbol()
    except RuntimeError:
        logger.error(
            "[CCXT] sync_leverage() dipanggil sebelum symbol ter-resolve "
            "(init_exchange belum sukses) — leverage tidak di-set."
        )
        return

    if not exchange.has.get("setLeverage", False):
        logger.warning(
            f"[CCXT] {EXCHANGE} tidak mendukung setLeverage — leverage akun "
            f"tidak disinkronkan. Set manual di exchange kalau perlu."
        )
        return

    try:
        await exchange.set_leverage(LEVERAGE, sym)
        logger.info(f"[CCXT] Leverage akun di-set {LEVERAGE}x untuk {sym} (sinkron config).")
    except Exception as e:
        msg = str(e).lower()
        # Leverage sudah sama → Bybit balas error 'not modified' (110043/110010).
        # Itu artinya kondisi SUDAH benar, bukan kegagalan.
        if "not modified" in msg or "110043" in msg or "110010" in msg:
            logger.info(f"[CCXT] Leverage {sym} sudah {LEVERAGE}x — tidak perlu diubah.")
            return
        logger.warning(
            f"[CCXT] Gagal set leverage {LEVERAGE}x untuk {sym}: {type(e).__name__}: {e}. "
            f"Bot tetap jalan, TAPI leverage akun bisa beda dari config → margin order "
            f"live mungkin tak sesuai harapan. Set manual di {EXCHANGE} bila perlu."
        )


async def close_exchange() -> None:
    """Tutup koneksi CCXT saat bot berhenti."""
    try:
        await exchange.close()
    except Exception:
        pass
