from __future__ import annotations

import pandas as pd
import numpy as np
import joblib
import os
import sys
import time
import warnings
import types as _types
from typing import Any, Optional

from ft_types import BotState, MarketDataSnapshot, Signal

# ==========================================
# 1. INISIALISASI MODEL ML & SCALER (OPSIONAL)
# ==========================================
# ML sepenuhnya opsional — jika file .pkl tidak ditemukan, model = None
# dan strategy bisa tetap jalan menggunakan logika indikator biasa.
MODEL_PATH  = 'trading_model_15m.pkl'
SCALER_PATH = 'trading_scaler_15m.pkl'

try:
    from sklearn.exceptions import InconsistentVersionWarning
    warnings.filterwarnings("ignore", category=InconsistentVersionWarning)
except ImportError:
    pass  # sklearn tidak terinstall → tidak apa-apa jika tidak pakai ML

# Cache model ke sys.modules agar hanya di-load 1x per proses
# (mencegah error "cannot load module more than once" dari sklearn C-extensions)
if '_strategy_ml_cache' not in sys.modules:
    _cache = _types.ModuleType('_strategy_ml_cache')
    _cache.model  = None  # type: ignore[attr-defined]
    _cache.scaler = None  # type: ignore[attr-defined]
    if os.path.exists(MODEL_PATH) and os.path.exists(SCALER_PATH):
        try:
            _cache.model  = joblib.load(MODEL_PATH)   # type: ignore[attr-defined]
            _cache.scaler = joblib.load(SCALER_PATH)  # type: ignore[attr-defined]
            print(f"[STRATEGY] [OK] Model ML dimuat dari '{MODEL_PATH}'")
        except Exception as _e:
            print(f"[STRATEGY] [WARN] Gagal load model ML: {_e} - Strategy jalan tanpa ML.")
    else:
        print(f"[STRATEGY] [INFO] File .pkl tidak ditemukan - Strategy jalan tanpa ML (pure indicator mode).")
    sys.modules['_strategy_ml_cache'] = _cache

_ml_cache: Any = sys.modules['_strategy_ml_cache']
model  = _ml_cache.model
scaler = _ml_cache.scaler

# Flag untuk dipakai di generate_signal()
ML_ENABLED: bool = (model is not None and scaler is not None)

# ==========================================
# 2. STATE MANAGER (UNTUK TRIPLE BARRIER)
# ==========================================
TARGET_TP_PCT: float = 0.004  # 0.4% Take Profit target
TARGET_SL_PCT: float = 0.004  # 0.4% Stop Loss target
TIME_STOP_SEC: int   = 3600   # 1 Jam Time Stop target

# Menyimpan status posisi berjalan secara internal untuk eksekusi Stop Loss, Take Profit, dan Time Stop
bot_state: BotState = {
    "in_position": False,
    "side": "none",       # BUG-04 FIX: tambah side tracking untuk short support
    "entry_price": 0.0,
    "entry_time": 0.0
}

# Minimum candle warmup sebelum strategi mulai
MIN_CANDLES: int = 50

# ==========================================
# 3. LOGIKA UTAMA BOT
# ==========================================
def generate_signal(data: MarketDataSnapshot) -> Signal:
    global bot_state

    # Cegah aktivitas trading jika engine masih tahap preload data historis
    if data.get("is_warmup", True):
        return {"action": "hold", "reason": "Sistem sedang Warmup Data Historis"}

    # Ambil candle dari timeframe apapun yang dikirim engine (bukan hardcode "15m")
    candles_tf: dict[str, object] = {}
    if data.get("candles"):
        # Ambil timeframe pertama yang ada
        for _tf_key, _clist in data["candles"].items():
            if _clist:
                candles_tf = {"tf": _tf_key, "list": _clist}
                break

    candles_list: list[object] = candles_tf.get("list", [])  # type: ignore[assignment]
    if len(candles_list) < 50:
        return {"action": "hold", "reason": f"Menunggu buffer candle mencapai 50 (sekarang {len(candles_list)})"}

    # Konversi ke Pandas DataFrame
    df: pd.DataFrame = pd.DataFrame(candles_list)

    # Ekstrak harga/waktu
    current_close: float = float(df['close'].iloc[-1])
    best_ask_data: dict[str, float] = data.get("best_ask", {})  # type: ignore[assignment]
    best_bid_data: dict[str, float] = data.get("best_bid", {})  # type: ignore[assignment]
    current_ask: float = best_ask_data.get("price", current_close)
    current_bid: float = best_bid_data.get("price", current_close)

    # ⚠️ PENTING: Gunakan open_time candle sebagai referensi waktu.
    # Ini membuat Time Stop bekerja baik saat LIVE maupun saat BACKTEST.
    # Jika pakai time.time(), backtest yang selesai dalam detik tidak akan pernah
    # memicu Time Stop karena elapsed time selalu mendekati 0.
    #
    # Format data["current"] = {"15m": {open_time: ..., close: ...}}
    # Ambil candle dict dari value pertama (apapun timeframe-nya)
    _current_dict: dict[str, object] = data.get("current", {})  # type: ignore[assignment]
    _candle_obj: object = next(iter(_current_dict.values()), {}) if _current_dict else {}
    candle_time: float = float(_candle_obj.get("open_time", 0)) if isinstance(_candle_obj, dict) else 0.0  # type: ignore[union-attr]
    # Fallback ke waktu sekarang hanya jika tidak ada data candle (mode live tanpa current)
    current_time: float = candle_time if candle_time > 0 else time.time()

    # ---------------------------------------------------------
    # A. LOGIKA KELUAR (EXIT: TRIPLE BARRIER)
    # ---------------------------------------------------------
    if bot_state["in_position"]:
        entry_price: float = bot_state["entry_price"]
        pos_side: str      = bot_state.get("side", "long")  # BUG-04 FIX: gunakan side yang disimpan

        if pos_side == "long":
            # Barrier 1 & 2: Take Profit dan Stop Loss untuk LONG
            tp_price: float = entry_price * (1 + TARGET_TP_PCT)
            sl_price: float = entry_price * (1 - TARGET_SL_PCT)

            if current_bid <= sl_price:
                bot_state["in_position"] = False
                bot_state["side"] = "none"
                return {"action": "close", "reason": f"[LONG] Hit SL Darurat (-{TARGET_SL_PCT*100}%)"}

            if current_bid >= tp_price:
                bot_state["in_position"] = False
                bot_state["side"] = "none"
                return {"action": "close", "reason": f"[LONG] Hit TP Target (+{TARGET_TP_PCT*100}%)"}

        else:  # short
            # BUG-04 FIX: Triple Barrier untuk SHORT (SL/TP dibalik)
            tp_price = entry_price * (1 - TARGET_TP_PCT)  # TP saat harga TURUN
            sl_price = entry_price * (1 + TARGET_SL_PCT)  # SL saat harga NAIK

            if current_ask >= sl_price:
                bot_state["in_position"] = False
                bot_state["side"] = "none"
                return {"action": "close", "reason": f"[SHORT] Hit SL Darurat (+{TARGET_SL_PCT*100}%)"}

            if current_ask <= tp_price:
                bot_state["in_position"] = False
                bot_state["side"] = "none"
                return {"action": "close", "reason": f"[SHORT] Hit TP Target (-{TARGET_TP_PCT*100}%)"}

        # Barrier 3: Time Stop (berlaku untuk LONG dan SHORT)
        if bot_state["entry_time"] > 0:
            time_elapsed: float = current_time - bot_state["entry_time"]
            if time_elapsed >= TIME_STOP_SEC:
                bot_state["in_position"] = False
                bot_state["side"] = "none"
                return {"action": "close", "reason": f"Hit Time Stop ({TIME_STOP_SEC} Detik Berlalu)"}

        # Jika belum menyentuh barrier apa-apa, tahan posisi
        return {"action": "hold", "reason": "Posisi terbuka, memantau Triple Barrier..."}

    # ---------------------------------------------------------
    # B. LOGIKA MASUK (ENTRY: MACHINE LEARNING & FEATURE ENGINEERING)
    # ---------------------------------------------------------
    try:
        # 1. Fitur Pergerakan & Lag
        df['return'] = df['close'].pct_change()
        for i in [1, 3, 5]:
            df[f'return_lag_{i}'] = df['return'].shift(i)
            df[f'volume_lag_{i}'] = df['volume'].shift(i)

        # 2. Fitur Momentum Volume (Surge Ratio)
        df['vol_ma_20'] = df['volume'].rolling(window=20).mean()
        df['vol_surge_ratio'] = df['volume'] / df['vol_ma_20']

        # 3. Indikator Klasik (RSI 14 & MACD 12, 26)
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        df['RSI_14'] = 100 - (100 / (1 + (gain / loss)))

        exp1 = df['close'].ewm(span=12, adjust=False).mean()
        exp2 = df['close'].ewm(span=26, adjust=False).mean()
        df['MACD'] = exp1 - exp2

        # 4. Filter Kolom dan Pembersihan NaN
        df.dropna(inplace=True)
        if len(df) == 0:
            return {"action": "hold", "reason": "Data tidak cukup setelah perhitungan indikator"}

        fitur_wajib: list[str] = [
            'volume', 'return', 'return_lag_1', 'volume_lag_1',
            'return_lag_3', 'volume_lag_3', 'return_lag_5', 'volume_lag_5',
            'vol_ma_20', 'vol_surge_ratio', 'RSI_14', 'MACD'
        ]

        # Ambil baris terakhir saja (candle terbaru yang telah selesai terbentuk)
        baris_terbaru: pd.DataFrame = df[fitur_wajib].iloc[-1:]

        # 5. Transformasi Data dengan Scaler dan Prediksi Model
        # NEW-03 FIX: Cek ML_ENABLED sebelum akses scaler/model.
        # Tanpa guard ini, jika .pkl tidak ada (ML disabled), setiap tick masuk except
        # → bot_monitor consecutive_errors naik terus → dashboard selalu merah "ERROR"
        # padahal bot berjalan normal dalam pure-indicator mode.
        if not ML_ENABLED:
            return {"action": "hold", "reason": "ML tidak aktif (file .pkl tidak ditemukan), gunakan pure indicator mode"}

        data_scaled = scaler.transform(baris_terbaru)
        proba: np.ndarray = model.predict_proba(data_scaled)[0]  # [prob_turun, prob_naik]
        probabilitas_naik: float  = float(proba[1])
        probabilitas_turun: float = float(proba[0])  # BUG-04 FIX: tambah probabilitas turun untuk sinyal short

        # 6. Evaluasi Trigger Entry
        if probabilitas_naik > 0.60:
            bot_state["in_position"] = True
            bot_state["side"]        = "long"   # BUG-04 FIX: simpan side
            bot_state["entry_price"] = current_ask  # Catat harga saat beli
            bot_state["entry_time"]  = current_time  # Catat waktu untuk Time Stop
            return {
                "action": "buy",
                "reason": f"Sinyal RF ML Prob Naik > 60% (Skor: {probabilitas_naik:.2f})"
            }

        # BUG-04 FIX: Sinyal SELL/SHORT saat probabilitas turun > 60%
        if probabilitas_turun > 0.60:
            bot_state["in_position"] = True
            bot_state["side"]        = "short"  # simpan side short
            bot_state["entry_price"] = current_bid  # Catat harga saat jual
            bot_state["entry_time"]  = current_time
            return {
                "action": "sell",
                "reason": f"Sinyal RF ML Prob Turun > 60% (Skor: {probabilitas_turun:.2f})"
            }

    except Exception as e:
        return {"action": "hold", "reason": f"Error Eksekusi ML: {str(e)}"}

    return {"action": "hold", "reason": "Tidak ada sinyal, probabilitas <= 60%"}


# =============================================================================
# ON_TICK — Entry point yang dipanggil main.py setiap candle/tick baru
# =============================================================================
def on_tick(data: MarketDataSnapshot) -> None:
    """
    Dipanggil oleh main.py setiap ada candle baru (KLINE mode) atau tick baru (TICK mode).
    Menghasilkan sinyal dari generate_signal() lalu meneruskan ke execution.place_order().
    Juga melaporkan metrik ke bot_monitor untuk health tracking.
    """
    import execution   # import di sini untuk hindari circular dependency
    import bot_monitor

    # --- Sinkronisasi Memori (Mencegah Amnesia Posisi setelah Restart) ---
    try:
        import position_manager as _pm
        _pos: dict[str, object] = _pm.get_position()
        _pos_side: str    = str(_pos.get("side", "none"))
        _entry_price: float = float(_pos.get("entry_price", 0.0))  # type: ignore[arg-type]

        global bot_state
        if _pos_side in ("long", "short"):
            bot_state["in_position"] = True
            bot_state["side"] = _pos_side  # BUG-04 FIX: sync side dari position_manager
            if _entry_price > 0:
                bot_state["entry_price"] = _entry_price
            # Fix 3: Pulihkan entry_time jika hilang setelah restart
            # Tanpa ini, Time Stop tidak akan pernah terpicu setelah bot restart.
            if bot_state.get("entry_time", 0) == 0:
                _open_time: Optional[float] = _pos.get("open_time")  # type: ignore[assignment]
                if _open_time:
                    bot_state["entry_time"] = _open_time
        else:
            bot_state["in_position"] = False
            bot_state["side"] = "none"  # BUG-04 FIX: reset side juga
            bot_state["entry_time"] = 0.0  # reset agar Time Stop tidak terpicu palsu
    except Exception:
        pass
    # ---------------------------------------------------------------------

    signal: Signal
    try:
        signal = generate_signal(data)
    except Exception as e:
        err_reason: str = f"generate_signal() crash: {type(e).__name__}: {e}"
        bot_monitor.record_error(err_reason)
        return

    # Ambil probabilitas ML terakhir jika tersedia (untuk health tracking)
    _prob: Optional[float] = None
    reason: str = signal.get("reason", "")
    if "Skor:" in reason:
        try:
            _prob = float(reason.split("Skor:")[1].split(")")[0].strip())
        except Exception:
            pass

    # Ambil equity & position_side dari position_manager untuk deteksi anomali
    _equity: Optional[float]
    _pos_side_mon: Optional[str]
    try:
        import position_manager as _pm
        _pnl_summary = _pm.get_pnl_summary()
        _equity       = float(_pnl_summary.get("equity", 0.0))  # type: ignore[arg-type]
        _pos_side_mon = str(_pnl_summary.get("side", "none"))
    except Exception:
        _equity       = None
        _pos_side_mon = None

    # Laporkan ke health tracker (sertakan data mentah untuk cek kualitas)
    bot_monitor.record_tick(signal, ml_prob=_prob, data=data,  # type: ignore[arg-type]
                            equity=_equity, position_side=_pos_side_mon)

    # Teruskan ke execution jika ada aksi nyata
    action: str = signal.get("action", "hold")
    if action != "hold":
        execution.place_order(signal)