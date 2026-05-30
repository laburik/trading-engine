# =============================================================================
# tests/test_candle_stream.py — Kline mode (DATA_MODE = "kline")
# =============================================================================
# Test fokus ke fungsi PURE (no I/O, no WebSocket):
#   - preload_candles: parse historical kline format (REST data, sudah ada)
#   - _process_ohlcv_update: detect candle-close via open_time change
#   - _get_spread / _get_orderbook_imbalance
#   - get_live_data: aggregator yang dipanggil tiap tick
#
# Watch loop async (_watch_ohlcv_loop, start_candle_stream) SENGAJA tidak
# di-test di sini — supaya tidak ada risiko salah konfigurasi pekerjaan
# tick-by-tick. Kalau perlu integration test, jalankan manual lewat run.bat.
# =============================================================================
from __future__ import annotations

from collections import deque

import pytest
from sortedcontainers import SortedDict


@pytest.fixture
def cs(monkeypatch):
    """Reset candle_stream + data_stream state per test."""
    import candle_stream as _cs
    import data_stream as _ds

    # Reset candle_buffers & _current_candle untuk semua TF
    for tf in _cs.candle_buffers:
        _cs.candle_buffers[tf] = deque(maxlen=200)
        _cs._current_candle[tf] = None

    monkeypatch.setattr(_cs, "is_warmup", False)

    # Reset shared data_stream state yang dipakai _get_spread & imbalance
    monkeypatch.setattr(_ds, "best_bid", {"price": 0.0, "qty": 0.0})
    monkeypatch.setattr(_ds, "best_ask", {"price": 0.0, "qty": 0.0})
    monkeypatch.setattr(_ds, "orderbook_snapshot", {
        "bids": SortedDict(), "asks": SortedDict(),
    })
    monkeypatch.setattr(_ds, "funding_rate",
                        {"value": 0.0, "next_funding_time": 0, "predicted": 0.0})
    monkeypatch.setattr(_ds, "last_prices", {})

    return _cs, _ds


# =============================================================================
# preload_candles — historical kline injection (REST data)
# =============================================================================
class TestPreloadCandles:
    def test_parses_ccxt_ohlcv_rows(self, cs):
        c, _ = cs
        rows = [
            [1700000000000, 1.5, 1.6, 1.4, 1.55, 1000.0],
            [1700000060000, 1.55, 1.65, 1.45, 1.60, 1200.0],
        ]
        loaded = c.preload_candles("1m", rows)
        assert loaded == 2
        first = c.candle_buffers["1m"][0]
        assert first["open"] == 1.5
        assert first["close"] == 1.55
        assert first["volume"] == 1000.0
        assert first["open_time"] == 1700000000.0

    def test_sorts_oldest_first(self, cs):
        c, _ = cs
        rows = [
            [1700000060000, 1.55, 1.65, 1.45, 1.60, 1200.0],
            [1700000000000, 1.5, 1.6, 1.4, 1.55, 1000.0],
        ]
        c.preload_candles("1m", rows)
        assert c.candle_buffers["1m"][0]["open_time"] == 1700000000.0
        assert c.candle_buffers["1m"][1]["open_time"] == 1700000060.0

    def test_unknown_tf_skipped(self, cs):
        c, _ = cs
        loaded = c.preload_candles("99x", [[1700000000000, 1, 1, 1, 1, 0]])
        assert loaded == 0

    def test_malformed_row_skipped(self, cs):
        c, _ = cs
        # Timestamp valid (sort tidak crash), tapi OHLCV non-numeric (ValueError)
        rows = [
            [1700000060000, "x", "y", "z", "a", "b"],
            [1700000000000, 1.5, 1.6, 1.4, 1.55, 1000.0],
        ]
        loaded = c.preload_candles("1m", rows)
        assert loaded == 1


# =============================================================================
# _process_ohlcv_update — detect candle-close via open_time change
# =============================================================================
class TestProcessOhlcvUpdate:
    def test_first_update_initializes_current_candle(self, cs):
        c, _ = cs
        row = [1700000060000, 1.5, 1.55, 1.49, 1.52, 1000.0]
        c._process_ohlcv_update("1m", row)
        assert c._current_candle["1m"] is not None
        assert c._current_candle["1m"]["close"] == 1.52
        # Belum ada yang ke buffer (first update)
        assert len(c.candle_buffers["1m"]) == 0

    def test_same_open_time_updates_current_no_close(self, cs):
        c, _ = cs
        # Dua update dengan open_time SAMA → candle live di-overwrite
        c._process_ohlcv_update("1m", [1700000060000, 1.5, 1.55, 1.49, 1.52, 100.0])
        c._process_ohlcv_update("1m", [1700000060000, 1.5, 1.60, 1.49, 1.58, 150.0])
        # Current ter-update, buffer masih kosong (open_time belum ganti)
        assert c._current_candle["1m"]["close"] == 1.58
        assert c._current_candle["1m"]["high"] == 1.60
        assert len(c.candle_buffers["1m"]) == 0

    def test_open_time_change_closes_previous(self, cs):
        c, _ = cs
        # Tick 1 di bucket A
        c._process_ohlcv_update("1m", [1700000060000, 1.5, 1.55, 1.49, 1.52, 100.0])
        # Tick 2 di bucket B (open_time berubah) → candle lama close
        c._process_ohlcv_update("1m", [1700000120000, 1.52, 1.60, 1.50, 1.58, 200.0])
        # Candle lama harus pindah ke buffer
        assert len(c.candle_buffers["1m"]) == 1
        old = c.candle_buffers["1m"][0]
        assert old["close"] == 1.52
        # Current = candle baru
        assert c._current_candle["1m"]["close"] == 1.58

    def test_last_prices_updated_in_data_stream(self, cs):
        c, ds = cs
        c._process_ohlcv_update("1m", [1700000060000, 1.5, 1.55, 1.49, 1.52, 100.0])
        assert ds.last_prices["1m"] == 1.52

    def test_close_logs_when_not_warmup(self, cs, caplog):
        """Saat is_warmup=False, log INFO untuk setiap candle yang baru ditutup."""
        c, _ = cs
        import logging
        c._process_ohlcv_update("1m", [1700000060000, 1.5, 1.55, 1.49, 1.52, 100.0])
        with caplog.at_level(logging.INFO, logger="candle_stream"):
            c._process_ohlcv_update("1m", [1700000120000, 1.52, 1.60, 1.50, 1.58, 200.0])
        assert any("NEW 1m CANDLE" in rec.message for rec in caplog.records)


# =============================================================================
# _get_spread — derived dari data_stream
# =============================================================================
class TestGetSpread:
    def test_returns_diff_when_both_valid(self, cs):
        c, ds = cs
        ds.best_bid["price"] = 1.500
        ds.best_ask["price"] = 1.502
        assert c._get_spread() == pytest.approx(0.002)

    def test_returns_zero_when_missing(self, cs):
        c, _ = cs
        assert c._get_spread() == 0.0


# =============================================================================
# _get_orderbook_imbalance — derived dari snapshot
# =============================================================================
class TestGetOrderbookImbalance:
    def test_buy_pressure_positive(self, cs):
        c, ds = cs
        ds.orderbook_snapshot["bids"][1.5] = 150.0
        ds.orderbook_snapshot["asks"][1.51] = 50.0
        assert c._get_orderbook_imbalance() > 0

    def test_ask_pressure_negative(self, cs):
        c, ds = cs
        ds.orderbook_snapshot["bids"][1.5] = 50.0
        ds.orderbook_snapshot["asks"][1.51] = 150.0
        assert c._get_orderbook_imbalance() < 0

    def test_empty_book_zero(self, cs):
        c, _ = cs
        assert c._get_orderbook_imbalance() == 0.0


# =============================================================================
# get_live_data — aggregator interface untuk strategi
# =============================================================================
class TestGetLiveData:
    def test_returns_required_keys(self, cs):
        c, _ = cs
        snap = c.get_live_data()
        for k in ("candles", "current", "best_bid", "best_ask",
                  "bid_ask_spread", "orderbook_imbalance", "volume_delta",
                  "funding_rate", "latest_tick", "is_warmup"):
            assert k in snap

    def test_volume_delta_always_zero_in_kline_mode(self, cs):
        c, _ = cs
        snap = c.get_live_data()
        assert snap["volume_delta"] == 0.0

    def test_latest_tick_always_none_in_kline_mode(self, cs):
        c, _ = cs
        snap = c.get_live_data()
        assert snap["latest_tick"] is None

    def test_includes_all_configured_timeframes(self, cs):
        c, _ = cs
        snap = c.get_live_data()
        for tf in c.candle_buffers:
            assert tf in snap["candles"]
            assert tf in snap["current"]

    def test_warmup_flag_propagated(self, cs, monkeypatch):
        c, _ = cs
        monkeypatch.setattr(c, "is_warmup", True)
        assert c.get_live_data()["is_warmup"] is True

    def test_current_candle_propagated_to_snapshot(self, cs):
        """Kalau ada _current_candle, snap['current'][tf] harus copy-nya."""
        c, _ = cs
        c._process_ohlcv_update("1m", [1700000060000, 1.5, 1.55, 1.49, 1.52, 100.0])
        snap = c.get_live_data()
        assert snap["current"]["1m"] is not None
        assert snap["current"]["1m"]["close"] == 1.52


# =============================================================================
# start_candle_stream — guard: kalau tidak ada TF valid, return tanpa start loop
# =============================================================================
# Ini test PURE control-flow guard — TIDAK panggil exchange.watch_ohlcv, tidak
# buka WebSocket. Cuma verifikasi: _valid_tfs kosong → fungsi log warning &
# return langsung tanpa asyncio.gather.
class TestStartCandleStreamGuard:
    def test_returns_early_when_no_valid_tfs(self, cs, monkeypatch, caplog):
        import asyncio
        import logging
        c, _ = cs
        monkeypatch.setattr(c, "_valid_tfs", [])
        with caplog.at_level(logging.WARNING, logger="candle_stream"):
            asyncio.run(c.start_candle_stream())
        assert any("Tidak ada timeframe valid" in r.message for r in caplog.records)