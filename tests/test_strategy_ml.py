# =============================================================================
# tests/test_strategy_ml.py — Error handling untuk strategy_ml.py
# =============================================================================
# Cover: silent-hold bug fix → narrow try/except, classify error, auto-disable.
# =============================================================================
from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from tests.conftest import make_candles


def _make_ml_data(tf: str = "15m", n_candles: int = 60) -> dict:
    """Snapshot data dengan jumlah candle cukup untuk lewat MIN_CANDLES=50."""
    candles = make_candles(n=n_candles, tf=tf)
    current = candles[-1]
    return {
        "candles":        {tf: candles[:-1]},
        "current":        {tf: current},
        "best_bid":       {"price": current["close"] * 0.9999, "qty": 1.0},
        "best_ask":       {"price": current["close"] * 1.0001, "qty": 1.0},
        "bid_ask_spread": current["close"] * 0.0002,
        "funding_rate":   0.0001,
        "latest_tick":    None,
        "is_warmup":      False,
    }


@pytest.fixture(autouse=True)
def reset_strategy_ml_state(monkeypatch):
    """Auto-reset module state untuk tiap test (no cross-contamination)."""
    import strategy_ml
    monkeypatch.setattr(strategy_ml, "_ML_PERMANENTLY_DISABLED", False)
    monkeypatch.setattr(strategy_ml, "_ML_DISABLE_REASON", "")
    # Mutable dicts: reset manual (monkeypatch tidak revert in-place mutation)
    strategy_ml._feat_cache["key"] = None
    strategy_ml._feat_cache["baris_terbaru"] = None
    strategy_ml.bot_state["in_position"] = False
    strategy_ml.bot_state["side"]        = "none"
    strategy_ml.bot_state["entry_price"] = 0.0
    strategy_ml.bot_state["entry_time"]  = 0.0
    yield


def _inject_working_ml(monkeypatch, prob_naik: float = 0.5, prob_turun: float = 0.5):
    """Pasang mock scaler + model yang return probabilitas tertentu."""
    import strategy_ml
    mock_scaler = MagicMock()
    mock_scaler.transform.return_value = np.array([[0.5] * 12])
    mock_model = MagicMock()
    mock_model.predict_proba.return_value = np.array([[prob_turun, prob_naik]])
    monkeypatch.setattr(strategy_ml, "scaler", mock_scaler)
    monkeypatch.setattr(strategy_ml, "model", mock_model)
    monkeypatch.setattr(strategy_ml, "ML_ENABLED", True)
    return mock_scaler, mock_model


# =============================================================================
# GUARDS — kondisi yang harus return hold tanpa mencoba inference
# =============================================================================
class TestPreflightGuards:
    def test_warmup_returns_hold(self):
        import strategy_ml
        data = _make_ml_data()
        data["is_warmup"] = True
        result = strategy_ml.generate_signal(data)
        assert result["action"] == "hold"
        assert "Warmup" in result["reason"]

    def test_ml_disabled_at_startup_returns_hold(self, monkeypatch):
        import strategy_ml
        monkeypatch.setattr(strategy_ml, "ML_ENABLED", False)
        result = strategy_ml.generate_signal(_make_ml_data())
        assert result["action"] == "hold"
        assert "tidak aktif" in result["reason"]

    def test_permanently_disabled_short_circuits(self, monkeypatch):
        """Setelah _ML_PERMANENTLY_DISABLED=True, tidak boleh sentuh scaler/model lagi."""
        import strategy_ml
        # Kalau scaler/model di-akses, AttributeError akan keluar (None has no .transform).
        # Test pass berarti short-circuit kerja — tidak pernah sampai inference block.
        monkeypatch.setattr(strategy_ml, "scaler", None)
        monkeypatch.setattr(strategy_ml, "model", None)
        monkeypatch.setattr(strategy_ml, "ML_ENABLED", True)
        monkeypatch.setattr(strategy_ml, "_ML_PERMANENTLY_DISABLED", True)
        monkeypatch.setattr(strategy_ml, "_ML_DISABLE_REASON", "test disable")
        result = strategy_ml.generate_signal(_make_ml_data())
        assert result["action"] == "hold"
        assert "ML nonaktif" in result["reason"]
        assert "test disable" in result["reason"]


# =============================================================================
# ERROR HANDLING — distinguish recoverable vs permanent
# =============================================================================
class TestPermanentErrors:
    """Permanent error → disable ML untuk sisa sesi."""

    def test_value_error_in_scaler_disables_ml(self, monkeypatch):
        import strategy_ml
        mock_scaler = MagicMock()
        mock_scaler.transform.side_effect = ValueError(
            "X has 5 features, scaler was trained on 12"
        )
        monkeypatch.setattr(strategy_ml, "scaler", mock_scaler)
        monkeypatch.setattr(strategy_ml, "model", MagicMock())
        monkeypatch.setattr(strategy_ml, "ML_ENABLED", True)

        result = strategy_ml.generate_signal(_make_ml_data())
        assert result["action"] == "hold"
        assert "fatal" in result["reason"].lower()
        assert strategy_ml._ML_PERMANENTLY_DISABLED is True

    def test_value_error_in_predict_disables_ml(self, monkeypatch):
        import strategy_ml
        mock_scaler = MagicMock()
        mock_scaler.transform.return_value = np.array([[0.5] * 12])
        mock_model = MagicMock()
        mock_model.predict_proba.side_effect = ValueError("Input contains NaN")
        monkeypatch.setattr(strategy_ml, "scaler", mock_scaler)
        monkeypatch.setattr(strategy_ml, "model", mock_model)
        monkeypatch.setattr(strategy_ml, "ML_ENABLED", True)

        result = strategy_ml.generate_signal(_make_ml_data())
        assert result["action"] == "hold"
        assert "fatal" in result["reason"].lower()
        assert strategy_ml._ML_PERMANENTLY_DISABLED is True

    def test_attribute_error_disables_ml(self, monkeypatch):
        import strategy_ml
        mock_scaler = MagicMock()
        mock_scaler.transform.side_effect = AttributeError(
            "'NoneType' object has no attribute 'transform'"
        )
        monkeypatch.setattr(strategy_ml, "scaler", mock_scaler)
        monkeypatch.setattr(strategy_ml, "model", MagicMock())
        monkeypatch.setattr(strategy_ml, "ML_ENABLED", True)

        result = strategy_ml.generate_signal(_make_ml_data())
        assert result["action"] == "hold"
        assert "corrupt" in result["reason"].lower() or "fatal" in result["reason"].lower()
        assert strategy_ml._ML_PERMANENTLY_DISABLED is True

    def test_disabled_state_persists_next_call(self, monkeypatch):
        """Setelah disable, panggilan berikutnya langsung short-circuit (tidak coba ulang)."""
        import strategy_ml
        mock_scaler = MagicMock()
        mock_scaler.transform.side_effect = ValueError("shape mismatch")
        monkeypatch.setattr(strategy_ml, "scaler", mock_scaler)
        monkeypatch.setattr(strategy_ml, "model", MagicMock())
        monkeypatch.setattr(strategy_ml, "ML_ENABLED", True)

        # Call 1: trigger disable
        strategy_ml.generate_signal(_make_ml_data())
        assert strategy_ml._ML_PERMANENTLY_DISABLED is True
        mock_scaler.transform.reset_mock()

        # Call 2: should NOT call scaler.transform anymore
        strategy_ml.generate_signal(_make_ml_data())
        assert mock_scaler.transform.call_count == 0


class TestRecoverableErrors:
    """Recoverable error → hold tapi ML tetap enabled (retry next tick)."""

    def test_unknown_exception_logs_but_does_not_disable(self, monkeypatch):
        """RuntimeError dari model = unknown — log tapi jangan disable (mungkin transient)."""
        import strategy_ml
        mock_scaler = MagicMock()
        mock_scaler.transform.return_value = np.array([[0.5] * 12])
        mock_model = MagicMock()
        mock_model.predict_proba.side_effect = RuntimeError("temporary glitch")
        monkeypatch.setattr(strategy_ml, "scaler", mock_scaler)
        monkeypatch.setattr(strategy_ml, "model", mock_model)
        monkeypatch.setattr(strategy_ml, "ML_ENABLED", True)

        result = strategy_ml.generate_signal(_make_ml_data())
        assert result["action"] == "hold"
        assert "transient" in result["reason"].lower() or "RuntimeError" in result["reason"]
        assert strategy_ml._ML_PERMANENTLY_DISABLED is False  # NOT disabled


# =============================================================================
# NORMAL FLOW — happy path harus tetap kerja
# =============================================================================
class TestNormalFlow:
    def test_buy_when_prob_naik_above_threshold(self, monkeypatch):
        _inject_working_ml(monkeypatch, prob_naik=0.75, prob_turun=0.25)
        import strategy_ml
        result = strategy_ml.generate_signal(_make_ml_data())
        assert result["action"] == "buy"
        assert "0.75" in result["reason"]
        assert strategy_ml.bot_state["in_position"] is True
        assert strategy_ml.bot_state["side"] == "long"

    def test_sell_when_prob_turun_above_threshold(self, monkeypatch):
        _inject_working_ml(monkeypatch, prob_naik=0.25, prob_turun=0.75)
        import strategy_ml
        result = strategy_ml.generate_signal(_make_ml_data())
        assert result["action"] == "sell"
        assert "0.75" in result["reason"]
        assert strategy_ml.bot_state["in_position"] is True
        assert strategy_ml.bot_state["side"] == "short"

    def test_hold_when_neutral(self, monkeypatch):
        _inject_working_ml(monkeypatch, prob_naik=0.50, prob_turun=0.50)
        import strategy_ml
        result = strategy_ml.generate_signal(_make_ml_data())
        assert result["action"] == "hold"
        assert strategy_ml.bot_state["in_position"] is False


# =============================================================================
# TRIPLE BARRIER — exit logic untuk posisi terbuka (LONG & SHORT)
# =============================================================================
# Saat bot_state["in_position"]=True, generate_signal() pertama cek 3 barrier:
#   1. Take Profit  (TP) — harga bergerak menguntungkan ke target
#   2. Stop Loss    (SL) — harga bergerak merugikan ke ambang
#   3. Time Stop    — durasi posisi melewati TIME_STOP_SEC
# Setelah salah satu kena, return "close" dan reset state.
class TestTripleBarrierLong:
    """LONG: profit kalau bid naik, loss kalau bid turun. Pakai BID untuk evaluasi."""

    def _open_long(self, strategy_ml, entry_price=1.0, entry_time=1000.0):
        strategy_ml.bot_state["in_position"] = True
        strategy_ml.bot_state["side"]        = "long"
        strategy_ml.bot_state["entry_price"] = entry_price
        strategy_ml.bot_state["entry_time"]  = entry_time

    def test_long_hits_tp_returns_close(self):
        import strategy_ml
        self._open_long(strategy_ml, entry_price=1.0)
        data = _make_ml_data()
        # Set bid SUPER tinggi → pasti lewat TP (1.004 = +0.4%)
        data["best_bid"]["price"] = 2.0
        data["best_ask"]["price"] = 2.001
        result = strategy_ml.generate_signal(data)
        assert result["action"] == "close"
        assert "LONG" in result["reason"]
        assert "TP" in result["reason"]
        # State direset
        assert strategy_ml.bot_state["in_position"] is False
        assert strategy_ml.bot_state["side"] == "none"

    def test_long_hits_sl_returns_close(self):
        import strategy_ml
        self._open_long(strategy_ml, entry_price=1.0)
        data = _make_ml_data()
        data["best_bid"]["price"] = 0.5
        data["best_ask"]["price"] = 0.501
        result = strategy_ml.generate_signal(data)
        assert result["action"] == "close"
        assert "SL" in result["reason"]
        assert strategy_ml.bot_state["in_position"] is False

    def test_long_within_barriers_returns_hold(self):
        import strategy_ml
        # entry_time mendekati candle open_time supaya time-stop tidak terpicu
        data = _make_ml_data()
        candle = data["current"]["15m"]
        self._open_long(strategy_ml, entry_price=1.0, entry_time=candle["open_time"] - 10)
        # Bid persis di entry → tidak hit TP/SL
        data["best_bid"]["price"] = 1.0
        data["best_ask"]["price"] = 1.001
        result = strategy_ml.generate_signal(data)
        assert result["action"] == "hold"
        # State posisi dipertahankan
        assert strategy_ml.bot_state["in_position"] is True


class TestTripleBarrierShort:
    """SHORT: profit kalau ask turun, loss kalau ask naik. Pakai ASK untuk evaluasi."""

    def _open_short(self, strategy_ml, entry_price=1.0, entry_time=1000.0):
        strategy_ml.bot_state["in_position"] = True
        strategy_ml.bot_state["side"]        = "short"
        strategy_ml.bot_state["entry_price"] = entry_price
        strategy_ml.bot_state["entry_time"]  = entry_time

    def test_short_hits_tp_returns_close(self):
        import strategy_ml
        self._open_short(strategy_ml, entry_price=1.0)
        data = _make_ml_data()
        # Ask drop di bawah TP_PRICE = 1 - 0.4% = 0.996
        data["best_bid"]["price"] = 0.5
        data["best_ask"]["price"] = 0.5
        result = strategy_ml.generate_signal(data)
        assert result["action"] == "close"
        assert "SHORT" in result["reason"]
        assert "TP" in result["reason"]

    def test_short_hits_sl_returns_close(self):
        import strategy_ml
        self._open_short(strategy_ml, entry_price=1.0)
        data = _make_ml_data()
        # Ask naik > SL_PRICE = 1 + 0.4% = 1.004
        data["best_bid"]["price"] = 2.0
        data["best_ask"]["price"] = 2.0
        result = strategy_ml.generate_signal(data)
        assert result["action"] == "close"
        assert "SL" in result["reason"]


class TestTripleBarrierTimeStop:
    def test_time_stop_triggers_close(self, monkeypatch):
        import strategy_ml
        strategy_ml.bot_state["in_position"] = True
        strategy_ml.bot_state["side"]        = "long"
        strategy_ml.bot_state["entry_price"] = 1.0
        # Entry time jauh lebih lama dari TIME_STOP_SEC
        strategy_ml.bot_state["entry_time"]  = 100.0

        data = _make_ml_data()
        # Bid di tengah barrier (tidak hit TP/SL)
        data["best_bid"]["price"] = 1.0
        data["best_ask"]["price"] = 1.001
        # Set candle open_time = waktu sekarang yang jauh > entry_time
        # _make_ml_data sudah pakai timestamp 2024-ish, jadi jauh > 100
        result = strategy_ml.generate_signal(data)
        assert result["action"] == "close"
        assert "Time Stop" in result["reason"]
        assert strategy_ml.bot_state["in_position"] is False


# =============================================================================
# FEATURE CACHE — recompute hanya kalau candle key berubah
# =============================================================================
class TestFeatureCache:
    def test_cache_miss_then_hit_reuses_baris(self, monkeypatch):
        """Tick kedua dengan candle yang sama harus baca dari cache."""
        _inject_working_ml(monkeypatch, prob_naik=0.5, prob_turun=0.5)
        import strategy_ml
        data = _make_ml_data()

        # Tick 1: cache kosong → recompute, isi cache
        strategy_ml.generate_signal(data)
        assert strategy_ml._feat_cache["key"] is not None
        first_baris = strategy_ml._feat_cache["baris_terbaru"]
        assert first_baris is not None

        # Tick 2: candle sama → cache HIT, baris_terbaru obj SAMA persis (identity)
        strategy_ml.generate_signal(data)
        assert strategy_ml._feat_cache["baris_terbaru"] is first_baris

    def test_cache_invalidates_when_candle_changes(self, monkeypatch):
        """Tick dengan candle baru → key beda → recompute, baris baru."""
        _inject_working_ml(monkeypatch, prob_naik=0.5, prob_turun=0.5)
        import strategy_ml
        data_1 = _make_ml_data()
        strategy_ml.generate_signal(data_1)
        old_baris = strategy_ml._feat_cache["baris_terbaru"]
        old_key = strategy_ml._feat_cache["key"]

        # Bikin data dengan candle BERUBAH (tambah 1 candle baru di buffer)
        from tests.conftest import make_candles
        new_candles = make_candles(n=70, tf="15m")
        data_2 = {
            "candles":  {"15m": new_candles[:-1]},
            "current":  {"15m": new_candles[-1]},
            "best_bid": {"price": new_candles[-1]["close"] * 0.999},
            "best_ask": {"price": new_candles[-1]["close"] * 1.001},
            "bid_ask_spread": 0.0,
            "funding_rate":   0.0,
            "latest_tick":    None,
            "is_warmup":      False,
        }
        strategy_ml.generate_signal(data_2)
        # Key & baris harus berubah
        assert strategy_ml._feat_cache["key"] != old_key
        assert strategy_ml._feat_cache["baris_terbaru"] is not old_baris


# =============================================================================
# FEATURE ENGINEERING — recoverable errors (hold, no disable)
# =============================================================================
class TestFeatureEngineeringErrors:
    def test_candle_missing_volume_column_returns_hold(self, monkeypatch):
        """Candle tanpa kolom 'volume' → KeyError di feature eng (volume_lag/vol_ma) → hold."""
        _inject_working_ml(monkeypatch, prob_naik=0.5, prob_turun=0.5)
        import strategy_ml

        from tests.conftest import make_candles
        candles = make_candles(n=60, tf="15m")
        # Buang kolom 'volume' — KeyError dilempar dari blok feature eng yang DI-DALAM try
        bad_candles = [{k: v for k, v in c.items() if k != "volume"} for c in candles]
        data = {
            "candles":  {"15m": bad_candles[:-1]},
            "current":  {"15m": bad_candles[-1]},
            "best_bid": {"price": bad_candles[-1]["close"]},
            "best_ask": {"price": bad_candles[-1]["close"]},
            "bid_ask_spread": 0.0,
            "funding_rate":   0.0,
            "latest_tick":    None,
            "is_warmup":      False,
        }
        result = strategy_ml.generate_signal(data)
        assert result["action"] == "hold"
        # ML tetap enabled (recoverable)
        assert strategy_ml._ML_PERMANENTLY_DISABLED is False

    def test_insufficient_candles_returns_hold(self):
        """< MIN_CANDLES → langsung hold sebelum feature eng."""
        import strategy_ml
        from tests.conftest import make_candles
        few = make_candles(n=10, tf="15m")
        data = {
            "candles":  {"15m": few[:-1]},
            "current":  {"15m": few[-1]},
            "best_bid": {"price": 1.0}, "best_ask": {"price": 1.0},
            "bid_ask_spread": 0.0, "funding_rate": 0.0,
            "latest_tick": None, "is_warmup": False,
        }
        result = strategy_ml.generate_signal(data)
        assert result["action"] == "hold"
        assert "buffer" in result["reason"].lower() or "candle" in result["reason"].lower()


# =============================================================================
# on_tick — wrapper yang dipanggil engine tiap tick
# =============================================================================
# Tugas on_tick: sync state dengan position_manager, panggil generate_signal,
# parse ML prob, lapor ke bot_monitor, dan teruskan ke execution.place_order
# kalau action != hold.
class TestOnTick:
    def _install_modules(self, monkeypatch):
        """Pasang execution, bot_monitor, position_manager stub di sys.modules."""
        import sys
        mock_exec    = MagicMock()
        mock_monitor = MagicMock()
        mock_pm      = MagicMock()
        mock_pm.get_position.return_value = {
            "side": "none", "entry_price": 0.0, "qty": 0.0, "open_time": None,
        }
        mock_pm.get_pnl_summary.return_value = {"equity": 1000.0, "side": "none"}
        monkeypatch.setitem(sys.modules, "execution", mock_exec)
        monkeypatch.setitem(sys.modules, "bot_monitor", mock_monitor)
        monkeypatch.setitem(sys.modules, "position_manager", mock_pm)
        return mock_exec, mock_monitor, mock_pm

    def test_buy_signal_forwarded_to_execution(self, monkeypatch):
        mock_exec, mock_monitor, _ = self._install_modules(monkeypatch)
        _inject_working_ml(monkeypatch, prob_naik=0.75, prob_turun=0.25)
        import strategy_ml
        strategy_ml.on_tick(_make_ml_data())
        # bot_monitor.record_tick dipanggil dengan signal
        mock_monitor.record_tick.assert_called_once()
        # execution.place_order dipanggil untuk action != hold
        mock_exec.place_order.assert_called_once()
        sig = mock_exec.place_order.call_args[0][0]
        assert sig["action"] == "buy"

    def test_hold_signal_does_not_call_execution(self, monkeypatch):
        mock_exec, mock_monitor, _ = self._install_modules(monkeypatch)
        _inject_working_ml(monkeypatch, prob_naik=0.5, prob_turun=0.5)
        import strategy_ml
        strategy_ml.on_tick(_make_ml_data())
        mock_monitor.record_tick.assert_called_once()
        mock_exec.place_order.assert_not_called()

    def test_position_sync_restores_state_from_position_manager(self, monkeypatch):
        """Saat bot restart, posisi long di Bybit harus di-sync ke bot_state."""
        mock_exec, mock_monitor, mock_pm = self._install_modules(monkeypatch)
        data = _make_ml_data()
        candle_time = data["current"]["15m"]["open_time"]
        # open_time mendekati candle saat ini supaya time-stop tidak terpicu
        mock_pm.get_position.return_value = {
            "side": "long", "entry_price": 1.5, "qty": 100.0,
            "open_time": candle_time - 10,
        }
        # Set bid/ask di entry → tidak hit TP/SL
        data["best_bid"]["price"] = 1.5
        data["best_ask"]["price"] = 1.5
        _inject_working_ml(monkeypatch, prob_naik=0.5, prob_turun=0.5)
        import strategy_ml
        # State awal: tidak in position
        strategy_ml.bot_state["in_position"] = False
        strategy_ml.bot_state["side"]        = "none"
        strategy_ml.bot_state["entry_price"] = 0.0
        strategy_ml.bot_state["entry_time"]  = 0.0

        strategy_ml.on_tick(data)

        # State harus disinkronisasi dari position_manager
        assert strategy_ml.bot_state["in_position"] is True
        assert strategy_ml.bot_state["side"] == "long"
        assert strategy_ml.bot_state["entry_price"] == 1.5
        assert strategy_ml.bot_state["entry_time"] == candle_time - 10

    def test_generate_signal_crash_records_error(self, monkeypatch):
        """Kalau generate_signal raise, on_tick catat ke bot_monitor.record_error."""
        mock_exec, mock_monitor, _ = self._install_modules(monkeypatch)
        import strategy_ml

        def _boom(*a, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(strategy_ml, "generate_signal", _boom)
        strategy_ml.on_tick(_make_ml_data())
        mock_monitor.record_error.assert_called_once()
        mock_exec.place_order.assert_not_called()
