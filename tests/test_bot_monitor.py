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

    def test_returns_dict_when_file_exists(self, bm, tmp_path):
        """Setelah record_tick + flush, read_health bisa parse file balik."""
        bm.record_tick({"action": "hold", "reason": "test"})
        # Force flush (bypass throttle yang state-nya module-level)
        bm._last_flush_time = 0.0
        bm._flush_to_file(force=True)
        # HEALTH_FILE absolut → pakai path="" supaya os.path.join return apa adanya
        result = bm.read_health(path="")
        assert isinstance(result, dict)
        assert "status" in result
        assert "total_ticks" in result
        assert result["total_ticks"] == 1

    def test_read_health_handles_corrupted_file(self, bm, tmp_path):
        """File JSON corrupt → return {} (don't crash dashboard)."""
        bad_file = tmp_path / "bot_health.json"
        bad_file.write_text("{ not valid json", encoding="utf-8")
        result = bm.read_health(path=str(tmp_path))
        assert result == {}


# =============================================================================
# BALANCE ANOMALY DETECTION (Deteksi #2)
# =============================================================================
class TestBalanceAnomaly:
    def test_equity_peak_tracks_max(self, bm):
        bm.record_tick({"action": "hold"}, equity=1000.0, position_side="none")
        bm.record_tick({"action": "hold"}, equity=1200.0, position_side="none")
        bm.record_tick({"action": "hold"}, equity=1100.0, position_side="none")
        assert bm._state["equity_peak"] == 1200.0

    def test_drop_below_threshold_flags_anomaly(self, bm):
        """Equity turun > 5% dari peak → anomaly string ter-set."""
        bm.record_tick({"action": "hold"}, equity=1000.0, position_side="none")
        # Drop 10% dari peak → > 5% threshold
        bm.record_tick({"action": "hold"}, equity=900.0, position_side="none")
        assert bm._state["equity_anomaly"] != ""
        assert "10" in bm._state["equity_anomaly"] or "turun" in bm._state["equity_anomaly"].lower()

    def test_small_drop_within_threshold_no_anomaly(self, bm):
        bm.record_tick({"action": "hold"}, equity=1000.0, position_side="none")
        # Drop 3% — di bawah threshold 5%
        bm.record_tick({"action": "hold"}, equity=970.0, position_side="none")
        assert bm._state["equity_anomaly"] == ""

    def test_anomaly_appears_in_warnings_via_evaluate_status(self, bm):
        bm.record_tick({"action": "hold"}, equity=1000.0, position_side="none")
        bm.record_tick({"action": "hold"}, equity=800.0, position_side="none")  # turun 20%
        # Status harus WARN dengan reason berisi Balance
        assert bm._state["status"] in ("WARN", "ERROR")
        assert any("Balance" in w for w in bm._state["extra_warnings"])


# =============================================================================
# SIGNAL EXECUTION CHECK (Deteksi #3)
# =============================================================================
class TestSignalExecutionCheck:
    def test_buy_then_position_long_clears_pending(self, bm):
        bm.record_tick({"action": "buy"}, position_side="none")
        assert bm._state["pending_action"] == "buy"
        # Tick berikutnya posisi sudah long → pending cleared
        bm.record_tick({"action": "hold"}, position_side="long")
        assert bm._state["pending_action"] is None
        assert bm._state["exec_miss_count"] == 0

    def test_buy_not_executed_after_grace_ticks_counted_as_miss(self, bm):
        bm.record_tick({"action": "buy"}, position_side="none")
        # 2 tick berikutnya position tetap none → miss
        bm.record_tick({"action": "hold"}, position_side="none")
        bm.record_tick({"action": "hold"}, position_side="none")
        assert bm._state["exec_miss_count"] >= 1
        assert "buy" in bm._state["last_exec_miss"].lower()

    def test_sell_then_position_short_clears_pending(self, bm):
        bm.record_tick({"action": "sell"}, position_side="none")
        bm.record_tick({"action": "hold"}, position_side="short")
        assert bm._state["pending_action"] is None
        assert bm._state["exec_miss_count"] == 0

    def test_close_then_position_none_clears_pending(self, bm):
        bm.record_tick({"action": "close"}, position_side="long")
        bm.record_tick({"action": "hold"}, position_side="none")
        assert bm._state["pending_action"] is None

    def test_miss_appears_in_warnings(self, bm):
        bm.record_tick({"action": "buy"}, position_side="none")
        bm.record_tick({"action": "hold"}, position_side="none")
        bm.record_tick({"action": "hold"}, position_side="none")
        assert any("Eksekusi" in w for w in bm._state["extra_warnings"])


# =============================================================================
# DATA QUALITY — stale candle, wide spread, buffer overflow
# =============================================================================
class TestDataQualityEdgeCases:
    def test_stale_candle_flagged(self, bm):
        """Candle dengan open_time jauh di masa lalu → 'STALE' issue."""
        old_time = time.time() - 86400  # 1 hari lalu
        data = {
            "candles": {"15m": [{"open_time": old_time, "close": 1.5}]},
            "best_bid": {"price": 1.5}, "best_ask": {"price": 1.501},
        }
        bm.record_tick({"action": "hold"}, data=data)
        assert any("STALE" in iss for iss in bm._state["data_quality"]["data_issues"])

    def test_candle_missing_open_time_flagged(self, bm):
        data = {
            "candles": {"15m": [{"close": 1.5}]},  # no open_time
            "best_bid": {"price": 1.5}, "best_ask": {"price": 1.501},
        }
        bm.record_tick({"action": "hold"}, data=data)
        assert any("open_time" in iss for iss in bm._state["data_quality"]["data_issues"])

    def test_wide_spread_flagged(self, bm):
        """Spread > 1% → issue tercatat."""
        data = {
            "candles": {},
            "best_bid": {"price": 1.0},
            "best_ask": {"price": 1.05},  # spread 5%
        }
        bm.record_tick({"action": "hold"}, data=data)
        assert bm._state["data_quality"]["spread_pct"] > 1.0
        assert any("Spread" in iss for iss in bm._state["data_quality"]["data_issues"])

    def test_buffer_overflow_flagged(self, bm):
        """Jumlah candle > limit × 1.5 → overflow."""
        # 15m limit di config = 50, overflow threshold = 75
        many = [{"open_time": time.time(), "close": 1.5}] * 200
        data = {
            "candles": {"15m": many},
            "best_bid": {"price": 1.5}, "best_ask": {"price": 1.501},
        }
        bm.record_tick({"action": "hold"}, data=data)
        assert bm._state["buffer_overflow"].get("15m") is True
        assert any("OVERFLOW" in iss for iss in bm._state["data_quality"]["data_issues"])


# =============================================================================
# NO TRADE WARNING
# =============================================================================
class TestNoTradeWarning:
    def test_long_idle_after_trade_triggers_warning(self, bm):
        """last_trade_time > 3 jam lalu → warning."""
        # Set last_trade_time manual ke 4 jam lalu
        bm._state["last_trade_time"] = time.time() - 4 * 3600
        bm.record_tick({"action": "hold"})
        assert bm._state["status"] == "WARN"
        assert any("Trade" in w for w in bm._state["extra_warnings"])


# =============================================================================
# WATCHDOG
# =============================================================================
class TestWatchdog:
    def test_start_and_stop_watchdog(self, bm):
        """Start & stop watchdog thread tidak boleh crash."""
        bm.start_watchdog()
        assert bm._watchdog_thread is not None
        assert bm._watchdog_thread.is_alive()
        bm.stop_watchdog()
        bm._watchdog_thread.join(timeout=12)  # tunggu loop selesai (sleep 10s)
        assert not bm._watchdog_thread.is_alive()

    def test_start_watchdog_idempotent(self, bm):
        """Panggil start_watchdog 2x → tidak buat thread kedua."""
        bm.start_watchdog()
        first = bm._watchdog_thread
        bm.start_watchdog()  # should no-op
        assert bm._watchdog_thread is first
        bm.stop_watchdog()
        bm._watchdog_thread.join(timeout=12)


# =============================================================================
# FLUSH — avg_prob calc + write succeeds
# =============================================================================
class TestFlush:
    def test_avg_ml_prob_included_in_payload(self, bm, monkeypatch):
        """avg_prob harus muncul di payload kalau ada history ML."""
        import json
        bm.record_tick({"action": "hold"}, ml_prob=0.4)
        bm.record_tick({"action": "hold"}, ml_prob=0.6)
        # Force flush (bypass throttle)
        bm._last_flush_time = 0
        bm._flush_to_file(force=True)
        # Read back & verify
        with open(bm.HEALTH_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        assert payload["avg_ml_prob_20"] == pytest.approx(0.5, abs=0.01)

    def test_flush_throttle_skips_recent_call(self, bm):
        """flush yang dipanggil < FLUSH_MIN_INTERVAL setelah yang terakhir → skip."""
        import os
        # Pertama: flush sukses
        bm._last_flush_time = 0
        bm._flush_to_file()
        first_mtime = os.path.getmtime(bm.HEALTH_FILE)
        # Kedua: langsung flush lagi (< 3 detik) → tidak tulis
        bm._flush_to_file()
        second_mtime = os.path.getmtime(bm.HEALTH_FILE)
        assert first_mtime == second_mtime
