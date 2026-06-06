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
import config as _cfg

# =============================================================================
# REALISME FILL — biaya yang sering luput di backtest naif (bikin hasil optimis)
# =============================================================================
# Dibaca dari config (fallback aman kalau belum di-set). Dipakai SEMUA simulator.
SLIPPAGE: float             = getattr(_cfg, "BACKTEST_SLIPPAGE", 0.0002)     # per fill
HALF_SPREAD: float          = getattr(_cfg, "BACKTEST_HALF_SPREAD", 0.0001)  # tiap sisi
FUNDING_RATE: float         = getattr(_cfg, "BACKTEST_FUNDING_RATE", 0.0001) # per 8 jam
FUNDING_INTERVAL_SEC: float = 8 * 3600


def _fill_price(ref: float, direction: str) -> float:
    """
    Harga fill realistis (spread + slippage):
      - 'buy'  → bayar di ATAS acuan (ambil ask + slippage)
      - 'sell' → terima di BAWAH acuan (kena bid − slippage)
    Dipanggil untuk SEMUA fill (entry, exit, SL/TP) supaya tiap transaksi bayar
    biaya yang nyata dibayar di live. SLIPPAGE/HALF_SPREAD bisa di-nol-kan di test
    untuk mengisolasi logika harga.
    """
    cost = HALF_SPREAD + SLIPPAGE
    return ref * (1.0 + cost) if direction == "buy" else ref * (1.0 - cost)


def _funding_cost(notional: float, holding_sec: float) -> float:
    """Funding perp ~ proporsional lama posisi ditahan (per FUNDING_INTERVAL_SEC)."""
    if holding_sec <= 0:
        return 0.0
    return notional * FUNDING_RATE * (holding_sec / FUNDING_INTERVAL_SEC)


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
    # Harga acuan = close bar TERAKHIR YANG TERTUTUP — bukan current (yang belum
    # tertutup saat keputusan diambil). Cegah look-ahead: strategi tidak boleh
    # "melihat" close bar yang justru jadi tempat dia fill. Fallback ke open
    # current kalau belum ada history (mis. MIN_CANDLES=0 di awal).
    ref = closed[-1]["close"] if closed else current["open"]
    return {
        "candles":            {tf: closed},
        "current":            {tf: current},
        "best_bid":           {"price": ref * 0.9999, "qty": 1.0},
        "best_ask":           {"price": ref * 1.0001, "qty": 1.0},
        "bid_ask_spread":     ref * 0.0002,
        "orderbook_imbalance": 0.0,
        "volume_delta":       0.0,
        "funding_rate":       0.0001,
        "latest_tick":        {"price": ref, "qty": 1.0, "side": "Buy",
                               "timestamp": current["open_time"]},
        "is_warmup":          False,
    }


# =============================================================================
# REALISME 1-MENIT (BACKTEST_REALISM) — eksekusi intrabar via sub-bar
# =============================================================================
# Di level candle besar, kalau range candle menyentuh SL DAN TP, mesin tak tahu
# mana duluan → asumsi pesimis SL. Dengan sub-bar 1m, urutan NYATA terbaca.
# Sinyal strategi TETAP di TF tinggi; sub-bar hanya dipakai untuk EKSEKUSI.
def _group_subbars(htf_candles: list[dict], detail_candles: list[dict],
                   tf_seconds: float) -> list[list[dict]]:
    """
    Bucket candle detail (1m) ke jendela tiap candle htf: [open_time, open_time+tf).
    Return list selaras dengan htf_candles (tiap elemen = sub-bar candle itu, urut).
    Two-pointer O(n+m); candle yang tak punya data 1m → bucket kosong.
    """
    buckets: list[list[dict]] = [[] for _ in htf_candles]
    if not detail_candles or not htf_candles:
        return buckets
    detail = sorted(detail_candles, key=lambda c: c["open_time"])
    n = len(detail)
    j = 0
    for idx, htf in enumerate(htf_candles):
        start = htf["open_time"]
        end = start + tf_seconds
        while j < n and detail[j]["open_time"] < start:
            j += 1
        k = j
        while k < n and detail[k]["open_time"] < end:
            buckets[idx].append(detail[k])
            k += 1
        j = k
    return buckets


def _build_forming(open_time: float, tf: str, subs: list[dict], upto: int) -> dict:
    """Candle htf yang BELUM tutup, dibentuk dari subs[0..upto] (inklusif)."""
    window = subs[:upto + 1]
    return {
        "timeframe":   tf,
        "open_time":   open_time,
        "open":        window[0]["open"],
        "high":        max(s["high"] for s in window),
        "low":         min(s["low"] for s in window),
        "close":       window[upto]["close"],
        "volume":      sum(s.get("volume", 0.0) for s in window),
        "buy_volume":  0.0,
        "sell_volume": 0.0,
        "tick_count":  0,
    }


def _resolve_exit_detail(pos: dict, subs: list[dict], sl_pct: float, tp_pct: float,
                         time_stop_sec: int):
    """
    Telusuri sub-bar 1m IN ORDER → exit pertama (SL/TP/TimeStop) yang kena menang.
    Prioritas dalam satu sub-bar: SL > TP > TimeStop (pesimis, sama dgn level-candle).
    Return (exit_price, reason, t_exit) atau None kalau tak ada exit di window ini.
    """
    ep = pos["entry_price"]
    side = pos["side"]
    entry_time = pos["entry_time"]
    if side == "long":
        tp_price = ep * (1 + tp_pct)
        sl_price = ep * (1 - sl_pct)
    else:
        tp_price = ep * (1 - tp_pct)
        sl_price = ep * (1 + sl_pct)

    for sb in subs:
        t = sb["open_time"]
        if t < entry_time:
            continue  # sub-bar sebelum entry tak relevan
        h = sb["high"]; l = sb["low"]; o = sb["open"]; c = sb["close"]
        if side == "long":
            if l <= sl_price:
                base = min(sl_price, o)            # gap turun → fill di open (lebih buruk)
                return _fill_price(base, "sell"), "SL", t
            if h >= tp_price:
                return _fill_price(tp_price, "sell"), "TP", t
            if time_stop_sec > 0 and (t - entry_time) >= time_stop_sec:
                return _fill_price(c, "sell"), "TimeStop", t
        else:
            if h >= sl_price:
                base = max(sl_price, o)            # gap naik → fill di open
                return _fill_price(base, "buy"), "SL", t
            if l <= tp_price:
                return _fill_price(tp_price, "buy"), "TP", t
            if time_stop_sec > 0 and (t - entry_time) >= time_stop_sec:
                return _fill_price(c, "buy"), "TimeStop", t
    return None


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
def simulate_single(strategy_module, candles: list[dict], tf: str,
                    subbars: list[list[dict]] | None = None) -> dict:
    """Strategy handle entry + exit. Bot live behavior.
    subbars != None → mode realisme 1m (eksekusi/exit intrabar, lihat
    _simulate_single_detail)."""
    if subbars is not None:
        return _simulate_single_detail(strategy_module, candles, tf, subbars)
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
        current  = candles[i]
        closed   = candles[:i]
        fill_ref = current["open"]    # fill di OPEN bar berikutnya (bukan close bar sinyal)
        mark     = current["close"]   # harga mark-to-market untuk equity curve

        sync_strategy_state(strategy_module)
        data = build_data_dict(closed, current, tf)

        try:
            signal = strategy_module.generate_signal(data)
        except Exception as e:
            signal = {"action": "hold", "reason": f"err: {e}"}

        action = _extract_action(signal)

        if action == "buy" and _MOCK_STATE["side"] == "none":
            entry = _fill_price(fill_ref, "buy")
            _MOCK_STATE["side"]        = "long"
            _MOCK_STATE["entry_price"] = entry
            _MOCK_STATE["entry_time"]  = current["open_time"]
            _MOCK_STATE["qty"]         = notional / entry
            _MOCK_STATE["open_time"]   = current["open_time"]

        elif action == "sell" and _MOCK_STATE["side"] == "none":
            entry = _fill_price(fill_ref, "sell")
            _MOCK_STATE["side"]        = "short"
            _MOCK_STATE["entry_price"] = entry
            _MOCK_STATE["entry_time"]  = current["open_time"]
            _MOCK_STATE["qty"]         = notional / entry
            _MOCK_STATE["open_time"]   = current["open_time"]

        elif action == "close" and _MOCK_STATE["side"] in ("long", "short"):
            ep  = _MOCK_STATE["entry_price"]
            qty = _MOCK_STATE["qty"]
            if _MOCK_STATE["side"] == "long":
                exitp = _fill_price(fill_ref, "sell")   # tutup long = jual @ bid
                gross = (exitp - ep) * qty
            else:
                exitp = _fill_price(fill_ref, "buy")    # tutup short = beli @ ask
                gross = (ep - exitp) * qty
            fee     = notional * FEE_RATE * 2
            funding = _funding_cost(notional, current["open_time"] - _MOCK_STATE["entry_time"])
            pnl     = gross - fee - funding
            balance += pnl
            trades.append({"side": _MOCK_STATE["side"], "entry": ep, "exit": exitp,
                           "pnl": pnl, "fee": fee, "funding": funding,
                           "reason": signal.get("reason", "")})
            _MOCK_STATE["side"]        = "none"
            _MOCK_STATE["entry_price"] = 0.0
            _MOCK_STATE["qty"]         = 0.0
            _MOCK_STATE["entry_time"]  = 0.0
            _MOCK_STATE["open_time"]   = None

        if _MOCK_STATE["side"] == "long":
            unreal = (mark - _MOCK_STATE["entry_price"]) * _MOCK_STATE["qty"]
        elif _MOCK_STATE["side"] == "short":
            unreal = (_MOCK_STATE["entry_price"] - mark) * _MOCK_STATE["qty"]
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


def simulate_multi(strategy_module, candles: list[dict], tf: str,
                   subbars: list[list[dict]] | None = None) -> dict:
    """
    Tiap "buy"/"sell" → trade independen. Simulator handle SL/TP/TimeStop pakai
    konstanta yang dibaca dari strategy module. "close" diabaikan.
    subbars != None → mode realisme 1m (SL/TP/TimeStop ditelusuri per sub-bar,
    lihat _simulate_multi_detail).
    """
    if subbars is not None:
        return _simulate_multi_detail(strategy_module, candles, tf, subbars)
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
        open_   = current["open"]
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
                    # Gap: kalau bar BUKA di bawah stop, fill di open (lebih buruk),
                    # bukan persis di sl_price. Lalu spread+slippage.
                    base = min(sl_price, open_)
                    exit_price = _fill_price(base, "sell"); reason = "SL"
                elif high >= tp_price:
                    exit_price = _fill_price(tp_price, "sell"); reason = "TP"
                elif time_stop_sec > 0 and (t - entry_time) >= time_stop_sec:
                    exit_price = _fill_price(close, "sell"); reason = "TimeStop"

                if exit_price is not None:
                    gross   = (exit_price - ep) * qty
                    fee     = notional * FEE_RATE * 2
                    funding = _funding_cost(notional, t - entry_time)
                    pnl     = gross - fee - funding
                    balance += pnl
                    trades.append({"side": "long", "entry": ep, "exit": exit_price,
                                   "pnl": pnl, "fee": fee, "funding": funding, "reason": reason})
                else:
                    still_open.append(pos)
            else:  # short
                tp_price = ep * (1 - tp_pct)
                sl_price = ep * (1 + sl_pct)
                exit_price = None
                reason = ""
                if high >= sl_price:
                    # Gap naik → fill di open (lebih buruk) bukan persis sl_price.
                    base = max(sl_price, open_)
                    exit_price = _fill_price(base, "buy"); reason = "SL"
                elif low <= tp_price:
                    exit_price = _fill_price(tp_price, "buy"); reason = "TP"
                elif time_stop_sec > 0 and (t - entry_time) >= time_stop_sec:
                    exit_price = _fill_price(close, "buy"); reason = "TimeStop"

                if exit_price is not None:
                    gross   = (ep - exit_price) * qty
                    fee     = notional * FEE_RATE * 2
                    funding = _funding_cost(notional, t - entry_time)
                    pnl     = gross - fee - funding
                    balance += pnl
                    trades.append({"side": "short", "entry": ep, "exit": exit_price,
                                   "pnl": pnl, "fee": fee, "funding": funding, "reason": reason})
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
            entry = _fill_price(open_, "buy")   # fill di OPEN + spread/slippage
            open_positions.append({"side": "long", "entry_price": entry,
                                   "entry_time": t, "qty": notional / entry})
        elif action == "sell":
            entry = _fill_price(open_, "sell")
            open_positions.append({"side": "short", "entry_price": entry,
                                   "entry_time": t, "qty": notional / entry})
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
# SIMULATOR REALISME 1m — varian intrabar dari simulate_single / simulate_multi
# =============================================================================
# Sinyal strategi TETAP di TF tinggi. Yang berubah: EKSEKUSI ditelusuri per
# sub-bar 1m di dalam tiap candle TF tinggi → urutan SL/TP & exit intrabar nyata.
def _simulate_single_detail(strategy_module, candles: list[dict], tf: str,
                            subbars: list[list[dict]]) -> dict:
    """
    Single-position realisme: strategi DIPANGGIL tiap sub-bar 1m dengan candle
    htf yang BELUM tutup (forming) → strategi "lihat candle naik-turun" & bisa
    keluar intrabar. Entry tak spam karena dijaga state posisi (side != none).
    Equity dicatat sekali per candle htf (konsisten dgn mode biasa).
    """
    global _MOCK_STATE
    _MOCK_STATE = {"side": "none", "entry_price": 0.0, "qty": 0.0,
                   "entry_time": 0.0, "open_time": None}
    reset_bot_state(strategy_module)
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
        subs = subbars[i] if i < len(subbars) else []
        if not subs:
            subs = [current]   # tak ada data 1m → perlakukan candle sbg 1 sub-bar

        for k, sb in enumerate(subs):
            forming = _build_forming(current["open_time"], tf, subs, k)
            sync_strategy_state(strategy_module)
            data = build_data_dict(closed, forming, tf)
            try:
                signal = strategy_module.generate_signal(data)
            except Exception as e:
                signal = {"action": "hold", "reason": f"err: {e}"}
            action = _extract_action(signal)
            fill_ref = sb["open"]
            t = sb["open_time"]

            if action == "buy" and _MOCK_STATE["side"] == "none":
                entry = _fill_price(fill_ref, "buy")
                _MOCK_STATE["side"]        = "long"
                _MOCK_STATE["entry_price"] = entry
                _MOCK_STATE["entry_time"]  = t
                _MOCK_STATE["qty"]         = notional / entry
                _MOCK_STATE["open_time"]   = t
            elif action == "sell" and _MOCK_STATE["side"] == "none":
                entry = _fill_price(fill_ref, "sell")
                _MOCK_STATE["side"]        = "short"
                _MOCK_STATE["entry_price"] = entry
                _MOCK_STATE["entry_time"]  = t
                _MOCK_STATE["qty"]         = notional / entry
                _MOCK_STATE["open_time"]   = t
            elif action == "close" and _MOCK_STATE["side"] in ("long", "short"):
                ep  = _MOCK_STATE["entry_price"]
                qty = _MOCK_STATE["qty"]
                if _MOCK_STATE["side"] == "long":
                    exitp = _fill_price(fill_ref, "sell"); gross = (exitp - ep) * qty
                else:
                    exitp = _fill_price(fill_ref, "buy");  gross = (ep - exitp) * qty
                fee     = notional * FEE_RATE * 2
                funding = _funding_cost(notional, t - _MOCK_STATE["entry_time"])
                pnl     = gross - fee - funding
                balance += pnl
                trades.append({"side": _MOCK_STATE["side"], "entry": ep, "exit": exitp,
                               "pnl": pnl, "fee": fee, "funding": funding,
                               "reason": signal.get("reason", "")})
                _MOCK_STATE["side"]        = "none"
                _MOCK_STATE["entry_price"] = 0.0
                _MOCK_STATE["qty"]         = 0.0
                _MOCK_STATE["entry_time"]  = 0.0
                _MOCK_STATE["open_time"]   = None

        mark = current["close"]
        if _MOCK_STATE["side"] == "long":
            unreal = (mark - _MOCK_STATE["entry_price"]) * _MOCK_STATE["qty"]
        elif _MOCK_STATE["side"] == "short":
            unreal = (_MOCK_STATE["entry_price"] - mark) * _MOCK_STATE["qty"]
        else:
            unreal = 0.0
        equity_curve.append(balance + unreal)

    return {"trades": trades, "equity_curve": equity_curve, "final_balance": balance}


def _simulate_multi_detail(strategy_module, candles: list[dict], tf: str,
                           subbars: list[list[dict]]) -> dict:
    """
    Multi-position realisme: strategi dipanggil SEKALI per candle htf (sinyal &
    jumlah entry identik mode biasa → tak ada spam), tapi SL/TP/TimeStop tiap
    posisi terbuka ditelusuri per sub-bar 1m (urutan sentuhan nyata).
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
        open_   = current["open"]
        t       = current["open_time"]
        subs = subbars[i] if i < len(subbars) else []
        if not subs:
            subs = [current]   # fallback: tak ada 1m → logika level-candle

        # --- Exit posisi terbuka, ditelusuri per sub-bar 1m ---
        still_open: list[dict] = []
        for pos in open_positions:
            ex = _resolve_exit_detail(pos, subs, sl_pct, tp_pct, time_stop_sec)
            if ex is not None:
                exit_price, reason, t_exit = ex
                ep = pos["entry_price"]; qty = pos["qty"]
                entry_time = pos["entry_time"]; side = pos["side"]
                gross = (exit_price - ep) * qty if side == "long" else (ep - exit_price) * qty
                fee     = notional * FEE_RATE * 2
                funding = _funding_cost(notional, t_exit - entry_time)
                pnl     = gross - fee - funding
                balance += pnl
                trades.append({"side": side, "entry": ep, "exit": exit_price,
                               "pnl": pnl, "fee": fee, "funding": funding, "reason": reason})
            else:
                still_open.append(pos)
        open_positions = still_open

        # --- Entry: keputusan SEKALI per candle htf (current = candle penuh) ---
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
            entry = _fill_price(open_, "buy")
            open_positions.append({"side": "long", "entry_price": entry,
                                   "entry_time": t, "qty": notional / entry})
        elif action == "sell":
            entry = _fill_price(open_, "sell")
            open_positions.append({"side": "short", "entry_price": entry,
                                   "entry_time": t, "qty": notional / entry})

        unreal = 0.0
        for pos in open_positions:
            if pos["side"] == "long":
                unreal += (close - pos["entry_price"]) * pos["qty"]
            else:
                unreal += (pos["entry_price"] - close) * pos["qty"]
        equity_curve.append(balance + unreal)

    return {"trades": trades, "equity_curve": equity_curve, "final_balance": balance}
