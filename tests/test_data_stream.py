# =============================================================================
# tests/test_data_stream.py — CCXT Pro orderbook & trade processors
# =============================================================================
# Module ini punya 3 lapis fungsi:
#   1. Pure state mutator (no I/O):
#        _update_best_bid_ask, _process_ccxt_orderbook, _process_ccxt_trades
#   2. Async one-shot (mock exchange):
#        _fetch_funding_rate, _heartbeat_loop (1 iterasi)
#   3. Async infinite loop (skip - butuh integration test):
#        _watch_orderbook_loop, _watch_trades_loop, start_data_stream
#
# Test fokus ke layer 1 & 2 — layer 3 cuma loop forever yang panggil layer 1.
# =============================================================================
from __future__ import annotations

import asyncio
import json
from collections import deque
from unittest.mock import AsyncMock

import pytest
from sortedcontainers import SortedDict


@pytest.fixture
def ds(monkeypatch, tmp_path):
    """Reset data_stream shared state per test, redirect heartbeat ke tmp."""
    import data_stream as _ds

    # Reset orderbook & best bid/ask
    monkeypatch.setattr(_ds, "best_bid", {"price": 0.0, "qty": 0.0})
    monkeypatch.setattr(_ds, "best_ask", {"price": 0.0, "qty": 0.0})
    monkeypatch.setattr(_ds, "best_bid_updated_at", 0.0)
    monkeypatch.setattr(_ds, "best_ask_updated_at", 0.0)
    monkeypatch.setattr(_ds, "best_bid_ts_event", 0.0)
    monkeypatch.setattr(_ds, "best_ask_ts_event", 0.0)
    monkeypatch.setattr(_ds, "orderbook_snapshot", {
        "bids": SortedDict(), "asks": SortedDict(),
    })
    monkeypatch.setattr(_ds, "orderbook_buffer", deque(maxlen=100))
    monkeypatch.setattr(_ds, "tick_buffer", deque(maxlen=10000))
    monkeypatch.setattr(_ds, "funding_rate",
                        {"value": 0.0, "next_funding_time": 0, "predicted": 0.0})
    monkeypatch.setattr(_ds, "new_tick_event", None)

    # Heartbeat file redirect
    monkeypatch.setattr(_ds, "HEARTBEAT_FILE", str(tmp_path / "heartbeat.json"))
    return _ds


# =============================================================================
# _update_best_bid_ask — pull best level dari SortedDict snapshot
# =============================================================================
class TestUpdateBestBidAsk:
    def test_picks_highest_bid_and_lowest_ask(self, ds):
        # SortedDict items() ascending → bids.peekitem(-1)=tertinggi, asks.peekitem(0)=terendah
        ds.orderbook_snapshot["bids"][1.498] = 200.0
        ds.orderbook_snapshot["bids"][1.500] = 500.0
        ds.orderbook_snapshot["asks"][1.501] = 400.0
        ds.orderbook_snapshot["asks"][1.503] = 100.0

        ds._update_best_bid_ask()

        assert ds.best_bid["price"] == 1.500
        assert ds.best_bid["qty"] == 500.0
        assert ds.best_ask["price"] == 1.501
        assert ds.best_ask["qty"] == 400.0

    def test_updates_timestamps(self, ds):
        ds.orderbook_snapshot["bids"][1.5] = 100.0
        ds.orderbook_snapshot["asks"][1.51] = 100.0
        assert ds.best_bid_updated_at == 0.0
        ds._update_best_bid_ask()
        assert ds.best_bid_updated_at > 0
        assert ds.best_ask_updated_at > 0

    def test_empty_book_does_not_update_timestamps(self, ds):
        """Book kosong → best_bid/ask tidak boleh ditimpa ke harga lama yang invalid."""
        ds._update_best_bid_ask()
        assert ds.best_bid_updated_at == 0.0
        assert ds.best_ask_updated_at == 0.0
        assert ds.best_bid["price"] == 0.0


# =============================================================================
# _process_ccxt_orderbook — bangun snapshot dari CCXT Pro orderbook dict
# =============================================================================
class TestProcessOrderbook:
    def test_typical_ccxt_response_processed(self, ds):
        ob = {
            "bids": [[1.500, 500.0], [1.499, 400.0], [1.498, 300.0]],
            "asks": [[1.501, 500.0], [1.502, 400.0], [1.503, 300.0]],
        }
        ds._process_ccxt_orderbook(ob)
        # Best bid/ask di-update
        assert ds.best_bid["price"] == 1.500
        assert ds.best_ask["price"] == 1.501
        # Snapshot terisi semua level
        assert len(ds.orderbook_snapshot["bids"]) == 3
        assert len(ds.orderbook_snapshot["asks"]) == 3
        # Buffer di-push 1 snapshot
        assert len(ds.orderbook_buffer) == 1

    def test_zero_qty_levels_filtered(self, ds):
        """Level dengan qty=0 (delete signal dari exchange) tidak boleh masuk."""
        ob = {
            "bids": [[1.500, 0], [1.499, 400.0]],
            "asks": [[1.501, 500.0], [1.502, 0]],
        }
        ds._process_ccxt_orderbook(ob)
        assert 1.500 not in ds.orderbook_snapshot["bids"]
        assert 1.499 in ds.orderbook_snapshot["bids"]
        assert 1.502 not in ds.orderbook_snapshot["asks"]

    def test_missing_bids_asks_handled(self, ds):
        """ob tanpa 'bids' atau 'asks' tidak crash."""
        ds._process_ccxt_orderbook({})
        # Snapshot tetap kosong, tidak crash
        assert len(ds.orderbook_snapshot["bids"]) == 0
        assert len(ds.orderbook_snapshot["asks"]) == 0

    def test_string_price_qty_coerced(self, ds):
        """CCXT kadang return string — harus dikonversi ke float."""
        ob = {
            "bids": [["1.500", "500.0"]],
            "asks": [["1.501", "500.0"]],
        }
        ds._process_ccxt_orderbook(ob)
        assert ds.best_bid["price"] == 1.500
        assert ds.best_ask["price"] == 1.501

    def test_clears_old_snapshot_before_refresh(self, ds):
        """Snapshot baru harus REPLACE lama, bukan accumulate."""
        ds.orderbook_snapshot["bids"][1.0] = 999.0  # stale entry
        ds._process_ccxt_orderbook({
            "bids": [[1.500, 100.0]], "asks": [[1.501, 100.0]],
        })
        # 1.0 harus hilang
        assert 1.0 not in ds.orderbook_snapshot["bids"]

    def test_snapshot_pushed_to_buffer_has_correct_keys(self, ds):
        ob = {"bids": [[1.5, 100.0]], "asks": [[1.51, 100.0]]}
        ds._process_ccxt_orderbook(ob)
        snap = ds.orderbook_buffer[-1]
        for k in ("timestamp", "bids", "asks", "best_bid", "best_ask"):
            assert k in snap


# =============================================================================
# ts_event / ts_init — dua timestamp ala Nautilus (pola data handler)
# =============================================================================
class TestTsEvent:
    def test_update_sets_ts_event_from_param(self, ds):
        ds.orderbook_snapshot["bids"][1.5] = 100.0
        ds.orderbook_snapshot["asks"][1.51] = 100.0
        ds._update_best_bid_ask(event_ts=1700000000.0)
        assert ds.best_bid_ts_event == 1700000000.0   # ts_event (waktu exchange)
        assert ds.best_ask_ts_event == 1700000000.0
        assert ds.best_bid_updated_at > 0             # ts_init (waktu terima)

    def test_update_default_event_ts_zero(self, ds):
        ds.orderbook_snapshot["bids"][1.5] = 100.0
        ds.orderbook_snapshot["asks"][1.51] = 100.0
        ds._update_best_bid_ask()  # tanpa event_ts
        assert ds.best_bid_ts_event == 0.0

    def test_process_orderbook_extracts_timestamp_ms_to_sec(self, ds):
        ob = {"timestamp": 1700000000000, "bids": [[1.5, 100.0]], "asks": [[1.51, 100.0]]}
        ds._process_ccxt_orderbook(ob)
        assert ds.best_bid_ts_event == pytest.approx(1700000000.0)  # ms / 1000

    def test_process_orderbook_missing_timestamp_zero(self, ds):
        ds._process_ccxt_orderbook({"bids": [[1.5, 100.0]], "asks": [[1.51, 100.0]]})
        assert ds.best_bid_ts_event == 0.0

    def test_process_orderbook_none_timestamp_zero(self, ds):
        ob = {"timestamp": None, "bids": [[1.5, 100.0]], "asks": [[1.51, 100.0]]}
        ds._process_ccxt_orderbook(ob)
        assert ds.best_bid_ts_event == 0.0

    def test_process_orderbook_invalid_timestamp_zero(self, ds):
        """timestamp non-numerik tidak boleh crash → fallback 0.0."""
        ob = {"timestamp": "bukan-angka", "bids": [[1.5, 100.0]], "asks": [[1.51, 100.0]]}
        ds._process_ccxt_orderbook(ob)
        assert ds.best_bid_ts_event == 0.0

    def test_data_latency_ms(self, ds, monkeypatch):
        monkeypatch.setattr(ds, "best_bid_ts_event", 1000.0)
        monkeypatch.setattr(ds, "best_bid_updated_at", 1001.0)  # terima 1s setelah event
        assert ds.data_latency_ms() == pytest.approx(1000.0)

    def test_data_latency_none_without_ts_event(self, ds, monkeypatch):
        monkeypatch.setattr(ds, "best_bid_ts_event", 0.0)
        monkeypatch.setattr(ds, "best_bid_updated_at", 1001.0)
        assert ds.data_latency_ms() is None


# =============================================================================
# _process_ccxt_trades — convert CCXT trade list ke TickData
# =============================================================================
class TestProcessTrades:
    def test_typical_trades_pushed_to_buffer(self, ds):
        trades = [
            {"timestamp": 1700000000000, "price": 1.5, "amount": 100, "side": "buy",  "id": "1"},
            {"timestamp": 1700000001000, "price": 1.51, "amount": 50,  "side": "sell", "id": "2"},
        ]
        ds._process_ccxt_trades(trades)
        assert len(ds.tick_buffer) == 2
        # Side normalisasi: buy → Buy, sell → Sell
        assert ds.tick_buffer[0]["side"] == "Buy"
        assert ds.tick_buffer[1]["side"] == "Sell"

    def test_timestamp_converted_ms_to_sec(self, ds):
        ds._process_ccxt_trades([
            {"timestamp": 1700000000000, "price": 1.5, "amount": 100, "side": "buy"},
        ])
        tick = ds.tick_buffer[0]
        # ms / 1000 = s
        assert tick["timestamp"] == 1700000000.0

    def test_empty_trades_list_no_op(self, ds):
        ds._process_ccxt_trades([])
        assert len(ds.tick_buffer) == 0

    def test_missing_id_handled(self, ds):
        ds._process_ccxt_trades([
            {"timestamp": 1700000000000, "price": 1.5, "amount": 100, "side": "buy"},
        ])
        # trade_id default ke "" jika missing
        assert ds.tick_buffer[0]["trade_id"] == ""

    def test_new_tick_event_set_when_trades_arrive(self, ds):
        ds.new_tick_event = asyncio.Event()
        assert not ds.new_tick_event.is_set()
        ds._process_ccxt_trades([
            {"timestamp": 1700000000000, "price": 1.5, "amount": 100, "side": "buy"},
        ])
        assert ds.new_tick_event.is_set()

    def test_new_tick_event_not_set_when_no_trades(self, ds):
        ds.new_tick_event = asyncio.Event()
        ds._process_ccxt_trades([])
        assert not ds.new_tick_event.is_set()


# =============================================================================
# _fetch_funding_rate — REST call via CCXT
# =============================================================================
class TestFetchFundingRate:
    def test_updates_funding_rate_state(self, ds, monkeypatch):
        async def _fake_fetch(symbol):
            return {"fundingRate": 0.0001}
        monkeypatch.setattr(ds.exchange, "fetch_funding_rate", _fake_fetch, raising=False)
        asyncio.run(ds._fetch_funding_rate())
        assert ds.funding_rate["value"] == pytest.approx(0.0001)

    def test_missing_funding_rate_defaults_to_zero(self, ds, monkeypatch):
        async def _fake_fetch(symbol):
            return {}  # no fundingRate key
        monkeypatch.setattr(ds.exchange, "fetch_funding_rate", _fake_fetch, raising=False)
        asyncio.run(ds._fetch_funding_rate())
        assert ds.funding_rate["value"] == 0.0

    def test_exception_swallowed(self, ds, monkeypatch, caplog):
        async def _boom(symbol):
            raise RuntimeError("API down")
        monkeypatch.setattr(ds.exchange, "fetch_funding_rate", _boom, raising=False)
        import logging
        with caplog.at_level(logging.WARNING, logger="data_stream"):
            asyncio.run(ds._fetch_funding_rate())  # tidak boleh raise
        assert any("Funding rate fetch error" in r.message for r in caplog.records)


# =============================================================================
# _heartbeat_loop — 1 iterasi (test heartbeat write, lalu cancel)
# =============================================================================
class TestHeartbeatLoop:
    def test_writes_heartbeat_json_one_iteration(self, ds, monkeypatch, tmp_path):
        """Run 1 iterasi heartbeat lalu cancel; verifikasi file ditulis."""
        # Speed up sleep interval supaya test cepat
        monkeypatch.setattr(ds, "HEARTBEAT_INTERVAL_SEC", 0.01)

        async def _run_once():
            task = asyncio.create_task(ds._heartbeat_loop())
            await asyncio.sleep(0.05)  # biar minimal 1 iterasi jalan
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_run_once())

        # File harus exist dan valid JSON
        import os
        assert os.path.exists(ds.HEARTBEAT_FILE)
        with open(ds.HEARTBEAT_FILE, "r") as f:
            payload = json.load(f)
        assert "last_update" in payload
        assert "last_prices" in payload


# =============================================================================
# _watch_orderbook_loop — 1 iterasi pakai mock exchange.watch_order_book
# =============================================================================
# NOTE: mock fungsi async harus `await asyncio.sleep(0)` supaya event loop
# punya kesempatan switch ke task lain (cancel signal). Tanpa ini, loop spin
# forever karena `_fake_watch` immediately resolves dan loop tidak pernah yield.
class TestWatchOrderbookLoop:
    def test_processes_orderbook_then_cancellable(self, ds, monkeypatch):
        call_count = {"n": 0}
        async def _fake_watch(symbol, limit):
            call_count["n"] += 1
            await asyncio.sleep(0)  # yield to event loop
            return {"bids": [[1.5, 100.0]], "asks": [[1.51, 100.0]]}

        monkeypatch.setattr(ds.exchange, "watch_order_book", _fake_watch, raising=False)

        async def _run():
            task = asyncio.create_task(ds._watch_orderbook_loop())
            await asyncio.sleep(0.02)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(asyncio.wait_for(_run(), timeout=2.0))
        assert call_count["n"] >= 1
        assert ds.best_bid["price"] == 1.5

    def test_exception_logged_and_loop_retries(self, ds, monkeypatch, caplog):
        """watch_order_book raise → log error, retry. Test pakai timeout untuk safety."""
        calls = {"n": 0}
        async def _flaky(symbol, limit):
            calls["n"] += 1
            await asyncio.sleep(0)  # yield
            raise RuntimeError("transient")

        monkeypatch.setattr(ds.exchange, "watch_order_book", _flaky, raising=False)

        async def _run():
            task = asyncio.create_task(ds._watch_orderbook_loop())
            await asyncio.sleep(0.05)  # tunggu beberapa retry
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        import logging
        with caplog.at_level(logging.ERROR, logger="data_stream"):
            asyncio.run(asyncio.wait_for(_run(), timeout=3.0))

        # Error harus terlog (retry behavior triggered)
        assert any("Orderbook watch error" in r.message for r in caplog.records)


# =============================================================================
# _watch_trades_loop — sama pattern dengan orderbook loop
# =============================================================================
class TestWatchTradesLoop:
    def test_processes_trades_one_iter(self, ds, monkeypatch):
        async def _fake_watch(symbol):
            await asyncio.sleep(0)  # yield
            return [
                {"timestamp": 1700000000000, "price": 1.5, "amount": 100, "side": "buy", "id": "x"},
            ]
        monkeypatch.setattr(ds.exchange, "watch_trades", _fake_watch, raising=False)

        async def _run():
            task = asyncio.create_task(ds._watch_trades_loop())
            await asyncio.sleep(0.02)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(asyncio.wait_for(_run(), timeout=2.0))
        assert len(ds.tick_buffer) >= 1