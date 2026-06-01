# =============================================================================
# test_connectivity.py — Tes klasifikasi blokir ISP (engine/connectivity.py)
# =============================================================================
# Fitur "deteksi blokir ISP" susah dites live (perlu mematikan koneksi), jadi
# yang diverifikasi di sini = LOGIKA KLASIFIKASI atas pesan exception. Probe
# jaringan (probe_bybit_*) sengaja tidak dites di sini (butuh network).
# =============================================================================
import connectivity


class TestIsIspBlockError:
    def test_ssl_handshake_failure_is_block(self):
        # Persis error yang muncul saat Cloudflare mati (sesi nyata).
        e = Exception("bybit GET https://api-demo.bybit.com ... SSLV3_ALERT_HANDSHAKE_FAILURE")
        assert connectivity.is_isp_block_error(e) is True

    def test_dns_failure_is_block(self):
        e = OSError("Cannot connect to host api.bybit.com:443 ssl:default [getaddrinfo failed]")
        assert connectivity.is_isp_block_error(e) is True

    def test_connection_reset_is_block(self):
        e = ConnectionResetError("Connection reset by peer")
        assert connectivity.is_isp_block_error(e) is True

    def test_normal_business_error_is_not_block(self):
        # Error level bisnis (saldo kurang) BUKAN blokir ISP — jangan salah vonis.
        e = Exception("bybit insufficient balance retCode 110007")
        assert connectivity.is_isp_block_error(e) is False

    def test_timeout_alone_is_not_block(self):
        # Timeout murni bisa banyak sebab → jangan langsung vonis ISP.
        e = TimeoutError("request timed out")
        assert connectivity.is_isp_block_error(e) is False


class TestExplainConnectionError:
    def test_block_returns_isp_message(self):
        e = Exception("... sslv3_alert_handshake_failure ...")
        assert connectivity.explain_connection_error(e) == connectivity.ISP_BLOCK_MESSAGE

    def test_non_block_returns_generic_message(self):
        e = ValueError("retCode 110007")
        msg = connectivity.explain_connection_error(e)
        assert msg != connectivity.ISP_BLOCK_MESSAGE
        assert "ValueError" in msg

    def test_isp_message_mentions_cloudflare(self):
        assert "Cloudflare" in connectivity.ISP_BLOCK_MESSAGE
