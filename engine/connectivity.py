# =============================================================================
# connectivity.py — Deteksi konektivitas Bybit + klasifikasi blokir ISP
# =============================================================================
# Banyak ISP (terutama di Indonesia) memblokir domain bybit.com di level
# DNS/SNI/TLS. Gejalanya: TLS handshake gagal / koneksi reset SEBELUM request
# sampai ke server. Modul ini mengklasifikasi error koneksi tersebut dan
# memberi pesan yang actionable (nyalakan Cloudflare / ganti DNS), bukan
# traceback mentah yang membingungkan.
#
# Dipakai oleh:
#   - main.py       (probe async, sebelum bot jalan — fail fast)
#   - dashboard.py  (probe sync, banner peringatan di Streamlit)
# =============================================================================
from __future__ import annotations

# Pesan yang ditampilkan saat terdeteksi blokir ISP.
ISP_BLOCK_MESSAGE = "Akses Bybit ditolak oleh ISP — gunakan Cloudflare atau ganti DNS."

# Tanda-tangan error yang KHAS blokir jaringan/ISP (TLS/DNS/koneksi diputus
# sebelum sampai server). Dicocokkan case-insensitive ke str(exception).
_ISP_BLOCK_SIGNATURES = (
    "sslv3_alert_handshake_failure",            # ← yang muncul saat Cloudflare mati
    "handshake failure",
    "sslerror",
    "ssl: ",
    "tlsv1",
    "connection reset",
    "connection aborted",
    "connection refused",
    "cannot connect to host",
    "server disconnected",
    "getaddrinfo failed",                       # DNS gagal (Windows)
    "name or service not known",                # DNS gagal (Linux)
    "temporary failure in name resolution",     # DNS gagal
    "nodename nor servname provided",           # DNS gagal (macOS)
)


def is_isp_block_error(exc: BaseException) -> bool:
    """True kalau exception koneksi cocok dengan tanda-tangan blokir ISP/jaringan.

    Heuristik — bukan kepastian mutlak (TLS/DNS bisa gagal karena sebab lain:
    Bybit down, firewall, jam salah). Maka pesannya berupa SARAN, bukan vonis.
    """
    msg = str(exc).lower()
    return any(sig in msg for sig in _ISP_BLOCK_SIGNATURES)


def explain_connection_error(exc: BaseException) -> str:
    """Ubah exception koneksi jadi pesan ramah.
    Blokir ISP → saran Cloudflare/DNS. Selain itu → ringkasan error apa adanya."""
    if is_isp_block_error(exc):
        return ISP_BLOCK_MESSAGE
    return f"Koneksi ke Bybit gagal: {type(exc).__name__}: {exc}"


async def probe_bybit_async(exchange) -> tuple[bool, str]:
    """Probe konektivitas pakai exchange CCXT (Pro) yang sudah ada — async.
    fetch_time() = panggilan publik ringan. Return (ok, message)."""
    try:
        await exchange.fetch_time()
        return True, "Bybit merespon — koneksi OK."
    except Exception as e:  # noqa: BLE001 — sengaja tangkap semua jenis error koneksi
        return False, explain_connection_error(e)


def probe_bybit_sync() -> tuple[bool, str]:
    """Probe konektivitas pakai CCXT sync (untuk dashboard Streamlit yang tidak
    async). Membangun client publik ringan lalu fetch_time(). Return (ok, message)."""
    try:
        import ccxt
        from config import EXCHANGE
        ex = getattr(ccxt, EXCHANGE)({"enableRateLimit": True,
                                      "options": {"defaultType": "linear"}})
        ex.fetch_time()
        return True, "Bybit merespon — koneksi OK."
    except Exception as e:  # noqa: BLE001
        return False, explain_connection_error(e)
