# =============================================================================
# tests/test_data_resampler.py — Tick-to-candle aggregation & derived metrics
# =============================================================================
# Cover:
#   - _bucket_start / _new_candle / _update_candle (pure helpers)
#   - _close_candle (append ke buffer)
#   - preload_candles (parse Bybit kline format)
#   - _process_tick_for_tf (tick → candle aggregation; new candle vs update)
#   - get_bid_ask_spread / get_orderbook_imbalance / get_volume_delta_last_n
#   - get_live_data (aggregator yang dipanggil tiap tick oleh main)
# =============================================================================
from __future__ import annotations

from collections import deque

import pytest
from sortedcontainers import SortedDict


@pytest.fixture
def dr(monkeypatch):
    """Reset state data_resampler & data_stream per test."""
    import data_resampler as _dr
    import data_stream as _ds

    # Reset buffer candle per tf
    for tf in _dr.candle_buffers:
        _dr.candle_buffers[tf] = deque(maxlen=200)
        _dr._current_candle[tf] = None

    # Reset is_warmup ke False supaya log path ke-cover
    monkeypatch.setattr(_dr, "is_warmup", False)

    # Reset data_stream shared state
    monkeypatch.setattr(_ds, "best_bid", {"price": 0.0, "qty": 0.0})
    monkeypatch.setattr(_ds, "best_ask", {"price": 0.0, "qty": 0.0})
    monkeypatch.setattr(_ds, "tick_buffer", deque(maxlen=1000))
    monkeypatch.setattr(_ds, "orderbook_snapshot", {
        "bids": SortedDict(), "asks": SortedDict(),
    })
    monkeypatch.setattr(_ds, "funding_rate", {"value": 0.0, "next_funding_time": 0, "predicted": 0.0})
    monkeypatch.setattr(_ds, "last_prices", {})

    return _dr, _ds


# =============================================================================
# _bucket_start — quantize timestamp ke awal bucket
# =============================================================================
class TestBucketStart:
    def test_aligns_to_interval(self, dr):
        d, _ = dr
        # tf=1m (60s), ts=1700000123 → bucket = 1700000100 (floor)
        bucket = d._bucket_start("1m", 1700000123.0)
        assert bucket == 1700000100.0

    def test_exact_boundary(self, dr):
        d, _ = dr
        # 1700001000 = 1888890 × 900, tepat aligned ke bucket 15m
        bucket = d._bucket_start("15m", 1700001000.0)
        assert bucket == 1700001000.0


# =============================================================================
# _new_candle — buat candle baru dari tick
# =============================================================================
class TestNewCandle:
    def test_initializes_ohlc_to_same_price(self, dr):
        d, _ = dr
        # ts=1700000040 aligned ke bucket 1m (60s)
        c = d._new_candle("1m", price=1.5, qty=100.0, ts=1700000040.0)
        assert c["open"] == c["high"] == c["low"] == c["close"] == 1.5
        assert c["volume"] == 100.0
        assert c["tick_count"] == 1
        assert c["timeframe"] == "1m"
        assert c["open_time"] == 1700000040.0


# =============================================================================
# _update_candle — append tick ke candle existing
# =============================================================================
class TestUpdateCandle:
    def test_close_always_updated(self, dr):
        d, _ = dr
        c = d._new_candle("1m", 1.5, 100, 0)
        d._update_candle(c, price=1.6, qty=50, side="Buy")
        assert c["close"] == 1.6
        assert c["volume"] == 150
        assert c["tick_count"] == 2

    def test_high_only_grows(self, dr):
        d, _ = dr
        c = d._new_candle("1m", 1.5, 100, 0)
        d._update_candle(c, 1.7, 10, "Buy")
        assert c["high"] == 1.7
        # Tick lebih rendah tidak boleh ubah high
        d._update_candle(c, 1.4, 10, "Sell")
        assert c["high"] == 1.7

    def test_low_only_shrinks(self, dr):
        d, _ = dr
        c = d._new_candle("1m", 1.5, 100, 0)
        d._update_candle(c, 1.3, 10, "Sell")
        assert c["low"] == 1.3
        d._update_candle(c, 1.6, 10, "Buy")
        assert c["low"] == 1.3

    def test_buy_sell_volume_tracked_separately(self, dr):
        d, _ = dr
        c = d._new_candle("1m", 1.5, 0, 0)
        c["buy_volume"] = 0
        c["sell_volume"] = 0
        c["volume"] = 0
        c["tick_count"] = 0
        d._update_candle(c, 1.5, 30, "Buy")
        d._update_candle(c, 1.5, 20, "Sell")
        assert c["buy_volume"] == 30
        assert c["sell_volume"] == 20


# =============================================================================
# preload_candles — inject historical Bybit kline
# =============================================================================
class TestPreloadCandles:
    def test_parses_bybit_kline_rows(self, dr):
        d, _ = dr
        # Format Bybit v5: [startTime_ms, open, high, low, close, volume, turnover]
        rows = [
            ["1700000000000", "1.5", "1.6", "1.4", "1.55", "1000.0", "0"],
            ["1700000060000", "1.55", "1.65", "1.45", "1.60", "1200.0", "0"],
        ]
        loaded = d.preload_candles("1m", rows)
        assert loaded == 2
        assert len(d.candle_buffers["1m"]) == 2
        first = d.candle_buffers["1m"][0]
        assert first["open"] == 1.5
        assert first["high"] == 1.6
        assert first["low"] == 1.4
        assert first["close"] == 1.55
        assert first["open_time"] == 1700000000.0  # ms → s

    def test_sorts_oldest_first(self, dr):
        d, _ = dr
        rows = [
            ["1700000060000", "1.55", "1.65", "1.45", "1.60", "1200.0", "0"],
            ["1700000000000", "1.5", "1.6", "1.4", "1.55", "1000.0", "0"],  # tertua di akhir
        ]
        d.preload_candles("1m", rows)
        assert d.candle_buffers["1m"][0]["open_time"] == 1700000000.0
        assert d.candle_buffers["1m"][1]["open_time"] == 1700000060.0

    def test_unknown_timeframe_skipped(self, dr):
        d, _ = dr
        loaded = d.preload_candles("99x", [["1700000000000", "1.5", "1.6", "1.4", "1.55", "1000.0", "0"]])
        assert loaded == 0

    def test_malformed_field_row_skipped_not_crash(self, dr):
        d, _ = dr
        # Timestamp valid (sort tidak crash) tapi OHLCV non-numeric (ValueError di for)
        rows = [
            ["1700000060000", "x", "y", "z", "a", "b", "c"],
            ["1700000000000", "1.5", "1.6", "1.4", "1.55", "1000.0", "0"],
        ]
        loaded = d.preload_candles("1m", rows)
        # Yang valid harus tetap masuk, yang invalid dilewat
        assert loaded == 1


# =============================================================================
# _process_tick_for_tf — main aggregation loop
# =============================================================================
class TestProcessTick:
    def test_first_tick_creates_candle(self, dr):
        d, _ = dr
        d._process_tick_for_tf("1m", price=1.5, qty=100, side="Buy", ts=1700000060.0)
        assert d._current_candle["1m"] is not None
        c = d._current_candle["1m"]
        assert c["open"] == 1.5
        assert c["buy_volume"] == 100

    def test_first_tick_sell_side(self, dr):
        d, _ = dr
        d._process_tick_for_tf("1m", price=1.5, qty=100, side="Sell", ts=1700000060.0)
        c = d._current_candle["1m"]
        assert c["sell_volume"] == 100
        assert c["buy_volume"] == 0

    def test_tick_in_same_bucket_updates_existing(self, dr):
        d, _ = dr
        # 1700000040 & 1700000050 sama-sama di bucket 1700000040 (1m=60s)
        d._process_tick_for_tf("1m", 1.5, 100, "Buy", 1700000040.0)
        d._process_tick_for_tf("1m", 1.6, 50, "Buy", 1700000050.0)
        c = d._current_candle["1m"]
        assert c["close"] == 1.6
        assert c["high"] == 1.6
        assert c["volume"] == 150
        assert len(d.candle_buffers["1m"]) == 0  # belum close

    def test_tick_in_new_bucket_closes_old_opens_new(self, dr):
        d, _ = dr
        # bucket 1700000040 (1m)
        d._process_tick_for_tf("1m", 1.5, 100, "Buy", 1700000040.0)
        # bucket 1700000100 — 60s lebih, bucket berbeda
        d._process_tick_for_tf("1m", 1.7, 80, "Sell", 1700000100.0)
        # Candle lama harus pindah ke buffer
        assert len(d.candle_buffers["1m"]) == 1
        old = d.candle_buffers["1m"][0]
        assert old["open"] == 1.5
        # Candle baru di-init dengan tick baru
        new = d._current_candle["1m"]
        assert new["open"] == 1.7
        assert new["sell_volume"] == 80

    def test_last_prices_updated(self, dr):
        d, ds = dr
        d._process_tick_for_tf("1m", 1.5, 100, "Buy", 1700000060.0)
        assert ds.last_prices["1m"] == 1.5


# =============================================================================
# Derived metrics — spread, imbalance, volume delta
# =============================================================================
class TestDerivedMetrics:
    def test_bid_ask_spread(self, dr):
        d, ds = dr
        ds.best_bid["price"] = 1.500
        ds.best_ask["price"] = 1.502
        assert d.get_bid_ask_spread() == pytest.approx(0.002)

    def test_spread_zero_when_data_missing(self, dr):
        d, _ = dr
        # Default best_bid/ask = 0
        assert d.get_bid_ask_spread() == 0.0

    def test_orderbook_imbalance_buy_pressure(self, dr):
        d, ds = dr
        # bids > asks → imbalance positif
        ds.orderbook_snapshot["bids"][1.5] = 100.0
        ds.orderbook_snapshot["asks"][1.51] = 50.0
        imb = d.get_orderbook_imbalance()
        assert imb > 0
        assert imb == pytest.approx((100 - 50) / 150, rel=1e-4)

    def test_orderbook_imbalance_ask_pressure(self, dr):
        d, ds = dr
        ds.orderbook_snapshot["bids"][1.5] = 50.0
        ds.orderbook_snapshot["asks"][1.51] = 100.0
        assert d.get_orderbook_imbalance() < 0

    def test_orderbook_imbalance_empty_book(self, dr):
        d, _ = dr
        assert d.get_orderbook_imbalance() == 0.0

    def test_volume_delta_net_buy(self, dr):
        d, ds = dr
        ds.tick_buffer.extend([
            {"price": 1.5, "qty": 100, "side": "Buy", "timestamp": 1.0},
            {"price": 1.5, "qty": 50, "side": "Sell", "timestamp": 2.0},
            {"price": 1.5, "qty": 30, "side": "Buy", "timestamp": 3.0},
        ])
        delta = d.get_volume_delta_last_n(50)
        # buy=130, sell=50 → delta=80
        assert delta == pytest.approx(80.0)

    def test_volume_delta_empty_buffer(self, dr):
        d, _ = dr
        assert d.get_volume_delta_last_n(50) == 0.0


# =============================================================================
# get_live_data — snapshot lengkap yang dikirim ke strategy
# =============================================================================
class TestGetLiveData:
    def test_returns_required_keys(self, dr):
        d, ds = dr
        ds.best_bid["price"] = 1.5
        ds.best_ask["price"] = 1.501
        snap = d.get_live_data()
        for k in ("candles", "current", "best_bid", "best_ask",
                  "bid_ask_spread", "orderbook_imbalance", "volume_delta",
                  "funding_rate", "latest_tick", "is_warmup"):
            assert k in snap

    def test_includes_all_configured_timeframes(self, dr):
        d, _ = dr
        snap = d.get_live_data()
        # Semua TF di config muncul
        for tf in d.candle_buffers:
            assert tf in snap["candles"]
            assert tf in snap["current"]

    def test_current_is_none_when_no_tick(self, dr):
        d, _ = dr
        snap = d.get_live_data()
        for tf in snap["current"]:
            assert snap["current"][tf] is None

    def test_warmup_flag_propagated(self, dr, monkeypatch):
        d, _ = dr
        monkeypatch.setattr(d, "is_warmup", True)
        assert d.get_live_data()["is_warmup"] is True


# =============================================================================
# _close_candle — appended ke buffer
# =============================================================================
class TestCloseCandle:
    def test_candle_appended_to_buffer(self, dr):
        d, _ = dr
        c = d._new_candle("1m", 1.5, 100, 1700000060.0)
        d._close_candle("1m", c)
        assert len(d.candle_buffers["1m"]) == 1
        assert d.candle_buffers["1m"][0]["open"] == 1.5

    def test_close_candle_logs_when_2h(self, dr, caplog):
        d, _ = dr
        import logging
        c = d._new_candle("2h", 1.5, 100, 1700000000.0)
        with caplog.at_level(logging.INFO, logger="data_resampler"):
            d._close_candle("2h", c)
        # Log emits NEW 2h CANDLE saat tidak warmup
        assert any("2h" in rec.message for rec in caplog.records)