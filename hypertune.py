# =============================================================================
# hypertune.py — Config-Driven Hyperparameter Tuning
# =============================================================================
#
# Cara pakai:
#   1. Edit tuning_config.py (STRATEGY_FILE, TIMEFRAME, DAYS_BACK, OPTIM_MODE,
#      POSITION_MODEL, PARAMS).
#   2. Pastikan parameter di strategy file (mis. RSI_PERIOD = 14) ada di top.
#   3. python hypertune.py
#
# Tidak ada CLI prompt — semua diatur via tuning_config.py.
# =============================================================================
from __future__ import annotations

import os
import sys

# Tambahkan folder engine/ ke sys.path agar modul infrastruktur bisa di-import flat.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine"))

import importlib
import itertools
import math
import time
import warnings
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import ccxt  # sync CCXT untuk one-shot download historis (bukan ccxt.pro)
import numpy as np
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# =============================================================================
# IMPORTANT: install mock untuk position_manager & execution SEBELUM import
# strategy apa pun. Strategy biasanya `from position_manager import get_position`
# sehingga binding terjadi saat import. Kalau mock dipasang setelah strategy
# import, binding sudah ke modul asli (yang mungkin tidak ada balance live).
# =============================================================================
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
# Sekarang aman import config + tuning_config
# =============================================================================
from config import SYMBOL, FEE_RATE, INITIAL_BALANCE, ORDER_SIZE_USDT, LEVERAGE, EXCHANGE
import tuning_config as tc

try:
    from sklearn.exceptions import InconsistentVersionWarning
    warnings.filterwarnings("ignore", category=InconsistentVersionWarning)
except ImportError:
    pass


# =============================================================================
# CONSTANTS
# =============================================================================
DATA_DIR = "data"

# CCXT menerima timeframe string langsung ("1m", "15m", "2h", dst).
# Map di bawah hanya dipakai untuk validasi + perhitungan cache age.
TF_SECONDS_MAP = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200, "1d": 86400,
}

TOP_N_VALIDATE = 10
TOP_N_DISPLAY  = 3


# =============================================================================
# CONFIG VALIDATION
# =============================================================================
def validate_config() -> None:
    required = ["STRATEGY_FILE", "TIMEFRAME", "DAYS_BACK", "OPTIM_MODE", "POSITION_MODEL", "PARAMS"]
    missing = [a for a in required if not hasattr(tc, a)]
    if missing:
        print(f"[ERROR] tuning_config.py kurang field: {missing}")
        sys.exit(1)
    if tc.TIMEFRAME not in TF_SECONDS_MAP:
        print(f"[ERROR] TIMEFRAME '{tc.TIMEFRAME}' invalid. Valid: {list(TF_SECONDS_MAP.keys())}")
        sys.exit(1)
    if tc.OPTIM_MODE not in ("profit", "drawdown", "balanced"):
        print(f"[ERROR] OPTIM_MODE harus profit|drawdown|balanced, dapat: '{tc.OPTIM_MODE}'")
        sys.exit(1)
    if tc.POSITION_MODEL not in ("single", "multi"):
        print(f"[ERROR] POSITION_MODEL harus single|multi, dapat: '{tc.POSITION_MODEL}'")
        sys.exit(1)
    if not isinstance(tc.PARAMS, dict) or not tc.PARAMS:
        print(f"[ERROR] PARAMS harus dict non-empty.")
        sys.exit(1)
    if not isinstance(tc.DAYS_BACK, int) or tc.DAYS_BACK < 1:
        print(f"[ERROR] DAYS_BACK harus integer >= 1.")
        sys.exit(1)
    if not isinstance(tc.STRATEGY_FILE, str) or not tc.STRATEGY_FILE:
        print(f"[ERROR] STRATEGY_FILE harus string non-empty.")
        sys.exit(1)


# =============================================================================
# PARAM EXPANSION
# =============================================================================
def expand_param(name: str, spec: Any) -> list[Any]:
    """
    [start, end, N] dengan semua numeric (bukan bool) + N int >=1 → linspace.
    Selain itu (panjang lain, atau ada bool/string) → discrete list apa adanya.
    """
    if not isinstance(spec, list) or len(spec) == 0:
        raise ValueError(f"PARAMS['{name}']: harus list non-empty")

    is_range = (
        len(spec) == 3
        and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in spec[:2])
        and isinstance(spec[2], int)
        and not isinstance(spec[2], bool)
        and spec[2] >= 1
    )

    if is_range:
        start, end, n = spec
        if n == 1:
            return [start]
        is_int_pair = isinstance(start, int) and isinstance(end, int)
        values = np.linspace(start, end, n).tolist()
        if is_int_pair:
            values = [int(round(v)) for v in values]
            # Dedup setelah rounding (bisa terjadi kalau range kecil)
            seen: set = set()
            uniq: list = []
            for v in values:
                if v not in seen:
                    seen.add(v)
                    uniq.append(v)
            return uniq
        # Float: round ke 10 desimal untuk hilangkan artifact linspace (0.099999...)
        return [round(float(v), 10) for v in values]

    return list(spec)


def expand_grid(params: dict) -> tuple[list[str], list[tuple]]:
    """Return (names, list of combo tuples)."""
    names = list(params.keys())
    expanded = [expand_param(k, params[k]) for k in names]
    combos = list(itertools.product(*expanded))
    return names, combos


# =============================================================================
# STRATEGY LOADER
# =============================================================================
def load_strategy(strategy_file: str):
    """
    Import strategy module dynamically. Mocks sudah dipasang di sys.modules.
    Auto-override strategy.TF = config.TIMEFRAME kalau attribute ada.
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

    if hasattr(mod, "TF"):
        mod.TF = tc.TIMEFRAME

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
# DATA DOWNLOAD (CCXT — multi-exchange)
# =============================================================================
def _ts_str(ts_sec: float) -> str:
    return datetime.fromtimestamp(ts_sec, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _build_ccxt_client():
    """Buat instance CCXT sync untuk one-shot REST fetch (bukan WebSocket)."""
    try:
        cls = getattr(ccxt, EXCHANGE)
    except AttributeError as e:
        print(f"[ERROR] EXCHANGE '{EXCHANGE}' tidak ada di ccxt. "
              f"Lihat https://docs.ccxt.com/#/?id=exchanges")
        raise SystemExit(1) from e
    ex = cls({"enableRateLimit": True, "options": {"defaultType": "linear"}})
    try:
        ex.load_markets()
    except Exception as e:
        print(f"[ERROR] {EXCHANGE}.load_markets() gagal: {e}")
        raise SystemExit(1) from e
    return ex


def _resolve_symbol(ex, raw_symbol: str) -> str:
    """Resolve raw symbol (mis. 'XRPUSDT') ke CCXT unified ('XRP/USDT:USDT')."""
    market = ex.markets_by_id.get(raw_symbol)
    if isinstance(market, list):
        market = market[0]
    if market:
        return market["symbol"]
    # Fallback: anggap user sudah menulis format unified
    return raw_symbol


def download_candles(symbol: str, tf: str, days_back: int) -> list[dict]:
    """Download OHLC via CCXT fetch_ohlcv dengan pagination."""
    interval_sec = TF_SECONDS_MAP[tf]
    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - days_back * 86400 * 1000

    ex = _build_ccxt_client()
    ccxt_symbol = _resolve_symbol(ex, symbol)

    all_rows: list[list] = []
    since: int = start_ms

    while since < end_ms:
        try:
            batch: list[list] = ex.fetch_ohlcv(ccxt_symbol, timeframe=tf, since=since, limit=1000)
        except Exception as e:
            print(f"[ERROR] {EXCHANGE}.fetch_ohlcv gagal: {e}")
            break

        if not batch:
            break

        all_rows.extend(batch)
        last_ts: int = int(batch[-1][0])
        next_since: int = last_ts + interval_sec * 1000
        if next_since <= since:
            break  # cegah infinite loop kalau exchange return stale data
        since = next_since
        if len(batch) < 1000:
            break  # batch terakhir, tidak ada data lagi

    # Dedup berdasarkan timestamp (kadang batch overlap)
    seen: set = set()
    uniq: list[list] = []
    for r in sorted(all_rows, key=lambda r: int(r[0])):
        ts: int = int(r[0])
        if ts in seen:
            continue
        seen.add(ts)
        uniq.append(r)

    # Strip candle yang sedang berjalan (belum tutup)
    if uniq:
        newest_start_ms: int = int(uniq[-1][0])
        if newest_start_ms + interval_sec * 1000 > int(time.time() * 1000):
            uniq = uniq[:-1]

    return [{
        "timeframe":   tf,
        "open_time":   int(r[0]) / 1000.0,
        "open":        float(r[1]),
        "high":        float(r[2]),
        "low":         float(r[3]),
        "close":       float(r[4]),
        "volume":      float(r[5]),
        "buy_volume":  0.0,
        "sell_volume": 0.0,
        "tick_count":  0,
    } for r in uniq]


def save_candles_csv(candles: list[dict], symbol: str, tf: str) -> str:
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
    start_d = datetime.fromtimestamp(candles[0]["open_time"], tz=timezone.utc).strftime("%Y-%m-%d")
    end_d   = datetime.fromtimestamp(candles[-1]["open_time"], tz=timezone.utc).strftime("%Y-%m-%d")
    # Prefix EXCHANGE supaya cache tidak tabrakan kalau user ganti exchange tapi
    # SYMBOL kebetulan sama (mis. "XRPUSDT" exist di bybit & binance).
    fname = f"{EXCHANGE}_{symbol}_{tf}_{start_d}_{end_d}.csv"
    fpath = os.path.join(DATA_DIR, fname)
    pd.DataFrame(candles).to_csv(fpath, index=False)
    return fpath


def _try_load_cache(symbol: str, tf: str, days_back: int,
                    max_age_minutes: float) -> tuple[list[dict] | None, str]:
    """
    Coba load candle dari CSV cache di folder data/ kalau:
      1. Ada file matching <symbol>_<tf>_*.csv
      2. File age < max_age_minutes
      3. Jumlah candle cukup masuk akal untuk days_back (minimal 95% dari expected)

    Return (candles | None, reason).
      candles=None artinya cache miss — caller harus download.
      candles=list artinya cache hit — pakai data ini.
    """
    if max_age_minutes <= 0:
        return None, "cache disabled (MAX_CACHE_AGE_MINUTES=0)"
    if not os.path.exists(DATA_DIR):
        return None, "data/ folder belum ada"

    # Cari CSV yang match pattern <exchange>_<symbol>_<tf>_*.csv
    prefix = f"{EXCHANGE}_{symbol}_{tf}_"
    candidates: list[tuple[float, str]] = []  # (mtime, full_path)
    try:
        for fname in os.listdir(DATA_DIR):
            if fname.startswith(prefix) and fname.endswith(".csv"):
                fpath = os.path.join(DATA_DIR, fname)
                if os.path.isfile(fpath):
                    candidates.append((os.path.getmtime(fpath), fpath))
    except OSError as e:
        return None, f"read data/ gagal: {e}"

    if not candidates:
        return None, f"tidak ada CSV match {prefix}*.csv"

    # Pakai yang terbaru (mtime tertinggi)
    candidates.sort(reverse=True)
    latest_mtime, latest_path = candidates[0]
    age_min: float = (time.time() - latest_mtime) / 60.0

    if age_min > max_age_minutes:
        return None, (
            f"cache stale: {os.path.basename(latest_path)} berusia "
            f"{age_min:.1f} menit (limit {max_age_minutes})"
        )

    # Validasi jumlah candle masuk akal
    try:
        df = pd.read_csv(latest_path)
    except Exception as e:
        return None, f"read CSV gagal: {e}"

    expected_total: float = (days_back * 86400.0) / TF_SECONDS_MAP[tf]
    min_acceptable: float = expected_total * 0.95   # toleransi 5%

    if len(df) < min_acceptable:
        return None, (
            f"cache partial: {len(df)} candle, butuh minimal "
            f"{int(min_acceptable)} (expected ~{int(expected_total)})"
        )

    # Convert df → list of dict (sama format dgn download_candles output)
    candles: list[dict] = df.to_dict("records")
    return candles, (
        f"hit: {os.path.basename(latest_path)} ({len(candles)} candles, "
        f"age {age_min:.1f}m)"
    )


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
# SIMULATOR — SINGLE POSITION (strategy-driven exit)
# =============================================================================
def simulate_single(strategy_module, candles: list[dict], tf: str) -> dict:
    """Strategy handle entry + exit. Bot live behavior."""
    global _MOCK_STATE
    _MOCK_STATE = {"side": "none", "entry_price": 0.0, "qty": 0.0,
                   "entry_time": 0.0, "open_time": None}
    reset_bot_state(strategy_module)

    # Pastikan strategy.TF match tf yang kita pass di data dict.
    # Tanpa ini, strategy.py membaca data["candles"][strategy.TF] yang berbeda
    # dari key yang kita masukkan, jadi candles selalu empty → hold semua.
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

        action = signal.get("action", "hold")

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
                           "pnl": pnl, "reason": signal.get("reason", "")})
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
# SIMULATOR — MULTI POSITION (sim-driven exit)
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
    Tiap "buy"/"sell" → trade independen. Simulator handle SL/TP/TimeStop
    pakai konstanta yang dibaca dari strategy module. "close" diabaikan.
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
                                   "pnl": pnl, "reason": reason})
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
                                   "pnl": pnl, "reason": reason})
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

        action = signal.get("action", "hold")
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


# =============================================================================
# METRICS
# =============================================================================
def compute_metrics(result: dict, initial_balance: float) -> dict:
    trades: list[dict] = result["trades"]
    equity_curve: list[float] = result["equity_curve"]
    final_balance: float = result["final_balance"]

    total_pnl = final_balance - initial_balance
    total_pnl_pct = (total_pnl / initial_balance) * 100 if initial_balance > 0 else 0.0

    num_trades = len(trades)
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    win_rate = (len(wins) / num_trades * 100) if num_trades > 0 else 0.0

    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss   = abs(sum(t["pnl"] for t in losses))
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = float("inf") if gross_profit > 0 else 0.0

    if equity_curve:
        eq = np.array(equity_curve, dtype=float)
        running_max = np.maximum.accumulate(eq)
        dd_pct = np.where(running_max > 0, (running_max - eq) / running_max, 0.0)
        max_dd_pct = float(dd_pct.max() * 100)
    else:
        max_dd_pct = 0.0

    max_consec_wins = 0
    max_consec_losses = 0
    cur_wins = 0
    cur_losses = 0
    for tr in trades:
        if tr["pnl"] > 0:
            cur_wins += 1
            cur_losses = 0
            if cur_wins > max_consec_wins:
                max_consec_wins = cur_wins
        else:
            cur_losses += 1
            cur_wins = 0
            if cur_losses > max_consec_losses:
                max_consec_losses = cur_losses

    if num_trades > 1:
        returns = np.array([t["pnl"] / initial_balance for t in trades])
        std = returns.std(ddof=1)
        sharpe = float(returns.mean() / std * math.sqrt(num_trades)) if std > 0 else 0.0
    else:
        sharpe = 0.0

    return {
        "pnl":               total_pnl,
        "pnl_pct":           total_pnl_pct,
        "num_trades":        num_trades,
        "win_rate":          win_rate,
        "profit_factor":     profit_factor,
        "max_dd_pct":        max_dd_pct,
        "max_consec_wins":   max_consec_wins,
        "max_consec_losses": max_consec_losses,
        "sharpe":            sharpe,
    }


def score_metrics(metrics: dict, mode: str) -> float:
    if metrics["num_trades"] < 3:
        return float("-inf")
    if mode == "profit":
        return metrics["pnl"]
    if mode == "drawdown":
        return -metrics["max_dd_pct"]
    if mode == "balanced":
        return metrics["sharpe"]
    return 0.0


# =============================================================================
# GRID SEARCH ORCHESTRATOR
# =============================================================================
def _progress_bar(current: int, total: int, width: int = 30) -> str:
    pct = current / total if total > 0 else 1.0
    filled = int(width * pct)
    bar = "=" * filled + "-" * (width - filled)
    return f"[{bar}] {current}/{total} ({pct*100:.1f}%)"


def run_grid_search(strategy_module, candles_train: list[dict], candles_test: list[dict],
                    names: list[str], combos: list[tuple]) -> list[dict]:
    sim_fn = simulate_single if tc.POSITION_MODEL == "single" else simulate_multi
    total = len(combos)
    print(f"\n[INFO] Grid Search: {total} kombinasi parameter")
    print(f"[INFO] In-sample: {len(candles_train)} candles | Out-of-sample: {len(candles_test)} candles")

    results: list[dict] = []
    start = time.time()
    last_print = 0.0
    first_err_shown = False

    for idx, combo in enumerate(combos, start=1):
        patch_params(strategy_module, names, combo)
        try:
            res_in = sim_fn(strategy_module, candles_train, tc.TIMEFRAME)
            met_in = compute_metrics(res_in, INITIAL_BALANCE)
            score_in = score_metrics(met_in, tc.OPTIM_MODE)
        except Exception as e:
            if not first_err_shown:
                print(f"\n[WARN] Combo gagal: {dict(zip(names, combo))} → {type(e).__name__}: {e}")
                first_err_shown = True
            continue

        results.append({
            "params":   dict(zip(names, combo)),
            "in":       met_in,
            "score_in": score_in,
        })

        now = time.time()
        if now - last_print > 0.3 or idx == total:
            elapsed = now - start
            eta = (elapsed / idx) * (total - idx) if idx > 0 else 0.0
            sys.stdout.write(f"\r{_progress_bar(idx, total)}  ETA {eta:.0f}s ")
            sys.stdout.flush()
            last_print = now
    print()

    results.sort(key=lambda r: r["score_in"], reverse=True)
    top_candidates = [r for r in results if r["score_in"] != float("-inf")][:TOP_N_VALIDATE]

    print(f"\n[INFO] Validating top {len(top_candidates)} candidates on out-of-sample...")
    for r in top_candidates:
        patch_params(strategy_module, names, tuple(r["params"][n] for n in names))
        try:
            res_out = sim_fn(strategy_module, candles_test, tc.TIMEFRAME)
            r["out"] = compute_metrics(res_out, INITIAL_BALANCE)
            r["score_out"] = score_metrics(r["out"], tc.OPTIM_MODE)
        except Exception:
            r["out"] = compute_metrics({"trades": [], "equity_curve": [INITIAL_BALANCE],
                                        "final_balance": INITIAL_BALANCE}, INITIAL_BALANCE)
            r["score_out"] = float("-inf")

    top_candidates.sort(key=lambda r: r["score_out"], reverse=True)
    return top_candidates[:TOP_N_DISPLAY]


# =============================================================================
# OUTPUT
# =============================================================================
def _fmt_pf(pf: float) -> str:
    return "inf" if math.isinf(pf) else f"{pf:.2f}"


def _fmt_value(v: Any) -> str:
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        return f"{v:.6f}".rstrip("0").rstrip(".")
    return str(v)


def _fmt_params(params: dict) -> str:
    return ", ".join(f"{k}={_fmt_value(v)}" for k, v in params.items())


def print_metric_block(label: str, m: dict) -> None:
    sign = "+" if m["pnl"] >= 0 else ""
    print(f"  {label}:")
    print(f"    PnL: {sign}{m['pnl']:.2f} USDT ({sign}{m['pnl_pct']:.2f}%)   "
          f"Trades: {m['num_trades']}   Win: {m['win_rate']:.1f}%")
    print(f"    Max DD: {m['max_dd_pct']:.2f}%   Profit Factor: {_fmt_pf(m['profit_factor'])}   "
          f"Sharpe: {m['sharpe']:.3f}")
    print(f"    Max Consec Wins: {m['max_consec_wins']}   Max Consec Losses: {m['max_consec_losses']}")


def print_top_results(top: list[dict]) -> None:
    print()
    print("=" * 70)
    print(f"  TOP {len(top)} PARAMETERS — strategy: {tc.STRATEGY_FILE}.py | optim: {tc.OPTIM_MODE.upper()}")
    print("  (Sorted by out-of-sample score)")
    print("=" * 70)

    if not top:
        print("\n[WARN] Tidak ada kombinasi yang valid (min 3 trade per backtest).")
        print("       Coba perluas range parameter di tuning_config.py atau tambah jumlah candle.")
        return

    score_label = {"profit": "PnL (USDT)", "drawdown": "-MaxDD (%)",
                   "balanced": "Sharpe Ratio"}.get(tc.OPTIM_MODE, "Score")

    for rank, r in enumerate(top, start=1):
        print()
        print(f"#{rank}  Out-of-Sample Score: {r['score_out']:.4f}   "
              f"In-Sample Score: {r['score_in']:.4f}   ({score_label})")
        print(f"  Params: {_fmt_params(r['params'])}")
        print_metric_block("In-Sample (80%)", r["in"])
        print_metric_block("Out-of-Sample (20%)", r["out"])

    print()
    print("=" * 70)
    print("  Selesai. Copy parameter di atas ke strategy file untuk dipakai live.")
    print("=" * 70)


# =============================================================================
# MAIN
# =============================================================================
def main() -> None:
    validate_config()

    print("=" * 70)
    print("  HYPERTUNING — Config-Driven")
    print("=" * 70)
    print(f"  Strategy file : {tc.STRATEGY_FILE}.py")
    print(f"  Symbol        : {SYMBOL}  (dari config.py)")
    print(f"  Timeframe     : {tc.TIMEFRAME}")
    print(f"  Days back     : {tc.DAYS_BACK}")
    print(f"  Optim mode    : {tc.OPTIM_MODE}")
    print(f"  Position      : {tc.POSITION_MODEL}")
    print(f"  Initial Bal   : {INITIAL_BALANCE:.2f} USDT")
    print(f"  Order Size    : {ORDER_SIZE_USDT} USDT × leverage {LEVERAGE} = {ORDER_SIZE_USDT*LEVERAGE} notional")
    print(f"  Fee Rate      : {FEE_RATE*100:.3f}% per side")
    print("=" * 70)

    # --- Expand grid ---
    try:
        names, combos = expand_grid(tc.PARAMS)
    except Exception as e:
        print(f"[ERROR] Gagal parse PARAMS: {e}")
        sys.exit(1)

    print(f"\n[INFO] Param dituning: {names}")
    print(f"[INFO] Total kombinasi: {len(combos)}")
    if len(combos) == 0:
        print(f"[ERROR] Grid kosong.")
        sys.exit(1)

    # Tampilkan expanded values untuk transparansi
    for n in names:
        vals = expand_param(n, tc.PARAMS[n])
        preview = vals if len(vals) <= 6 else (vals[:3] + ["..."] + vals[-2:])
        print(f"       {n}: {preview}  (n={len(vals)})")

    # --- Load strategy ---
    strategy_module = load_strategy(tc.STRATEGY_FILE)
    print(f"\n[OK]   Strategy '{tc.STRATEGY_FILE}.py' loaded.")

    # --- Cek cache dulu ---
    max_cache_age = float(getattr(tc, "MAX_CACHE_AGE_MINUTES", 60))
    cached_candles, cache_reason = _try_load_cache(SYMBOL, tc.TIMEFRAME, tc.DAYS_BACK, max_cache_age)
    if cached_candles is not None:
        candles: list[dict] = cached_candles
        print(f"\n[CACHE] {cache_reason}")
        print(f"        Skip download — pakai data cache untuk hemat waktu.")
        print(f"        Range: {_ts_str(candles[0]['open_time'])} → {_ts_str(candles[-1]['open_time'])}")
    else:
        # --- Cache miss → download ---
        print(f"\n[CACHE] miss: {cache_reason}")
        print(f"[INFO] Downloading {SYMBOL} {tc.TIMEFRAME} candles untuk {tc.DAYS_BACK} hari...")
        t0 = time.time()
        candles = download_candles(SYMBOL, tc.TIMEFRAME, tc.DAYS_BACK)
        if not candles:
            print("[ERROR] Gagal download data atau data kosong.")
            sys.exit(1)
        print(f"[OK]   {len(candles)} candles ({time.time()-t0:.1f}s)")
        print(f"       Range: {_ts_str(candles[0]['open_time'])} → {_ts_str(candles[-1]['open_time'])}")

        csv_path = save_candles_csv(candles, SYMBOL, tc.TIMEFRAME)
        print(f"[OK]   CSV: {csv_path}")

    # --- Split 80/20 ---
    split_idx = int(len(candles) * 0.8)
    candles_train = candles[:split_idx]
    candles_test  = candles[split_idx:]
    if len(candles_train) < 50 or len(candles_test) < 20:
        print(f"[ERROR] Data terlalu sedikit. Train={len(candles_train)}, Test={len(candles_test)}.")
        print(f"        Pilih timeframe lebih kecil atau perpanjang DAYS_BACK di tuning_config.py.")
        sys.exit(1)

    # --- Grid search ---
    t0 = time.time()
    top = run_grid_search(strategy_module, candles_train, candles_test, names, combos)
    print(f"\n[INFO] Total waktu hypertuning: {time.time()-t0:.1f}s")

    print_top_results(top)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INFO] Dibatalkan user.")
        sys.exit(0)
