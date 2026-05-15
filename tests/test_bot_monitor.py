# =============================================================================
# tests/test_bot_monitor.py — Unit test untuk health tracker
# =============================================================================
# Cover: record_tick, record_error, status evaluation, ML drift detection
# =============================================================================
from __future__ import annotations

import time
from collections import deque

import pytest


@pytest.fixture
def bm(monkeypatch, tmp_path):
    """Fresh import bot_monitor dengan state direset & file write redirect ke tmp."""
    import bot_monitor as _bm

    # Redirect file writes ke tmpdir (jangan kotori bot_health.json asli)
    monkeypatch.setattr(_bm, "HEALTH_FILE", str(tmp_path / "test_health.json"))

    # Reset state
    _bm._state["start_time"] = time.time()
    _bm._state["last_tick_time"] = 0.0
    _bm._state["last_trade_time"] = 0.0
    _bm._state["total_ticks"] = 0
    _bm._state["signal_counts"] = {"buy": 0, "sell": 0, "close": 0, "hold": 0, "error": 0}
    _bm._state["consecutive_errors"] = 0
    _bm._state["total_errors"] = 0
    _bm._state["last_error_reason"] = ""
    _bm._state["last_ml_prob"] = None
    _bm._state["ml_prob_history"] = deque(maxlen=30)
    _bm._state["status"] = "STARTING"
    _bm._state["status_reason"] = ""
    _bm._state["extra_warnings"] = []
    _bm._state["data_quality"] = {
        "candles_per_tf": {}, "last_candle_age_sec": {},
        "bid_price": 0.0, "ask_price": 0.0,
        "bid_valid": False, "ask_valid": False,
        "spread_pct": 0.0, "data_issues": [],
    }
    _bm._state["ml_drift_detected"] = False
    _bm._state["ml_drift_std"] = None
    _bm._state["equity_history"] = deque(maxlen=120)
    _bm._state["equity_peak"] = 0.0
    _bm._state["equity_anomaly"] = ""
    _bm._state["pending_action"] = None
    _bm._state["pending_since_ticks"] = 0
    _bm._state["exec_miss_count"] = 0
    _bm._state["last_exec_miss"] = ""
    _bm._state["buffer_overflow"] = {}
    return _bm


# =============================================================================
# RECORD_TICK — fungsi inti yang dipanggil tiap tick
# =============================================================================
class TestRecordTick:
    def test_increments_total_ticks(self, bm):
        bm.record_tick({"action": "hold", "reason": "test"})
        assert bm._state["total_ticks"] == 1
        bm.record_tick({"action": "hold", "reason": "test"})
        assert bm._state["total_ticks"] == 2

    def test_signal_counts_distribution(self, bm):
        bm.record_tick({"action": "hold"})
        bm.record_tick({"action": "buy"})
        bm.record_tick({"action": "buy"})
        bm.record_tick({"action": "sell"})
        bm.record_tick({"action": "close"})
        assert bm._state["signal_counts"]["hold"] == 1
        assert bm._state["signal_counts"]["buy"] == 2
        assert bm._state["signal_counts"]["sell"] == 1
        assert bm._state["signal_counts"]["close"] == 1

    def test_error_in_reason_counted_as_error(self, bm):
        bm.record_tick({"action": "hold", "reason": "Error: something broke"})
        assert bm._state["signal_counts"]["error"] == 1
        assert bm._state["consecutive_errors"] == 1

    def test_consecutive_errors_reset_on_success(self, bm):
        bm.record_tick({"action": "hold", "reason": "Error: bad"})
        bm.record_tick({"action": "hold", "reason": "Error: bad"})
        assert bm._state["consecutive_errors"] == 2
        bm.record_tick({"action": "hold", "reason": "all good"})
        assert bm._state["consecutive_errors"] == 0

    def test_last_trade_time_updated_on_action(self, bm):
        before = bm._state["last_trade_time"]
        bm.record_tick({"action": "hold"})
        assert bm._state["last_trade_time"] == before  # hold ≠ trade
        bm.record_tick({"action": "buy"})
        assert bm._state["last_trade_time"] > before

    def test_ml_prob_appended_to_history(self, bm):
        bm.record_tick({"action": "hold"}, ml_prob=0.65)
        bm.record_tick({"action": "hold"}, ml_prob=0.70)
        assert bm._state["last_ml_prob"] == 0.70
        assert list(bm._state["ml_prob_history"]) == [0.65, 0.70]

    def test_status_transitions_from_starting_to_ok(self, bm):
        assert bm._state["status"] == "STARTING"
        bm.record_tick({"action": "hold", "reason": "no signal"})
        # Setelah tick valid pertama tanpa data issues, status jadi OK
        assert bm._state["status"] in ("OK", "WARN")
        assert bm._state["status"] != "STARTING"


# =============================================================================
# RECORD_ERROR — dipanggil dari except block
# =============================================================================
class TestRecordError:
    def test_increments_error_counters(self, bm):
        bm.record_error("crash in generate_signal")
        assert bm._state["total_errors"] == 1
        assert bm._state["consecutive_errors"] == 1
        assert bm._state["last_error_reason"] == "crash in generate_signal"

    def test_max_consecutive_triggers_error_status(self, bm):
        # MAX_CONSECUTIVE_ERR = 5 (lihat config bot_monitor)
        for _ in range(bm.MAX_CONSECUTIVE_ERR):
            bm.record_error("bad")
        assert bm._state["status"] == "ERROR"


# =============================================================================
# DATA QUALITY ANALYSIS
# =============================================================================
class TestDataQuality:
    def test_bid_ask_valid_when_positive(self, bm):
        data = {
            "candles": {"2h": [{"open_time": time.time(), "close": 1.5}]},
            "best_bid": {"price": 1.5},
            "best_ask": {"price": 1.501},
        }
        bm.record_tick({"action": "hold"}, data=data)
        assert bm._state["data_quality"]["bid_valid"] is True
        assert bm._state["data_quality"]["ask_valid"] is True

    def test_bid_zero_flagged_invalid(self, bm):
        data = {
            "candles": {},
            "best_bid": {"price": 0.0},
            "best_ask": {"price": 0.0},
        }
        bm.record_tick({"action": "hold"}, data=data)
        assert bm._state["data_quality"]["bid_valid"] is False
        assert bm._state["data_quality"]["ask_valid"] is False
        assert any("best_bid" in iss for iss in bm._state["data_quality"]["data_issues"])

    def test_candles_per_tf_populated(self, bm):
        data = {
            "candles": {"2h": [{"open_time": time.time(), "close": 1.5}] * 50},
            "best_bid": {"price": 1.5}, "best_ask": {"price": 1.501},
        }
        bm.record_tick({"action": "hold"}, data=data)
        assert bm._state["data_quality"]["candles_per_tf"]["2h"] == 50


# =============================================================================
# ML DRIFT DETECTION
# =============================================================================
class TestModelDrift:
    def test_no_drift_with_few_samples(self, bm):
        for _ in range(10):
            bm.record_tick({"action": "hold"}, ml_prob=0.5)
        # MIN_SAMPLES = 30, jadi belum cukup untuk deteksi
        assert bm._state["ml_drift_detected"] is False

    def test_drift_detected_when_prob_constant(self, bm):
        # Isi 30 kali dengan nilai sama → std = 0 → drift
        for _ in range(30):
            bm.record_tick({"action": "hold"}, ml_prob=0.5)
        assert bm._state["ml_drift_detected"] is True

    def test_no_drift_when_prob_varies(self, bm):
        import random
        random.seed(0)
        for _ in range(30):
            # variasi besar → std > threshold
            bm.record_tick({"action": "hold"}, ml_prob=random.uniform(0.3, 0.7))
        assert bm._state["ml_drift_detected"] is False


# =============================================================================
# READ HEALTH
# =============================================================================
class TestReadHealth:
    def test_returns_empty_dict_when_no_file(self, bm, tmp_path):
        result = bm.read_health(path=str(tmp_path / "nonexistent"))
        assert result == {}
