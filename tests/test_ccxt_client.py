# =============================================================================
# tests/test_ccxt_client.py — CCXT wrapper (exchange init, symbol resolution)
# =============================================================================
# Module ini load CCXT pro & instantiate exchange di import time. Test fokus
# ke fungsi yang dipanggil setelah import:
#   - get_symbol()           : raise kalau belum init, return symbol setelah set
#   - init_exchange()        : load_markets + resolve unified symbol
#   - close_exchange()       : tutup koneksi, swallow exception
# =============================================================================
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def cc(monkeypatch):
    """Reset _ccxt_symbol & CCXT_SYMBOL ke string kosong sebelum tiap test."""
    import ccxt_client as _cc
    monkeypatch.setattr(_cc, "_ccxt_symbol", "")
    monkeypatch.setattr(_cc, "CCXT_SYMBOL", "")
    return _cc


# =============================================================================
# get_symbol — guard sebelum init_exchange selesai
# =============================================================================
class TestGetSymbol:
    def test_raises_when_not_initialized(self, cc):
        """Sebelum init_exchange(), get_symbol() harus raise RuntimeError."""
        with pytest.raises(RuntimeError, match="init_exchange"):
            cc.get_symbol()

    def test_returns_value_after_init(self, cc, monkeypatch):
        monkeypatch.setattr(cc, "_ccxt_symbol", "XRP/USDT:USDT")
        assert cc.get_symbol() == "XRP/USDT:USDT"


# =============================================================================
# init_exchange — happy path + error cases
# =============================================================================
class TestInitExchange:
    def test_resolves_symbol_from_markets_by_id(self, cc, monkeypatch):
        """load_markets sukses + markets_by_id punya entry → CCXT_SYMBOL terisi."""
        # Mock load_markets supaya tidak HTTP
        fake_load = AsyncMock(return_value={})
        monkeypatch.setattr(cc.exchange, "load_markets", fake_load, raising=False)
        # markets_by_id: dict {SYMBOL_raw: market_info_dict}
        monkeypatch.setattr(cc.exchange, "markets_by_id",
                            {cc.SYMBOL: {"symbol": "XRP/USDT:USDT"}}, raising=False)

        asyncio.run(cc.init_exchange())

        # Symbol harus ter-set
        assert cc._ccxt_symbol == "XRP/USDT:USDT"
        assert cc.CCXT_SYMBOL == "XRP/USDT:USDT"
        fake_load.assert_awaited_once()

    def test_handles_list_market_info(self, cc, monkeypatch):
        """Beberapa CCXT exchange return list di markets_by_id — ambil [0]."""
        fake_load = AsyncMock(return_value={})
        monkeypatch.setattr(cc.exchange, "load_markets", fake_load, raising=False)
        monkeypatch.setattr(cc.exchange, "markets_by_id",
                            {cc.SYMBOL: [{"symbol": "XRP/USDT:USDT"}]}, raising=False)

        asyncio.run(cc.init_exchange())
        assert cc._ccxt_symbol == "XRP/USDT:USDT"

    def test_symbol_not_found_logs_error(self, cc, monkeypatch, caplog):
        """Symbol tidak ada di markets_by_id → log error, _ccxt_symbol tetap kosong."""
        fake_load = AsyncMock(return_value={})
        monkeypatch.setattr(cc.exchange, "load_markets", fake_load, raising=False)
        monkeypatch.setattr(cc.exchange, "markets_by_id", {}, raising=False)

        import logging
        with caplog.at_level(logging.ERROR, logger="ccxt_client"):
            asyncio.run(cc.init_exchange())

        assert cc._ccxt_symbol == ""
        assert any("tidak ditemukan" in rec.message for rec in caplog.records)

    def test_load_markets_exception_swallowed(self, cc, monkeypatch, caplog):
        """load_markets raise → log error, function tidak crash."""
        async def _boom():
            raise RuntimeError("network down")

        monkeypatch.setattr(cc.exchange, "load_markets", _boom, raising=False)

        import logging
        with caplog.at_level(logging.ERROR, logger="ccxt_client"):
            asyncio.run(cc.init_exchange())  # tidak boleh raise

        assert any("init_exchange()" in rec.message for rec in caplog.records)


# =============================================================================
# close_exchange — tutup koneksi gracefully
# =============================================================================
class TestCloseExchange:
    def test_calls_exchange_close(self, cc, monkeypatch):
        fake_close = AsyncMock()
        monkeypatch.setattr(cc.exchange, "close", fake_close, raising=False)
        asyncio.run(cc.close_exchange())
        fake_close.assert_awaited_once()

    def test_swallows_exception(self, cc, monkeypatch):
        """Kalau exchange.close raise, close_exchange tetap return tanpa crash."""
        async def _boom():
            raise RuntimeError("can't close")

        monkeypatch.setattr(cc.exchange, "close", _boom, raising=False)
        # Tidak boleh raise
        asyncio.run(cc.close_exchange())