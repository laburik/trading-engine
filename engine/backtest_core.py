# =============================================================================
# backtest_core.py — Mesin simulasi backtest (strategy harness + simulator PnL)
# =============================================================================
# Diekstrak dari hypertune.py supaya dipakai bersama oleh backtest.py CLI dan
# hypertune.py → SATU simulator, nol drift.
#
# Modul ini PEMILIK _MOCK_STATE dan memasang mock position_manager/execution ke
# sys.modules SAAT DI-IMPORT (sebelum strategy apa pun di-import). Maka consumer
# harus `import backtest_core` SEBELUM memanggil load_strategy().
# =============================================================================
from __future__ import annotations

import importlib
import sys
import warnings
from unittest.mock import MagicMock

# =============================================================================
# MOCK STATE + INSTALL (harus SEBELUM strategy di-import)
# =============================================================================
# Strategy biasanya `from position_manager import get_position` sehingga binding
# terjadi saat import. Kalau mock dipasang setelah strategy import, binding sudah
# ke modul asli (yang mungkin tidak ada balance live).
_MOCK_STATE: dict = {
    "side": "none",
    "entry_price": 0.0,
    "qty": 0.0,
    "entry_time": 0.0,
    "open_time": None,
}


def _mock_get_position() -> dict:
    return {
        "side":        _MOCK_STATE["side"],
        "entry_price": _MOCK_STATE["entry_price"],
        "qty":         _MOCK_STATE["qty"],
        "open_time":   _MOCK_STATE["open_time"],
    }


def _mock_get_pnl_summary() -> dict:
    return {
        "side":               _MOCK_STATE["side"],
        "entry_price":        _MOCK_STATE["entry_price"],
        "qty":                _MOCK_STATE["qty"],
        "balance":            0.0,
        "unrealized_pnl":     0.0,
        "equity":             0.0,
        "realized_pnl_total": 0.0,
        "total_fees":         0.0,
    }


# Install mock modules into sys.modules BEFORE strategy gets a chance to import.
_mock_execution = MagicMock()
sys.modules["execution"] = _mock_execution

_mock_pm = MagicMock()
_mock_pm.get_position    = _mock_get_position
_mock_pm.get_pnl_summary = _mock_get_pnl_summary
sys.modules["position_manager"] = _mock_pm

# Mock bot_monitor (strategy_ml.on_tick calls bot_monitor.record_tick).
_mock_bm = MagicMock()
sys.modules["bot_monitor"] = _mock_bm

# =============================================================================
# Sekarang aman import config
# =============================================================================
from config import FEE_RATE, INITIAL_BALANCE, ORDER_SIZE_USDT, LEVERAGE

try:
    from sklearn.exceptions import InconsistentVersionWarning
    warnings.filterwarnings("ignore", category=InconsistentVersionWarning)
except ImportError:
    pass


# =============================================================================
# STRATEGY LOADER + HARNESS
# =============================================================================
def load_strategy(strategy_file: str, timeframe: str | None = None):
    """
    Import strategy module dynamically. Mocks sudah dipasang di sys.modules.
    Kalau `timeframe` diberikan, set strategy.TF supaya match data yang dipass.
    """
    try:
        if strategy_file in sys.modules:
            mod = importlib.reload(sys.modules[strategy_file])
        else:
            mod = importlib.import_module(strategy_file)
    except ModuleNotFoundError:
        print(f"[ERROR] File '{strategy_file}.py' tidak ditemukan di folder ini.")
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] Gagal load strategy '{strategy_file}': {type(e).__name__}: {e}")
        sys.exit(1)

    if not hasattr(mod, "generate_signal"):
        print(f"[ERROR] Strategy '{strategy_file}.py' harus expose generate_signal(data).")
        sys.exit(1)

    if timeframe is not None and hasattr(mod, "TF"):
        mod.TF = timeframe

    return mod


def patch_params(strategy_module, names: list[str], values: tuple) -> None:
    """Set parameter ke strategy module."""
    for n, v in zip(names, values):
        setattr(strategy_module, n, v)


def reset_bot_state(strategy_module) -> None:
    """Reset internal bot_state dict kalau ada (utk strategy_ml-style)."""
    if hasattr(strategy_module, "bot_state"):
        bs = strategy_module.bot_state
        bs["in_position"] = False
        bs["side"] = "none"
        bs["entry_price"] = 0.0
        bs["entry_time"] = 0.0


def sync_strategy_state(strategy_module) -> None:
    """Mirror _MOCK_STATE ke strategy.bot_state (utk ML-style strategies)."""
    if hasattr(strategy_module, "bot_state"):
        bs = strategy_module.bot_state
        if _MOCK_STATE["side"] in ("long", "short"):
            bs["in_position"] = True
            bs["side"]        = _MOCK_STATE["side"]
            bs["entry_price"] = _MOCK_STATE["entry_price"]
            bs["entry_time"]  = _MOCK_STATE["entry_time"]
        else:
            bs["in_position"] = False
            bs["side"]        = "none"
            bs["entry_price"] = 0.0
            bs["entry_time"]  = 0.0


# =============================================================================
# DATA DICT BUILDER (untuk dipassing ke strategy.generate_signal)
# =============================================================================
def build_data_dict(closed: list[dict], current: dict, tf: str) -> dict:
    close = current["close"]
    return {
        "candles":            {tf: closed},
        "current":            {tf: current},
        "best_bid":           {"price": close * 0.9999, "qty": 1.0},
        "best_ask":           {"price": close * 1.0001, "qty": 1.0},
        "bid_ask_spread":     close * 0.0002,
        "orderbook_imbalance": 0.0,
        "volume_delta":       0.0,
        "funding_rate":       0.0001,
        "latest_tick":        {"price": close, "qty": 1.0, "side": "Buy",
                               "timestamp": current["open_time"]},
        "is_warmup":          False,
    }


# =============================================================================
# DEFENSIVE SIGNAL READER
# =============================================================================
# Backtest sengaja TIDAK menjalankan pre-flight penuh (itu gerbang khusus live).
# Tapi return aneh / typo action sebaiknya tetap ketahuan tanpa bikin friksi —
# jadi kita warning SEKALI saja (dedupe), lalu perlakukan sebagai "hold".
_VALID_ACTIONS = ("buy", "sell", "close", "hold")
_signal_warned: set[str] = set()


def _warn_once(msg: str) -> None:
    """Print warning sekali per pesan unik (backtest bisa ratusan ribu iterasi)."""
    if msg not in _signal_warned:
        _signal_warned.add(msg)
        print(f"[WARN] {msg}")


def _extract_action(signal: object) -> str:
    """
    Ambil 'action' dari return strategy secara defensif.
    - Bukan dict        → warning + 'hold'
    - Action tak dikenal → warning + 'hold'
    """
    if not isinstance(signal, dict):
        _warn_once(
            f"generate_signal() mengembalikan {type(signal).__name__}, bukan dict — "
            "diperlakukan sebagai 'hold'. Harusnya: {'action': 'hold', 'reason': '...'}."
        )
        return "hold"
    action = signal.get("action", "hold")
    if action not in _VALID_ACTIONS:
        _warn_once(
            f"generate_signal() mengembalikan action tak dikenal: {action!r} — "
            "diperlakukan sebagai 'hold'. Action valid: buy/sell/close/hold "
            "(cek kemungkinan typo)."
        )
        return "hold"
    return action


# =============================================================================
# SIMULATOR — SINGLE POSITION (strategy-driven exit, cermin bot live)
# =============================================================================
def simulate_single(strategy_module, candles: list[dict], tf: str) -> dict:
    """Strategy handle entry + exit. Bot live behavior."""
    global _MOCK_STATE
    _MOCK_STATE = {"side": "none", "entry_price": 0.0, "qty": 0.0,
                   "entry_time": 0.0, "open_time": None}
    reset_bot_state(strategy_module)

    # Pastikan strategy.TF match tf yang kita pass di data dict.
    if hasattr(strategy_module, "TF"):
        strategy_module.TF = tf

    min_warmup = int(getattr(strategy_module, "MIN_CANDLES", 50))
    notional = ORDER_SIZE_USDT * LEVERAGE

    trades: list[dict] = []
    equity_curve: list[float] = []
    balance = INITIAL_BALANCE

    for i in range(min_warmup, len(candles)):
        current = candles[i]
        closed  = candles[:i]
        close   = current["close"]

        sync_strategy_state(strategy_module)
        data = build_data_dict(closed, current, tf)

        try:
            signal = strategy_module.generate_signal(data)
        except Exception as e:
            signal = {"action": "hold", "reason": f"err: {e}"}

        action = _extract_action(signal)

        if action == "buy" and _MOCK_STATE["side"] == "none":
            _MOCK_STATE["side"]        = "long"
            _MOCK_STATE["entry_price"] = close
            _MOCK_STATE["entry_time"]  = current["open_time"]
            _MOCK_STATE["qty"]         = notional / close
            _MOCK_STATE["open_time"]   = current["open_time"]

        elif action == "sell" and _MOCK_STATE["side"] == "none":
            _MOCK_STATE["side"]        = "short"
            _MOCK_STATE["entry_price"] = close
            _MOCK_STATE["entry_time"]  = current["open_time"]
            _MOCK_STATE["qty"]         = notional / close
            _MOCK_STATE["open_time"]   = current["open_time"]

        elif action == "close" and _MOCK_STATE["side"] in ("long", "short"):
            ep  = _MOCK_STATE["entry_price"]
            qty = _MOCK_STATE["qty"]
            if _MOCK_STATE["side"] == "long":
                gross = (close - ep) * qty
            else:
                gross = (ep - close) * qty
            fee = notional * FEE_RATE * 2
            pnl = gross - fee
            balance += pnl
            trades.append({"side": _MOCK_STATE["side"], "entry": ep, "exit": close,
                           "pnl": pnl, "fee": fee, "reason": signal.get("reason", "")})
            _MOCK_STATE["side"]        = "none"
            _MOCK_STATE["entry_price"] = 0.0
            _MOCK_STATE["qty"]         = 0.0
            _MOCK_STATE["entry_time"]  = 0.0
            _MOCK_STATE["open_time"]   = None

        if _MOCK_STATE["side"] == "long":
            unreal = (close - _MOCK_STATE["entry_price"]) * _MOCK_STATE["qty"]
        elif _MOCK_STATE["side"] == "short":
            unreal = (_MOCK_STATE["entry_price"] - close) * _MOCK_STATE["qty"]
        else:
            unreal = 0.0
        equity_curve.append(balance + unreal)

    return {"trades": trades, "equity_curve": equity_curve, "final_balance": balance}


# =============================================================================
# SIMULATOR — MULTI POSITION (sim-driven exit via SL/TP/TimeStop)
# =============================================================================
def _get_risk_params(strategy_module) -> tuple[float, float, int]:
    sl = (getattr(strategy_module, "STOP_LOSS_PCT", None)
          or getattr(strategy_module, "TARGET_SL_PCT", None)
          or getattr(strategy_module, "SL_PCT", None))
    tp = (getattr(strategy_module, "TAKE_PROFIT_PCT", None)
          or getattr(strategy_module, "TARGET_TP_PCT", None)
          or getattr(strategy_module, "TP_PCT", None))
    ts = (getattr(strategy_module, "TIME_STOP_SEC", 0) or 0)
    if sl is None or tp is None:
        raise RuntimeError(
            "Multi-position mode butuh strategy file expose STOP_LOSS_PCT & "
            "TAKE_PROFIT_PCT (atau TARGET_SL_PCT/TARGET_TP_PCT)."
        )
    return float(sl), float(tp), int(ts)


def simulate_multi(strategy_module, candles: list[dict], tf: str) -> dict:
    """
    Tiap "buy"/"sell" → trade independen. Simulator handle SL/TP/TimeStop pakai
    konstanta yang dibaca dari strategy module. "close" diabaikan.
    """
    global _MOCK_STATE
    _MOCK_STATE = {"side": "none", "entry_price": 0.0, "qty": 0.0,
                   "entry_time": 0.0, "open_time": None}
    reset_bot_state(strategy_module)

    if hasattr(strategy_module, "TF"):
        strategy_module.TF = tf

    min_warmup = int(getattr(strategy_module, "MIN_CANDLES", 50))
    notional = ORDER_SIZE_USDT * LEVERAGE
    sl_pct, tp_pct, time_stop_sec = _get_risk_params(strategy_module)

    open_positions: list[dict] = []
    trades: list[dict] = []
    equity_curve: list[float] = []
    balance = INITIAL_BALANCE

    for i in range(min_warmup, len(candles)):
        current = candles[i]
        closed  = candles[:i]
        close   = current["close"]
        high    = current["high"]
        low     = current["low"]
        t       = current["open_time"]

        # --- Exit posisi terbuka (intra-bar) ---
        still_open: list[dict] = []
        for pos in open_positions:
            ep = pos["entry_price"]
            qty = pos["qty"]
            side = pos["side"]
            entry_time = pos["entry_time"]

            if side == "long":
                tp_price = ep * (1 + tp_pct)
                sl_price = ep * (1 - sl_pct)
                exit_price = None
                reason = ""
                if low <= sl_price:
                    exit_price = sl_price; reason = "SL"
                elif high >= tp_price:
                    exit_price = tp_price; reason = "TP"
                elif time_stop_sec > 0 and (t - entry_time) >= time_stop_sec:
                    exit_price = close; reason = "TimeStop"

                if exit_price is not None:
                    gross = (exit_price - ep) * qty
                    fee = notional * FEE_RATE * 2
                    pnl = gross - fee
                    balance += pnl
                    trades.append({"side": "long", "entry": ep, "exit": exit_price,
                                   "pnl": pnl, "fee": fee, "reason": reason})
                else:
                    still_open.append(pos)
            else:  # short
                tp_price = ep * (1 - tp_pct)
                sl_price = ep * (1 + sl_pct)
                exit_price = None
                reason = ""
                if high >= sl_price:
                    exit_price = sl_price; reason = "SL"
                elif low <= tp_price:
                    exit_price = tp_price; reason = "TP"
                elif time_stop_sec > 0 and (t - entry_time) >= time_stop_sec:
                    exit_price = close; reason = "TimeStop"

                if exit_price is not None:
                    gross = (ep - exit_price) * qty
                    fee = notional * FEE_RATE * 2
                    pnl = gross - fee
                    balance += pnl
                    trades.append({"side": "short", "entry": ep, "exit": exit_price,
                                   "pnl": pnl, "fee": fee, "reason": reason})
                else:
                    still_open.append(pos)

        open_positions = still_open

        # --- Force "no position" supaya strategy keep generating entry signals ---
        _MOCK_STATE["side"]        = "none"
        _MOCK_STATE["entry_price"] = 0.0
        _MOCK_STATE["qty"]         = 0.0
        _MOCK_STATE["entry_time"]  = 0.0
        sync_strategy_state(strategy_module)

        data = build_data_dict(closed, current, tf)
        try:
            signal = strategy_module.generate_signal(data)
        except Exception as e:
            signal = {"action": "hold", "reason": f"err: {e}"}

        action = _extract_action(signal)
        if action == "buy":
            open_positions.append({"side": "long", "entry_price": close,
                                   "entry_time": t, "qty": notional / close})
        elif action == "sell":
            open_positions.append({"side": "short", "entry_price": close,
                                   "entry_time": t, "qty": notional / close})
        # "close" diabaikan di multi mode

        unreal = 0.0
        for pos in open_positions:
            if pos["side"] == "long":
                unreal += (close - pos["entry_price"]) * pos["qty"]
            else:
                unreal += (pos["entry_price"] - close) * pos["qty"]
        equity_curve.append(balance + unreal)

    return {"trades": trades, "equity_curve": equity_curve, "final_balance": balance}
