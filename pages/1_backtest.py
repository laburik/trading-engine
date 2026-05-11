import sys
import time
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import numpy as np
import pandas as pd
import streamlit as st
from datetime import datetime, timezone
from unittest.mock import MagicMock
from config import SYMBOL, INITIAL_BALANCE, FEE_RATE

st.set_page_config(page_title="🔬 Backtest", page_icon="🔬", layout="wide")

st.markdown("""
<style>
    .main { background-color: #0e1117; }
</style>
""", unsafe_allow_html=True)

# =============================================================================
# HELPERS
# =============================================================================
_BYBIT_TF_MAP = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720", "1d": "D",
}

def _ts(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

@st.cache_data(ttl=60, show_spinner=False)
def fetch_backtest_candles(symbol: str, interval_label: str, limit: int) -> list:
    bybit_interval = _BYBIT_TF_MAP.get(interval_label, "1")
    interval_min   = int(bybit_interval) if bybit_interval.isdigit() else 1440
    url = "https://api.bybit.com/v5/market/kline"
    params = {"category": "linear", "symbol": symbol, "interval": bybit_interval, "limit": limit}
    resp = requests.get(url, params=params, timeout=15, verify=False)
    try:
        body = resp.json()
    except Exception as e:
        raise ValueError(f"Bukan JSON (HTTP {resp.status_code}): {resp.text[:200]}")
    if body.get("retCode") != 0:
        raise ValueError(f"Bybit API error: {body.get('retMsg', '')}")
    rows = sorted(body["result"]["list"], key=lambda r: int(r[0]))
    interval_sec = interval_min * 60
    if rows:
        newest_ms = int(rows[-1][0])
        if newest_ms + interval_sec * 1000 > int(time.time() * 1000):
            rows = rows[:-1]
    return [
        {
            "timeframe":  interval_label,
            "open_time":  int(r[0]) / 1000.0,
            "open":   float(r[1]), "high":  float(r[2]),
            "low":    float(r[3]), "close": float(r[4]),
            "volume": float(r[5]),
            "buy_volume": 0.0, "sell_volume": 0.0, "tick_count": 0,
        }
        for r in rows
    ]

@st.cache_resource(show_spinner=False)
def _init_strategy_mocks():
    sys.modules.setdefault("execution",        MagicMock())
    sys.modules.setdefault("position_manager", MagicMock())
    return True

def _load_strategy_module():
    """
    Reload strategy dengan aman agar Streamlit membaca perubahan tanpa restart.
    """
    _init_strategy_mocks()
    import strategy as strat
    import importlib
    return importlib.reload(strat)

# =============================================================================
# MODE 1: COLAB-COMPATIBLE (Original Training Backtest)
# Mereplikasi logika backtest saat pelatihan model di Colab:
# - Setiap candle dengan prob > 0.60 langsung jadi 1 trade INDEPENDEN
# - Outcome ditentukan dengan cek future_high (max high 4 candle ke depan)
# - Modal per trade FIXED (bukan all-in)
# - Bisa multi-posisi paralel (tidak ada position lock)
# =============================================================================
def run_colab_mode(candles: list, tf_label: str, modal_per_trade: float) -> tuple[list, list]:
    strat = _load_strategy_module()

    TARGET_PROFIT_PCT = getattr(strat, "TARGET_TP_PCT", 0.004)    # Ambil dari strategy.py
    FUTURE_CANDLES    = 4        # 4 candle ke depan = 1 jam di 15m
    FEE_PER_TRADE     = modal_per_trade * FEE_RATE * 2  # entry fee + exit fee (2 sisi, total 0.04%)

    MIN = getattr(strat, "MIN_CANDLES", 50)

    # Hitung fitur ML untuk semua candle sekaligus (vectorized, seperti Colab)
    df = pd.DataFrame(candles)

    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df['return']        = df['close'].pct_change()
        for lag in [1, 3, 5]:
            df[f'return_lag_{lag}'] = df['return'].shift(lag)
            df[f'volume_lag_{lag}'] = df['volume'].shift(lag)
        df['vol_ma_20']      = df['volume'].rolling(window=20).mean()
        df['vol_surge_ratio']= df['volume'] / df['vol_ma_20']
        delta = df['close'].diff()
        gain  = delta.where(delta > 0, 0).rolling(window=14).mean()
        loss  = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        df['RSI_14'] = 100 - (100 / (1 + (gain / loss)))
        exp1 = df['close'].ewm(span=12, adjust=False).mean()
        exp2 = df['close'].ewm(span=26, adjust=False).mean()
        df['MACD'] = exp1 - exp2

        # future_high: max HIGH dari 4 candle SETELAH candle ini (persis logika Colab)
        # Formula: rolling(4).max() backward → hasilkan max per window 4
        #          shift(-4) → geser ke depan sehingga index i = max(high[i+1..i+4])
        df['future_high'] = (
            df['high']
            .rolling(window=FUTURE_CANDLES, min_periods=FUTURE_CANDLES)
            .max()
            .shift(-FUTURE_CANDLES)
        )

    fitur_wajib = [
        'volume', 'return', 'return_lag_1', 'volume_lag_1',
        'return_lag_3', 'volume_lag_3', 'return_lag_5', 'volume_lag_5',
        'vol_ma_20', 'vol_surge_ratio', 'RSI_14', 'MACD'
    ]

    # Hapus baris NaN (warmup + FUTURE_CANDLES candle terakhir tidak bisa dievaluasi)
    df_clean = df.dropna(subset=fitur_wajib + ['future_high']).copy()
    df_clean = df_clean.iloc[MIN:]  # skip warmup awal

    if df_clean.empty:
        return [], []

    # Batch predict semua candle sekaligus (jauh lebih efisien)
    X = df_clean[fitur_wajib].values
    X_scaled = strat.scaler.transform(X)
    probas = strat.model.predict_proba(X_scaled)[:, 1]

    signals       = []
    capital       = INITIAL_BALANCE
    equity_history = []

    for idx, (row_idx, row) in enumerate(df_clean.iterrows()):
        prob         = probas[idx]
        t_str        = _ts(int(row['open_time'] * 1000))
        entry_price  = row['close']
        future_high  = row['future_high']

        if prob > 0.60:
            tp_price = entry_price * (1 + TARGET_PROFIT_PCT)

            if future_high >= tp_price:
                # WIN: harga mencapai TP dalam 4 candle ke depan
                pnl = (modal_per_trade * TARGET_PROFIT_PCT) - FEE_PER_TRADE
                status = "WIN (TP)"
            else:
                # LOSS: tidak mencapai TP → SL/Time Stop
                pnl = -(modal_per_trade * TARGET_PROFIT_PCT) - FEE_PER_TRADE
                status = "LOSS (SL/Time)"

            capital += pnl
            signals.append({
                "bar":    t_str,
                "action": status,
                "price":  round(entry_price, 5),
                "pnl":    round(pnl, 4),
                "prob":   round(prob, 3),
            })

        equity_history.append({
            "bar":     t_str,
            "balance": round(capital, 4),
            "equity":  round(capital, 4),
        })

    return signals, equity_history


# =============================================================================
# MODE 2: SIMULASI LIVE (Sequential bar-by-bar, MULTI-POSISI)
# Mereplikasi cara bot bekerja saat live trading DENGAN multi-posisi:
# - Setiap candle selalu dievaluasi untuk entry (tidak diblok posisi lain)
# - Setiap posisi punya TP/SL/Time Stop masing-masing
# - Modal per trade = MODAL_PER_TRADE (fixed, tidak all-in)
# - Engine yang mengelola posisi (bukan bot_state strategy)
# =============================================================================
def run_live_mode(candles: list, tf_label: str, initial_bal: float,
                  modal_per_trade: float = 100.0) -> tuple[list, list]:
    strat = _load_strategy_module()
    MIN = getattr(strat, "MIN_CANDLES", 50)

    TARGET_TP  = getattr(strat, "TARGET_TP_PCT", 0.004)
    TARGET_SL  = getattr(strat, "TARGET_SL_PCT", 0.004)
    TIME_STOP  = getattr(strat, "TIME_STOP_SEC", 3600)

    signals        = []
    equity_history = []
    balance        = initial_bal
    open_positions = []  # list of {entry_price, entry_time, qty, fee_paid}

    def _build_data(closed, current):
        return {
            "candles":  {tf_label: closed},
            "current":  {tf_label: current},
            "best_bid": {"price": current["close"] * 0.9999, "qty": 1.0},
            "best_ask": {"price": current["close"] * 1.0001, "qty": 1.0},
            "bid_ask_spread":      current["close"] * 0.0002,
            "orderbook_imbalance": 0.0,
            "volume_delta":        0.0,
            "funding_rate":        0.0001,
            "latest_tick": {"price": current["close"], "qty": 1.0,
                            "side": "Buy", "timestamp": current["open_time"]},
            "is_warmup": False,
        }

    for i in range(MIN, len(candles)):
        closed       = candles[:i]
        current      = candles[i]
        close        = current["close"]
        candle_time  = current["open_time"]
        t_str        = _ts(int(candle_time * 1000))

        # -----------------------------------------------------------------
        # 1. Cek exit untuk SEMUA posisi yang sedang terbuka
        # -----------------------------------------------------------------
        high_price = current["high"]
        low_price  = current["low"]

        still_open = []
        for pos in open_positions:
            ep   = pos["entry_price"]
            tp_p = ep * (1 + TARGET_TP)
            sl_p = ep * (1 - TARGET_SL)
            elapsed = candle_time - pos["entry_time"]

            if high_price >= tp_p:
                pnl = (modal_per_trade * TARGET_TP) - (modal_per_trade * FEE_RATE)
                balance += pnl
                signals.append({"bar": t_str, "action": "CLOSE (TP)",
                                 "entry": round(ep, 5), "exit": round(tp_p, 5),
                                 "pnl": round(pnl, 4), "reason": "Hit TP (+0.4%)"})
            elif low_price <= sl_p:
                pnl = -(modal_per_trade * TARGET_SL) - (modal_per_trade * FEE_RATE)
                balance += pnl
                signals.append({"bar": t_str, "action": "CLOSE (SL)",
                                 "entry": round(ep, 5), "exit": round(sl_p, 5),
                                 "pnl": round(pnl, 4), "reason": "Hit SL (-0.4%)"})
            elif elapsed >= TIME_STOP:
                gross = close - ep
                pnl   = (modal_per_trade * gross / ep) - (modal_per_trade * FEE_RATE)
                balance += pnl
                signals.append({"bar": t_str, "action": "CLOSE (Time)",
                                 "entry": round(ep, 5), "exit": round(close, 5),
                                 "pnl": round(pnl, 4), "reason": "Time Stop (1 Jam)"})
            else:
                still_open.append(pos)  # posisi masih aktif

        open_positions = still_open

        # -----------------------------------------------------------------
        # 2. Cek sinyal entry — SELALU dievaluasi (tidak diblok posisi lain)
        #    Paksa bot_state.in_position = False agar strategy selalu
        #    mengembalikan sinyal entry jika prob > 0.60
        # -----------------------------------------------------------------
        strat.bot_state["in_position"] = False
        try:
            signal = strat.generate_signal(_build_data(closed, current))
        except Exception as e:
            signal = {"action": "hold", "reason": str(e)}

        if signal.get("action") == "buy":
            fee = modal_per_trade * FEE_RATE
            balance -= fee
            open_positions.append({
                "entry_price": close,
                "entry_time":  candle_time,
            })
            signals.append({"bar": t_str, "action": "BUY",
                             "entry": round(close, 5), "exit": None,
                             "pnl": None,
                             "reason": signal.get("reason", "")})

        # -----------------------------------------------------------------
        # 3. Hitung unrealized PnL semua posisi terbuka (mark-to-market)
        # -----------------------------------------------------------------
        unrealized = sum(
            modal_per_trade * (close - pos["entry_price"]) / pos["entry_price"]
            for pos in open_positions
        )
        equity_history.append({
            "bar":     t_str,
            "balance": round(balance, 4),
            "equity":  round(balance + unrealized, 4),
        })

    return signals, equity_history


# =============================================================================
# UI
# =============================================================================
st.markdown("## 🔬 Backtest — Simulasi Strategis")
st.divider()

# Mode selector
bt_mode = st.radio(
    "🔧 Mode Backtest",
    options=["🎯 Mode Colab (Original)", "📡 Mode Simulasi Live"],
    horizontal=True,
    key="bt_mode",
)
is_colab_mode = bt_mode.startswith("🎯")

if is_colab_mode:
    st.info(
        "**Mode Colab**: Mereplikasi backtest saat pelatihan model. "
        "Setiap sinyal adalah trade **independen** — outcome ditentukan dari `future_high` 4 candle ke depan. "
        "Hasilnya harus mendekati CSV `trade xrp.csv`.",
        icon="🎯"
    )
else:
    st.info(
        "**Mode Simulasi Live (Multi-Posisi)**: Bar-by-bar seperti bot live. "
        "Setiap candle **selalu dievaluasi** untuk entry baru — bisa banyak posisi terbuka sekaligus. "
        "Setiap posisi punya TP/SL/Time Stop sendiri. Modal per trade fixed.",
        icon="📡"
    )

st.divider()

c1, c2, c3, c4 = st.columns([2, 1.5, 1.5, 1])
with c1:
    bt_symbol = st.text_input("Symbol", value=SYMBOL, key="bt_sym")
with c2:
    bt_tf = st.selectbox(
        "Timeframe ⚠️ Model dilatih di 15m",
        list(_BYBIT_TF_MAP.keys()),
        index=list(_BYBIT_TF_MAP.keys()).index("15m"),
        key="bt_tf",
    )
with c3:
    bt_limit = st.slider("Jumlah Candle", 50, 1000, 1000, key="bt_lim")
with c4:
    bt_modal = st.number_input("Modal/Trade (USDT)", value=100.0,
                               min_value=1.0, step=10.0, key="bt_modal")

run_bt = st.button("▶️ Jalankan Backtest", type="primary", use_container_width=True)

if run_bt:
    with st.spinner(f"Mengambil {bt_limit} candle {bt_symbol} {bt_tf} dari Bybit..."):
        try:
            candles = fetch_backtest_candles(bt_symbol, bt_tf, bt_limit)
            st.success(
                f"✅ {len(candles)} candle dimuat  "
                f"({_ts(int(candles[0]['open_time']*1000))} → "
                f"{_ts(int(candles[-1]['open_time']*1000))})"
            )
        except Exception as e:
            st.error(f"Gagal ambil data: {e}")
            candles = []

    if candles:
        with st.spinner("Menjalankan simulasi strategi..."):
            try:
                if is_colab_mode:
                    signals, eq_hist = run_colab_mode(candles, bt_tf, bt_modal)
                    initial_ref = INITIAL_BALANCE
                else:
                    signals, eq_hist = run_live_mode(candles, bt_tf, INITIAL_BALANCE,
                                                     modal_per_trade=bt_modal)
                    initial_ref = INITIAL_BALANCE
            except Exception as e:
                st.error(f"Error saat backtest: {e}")
                signals, eq_hist = [], []
                initial_ref = INITIAL_BALANCE

        if eq_hist:
            eq_df = pd.DataFrame(eq_hist)

            # KPI
            final_eq     = eq_df["equity"].iloc[-1]
            peak         = eq_df["equity"].max()
            trough       = eq_df["equity"].min()
            ret_pct      = round((final_eq - initial_ref) / initial_ref * 100, 2)
            max_dd       = round((peak - trough) / peak * 100, 2) if peak > 0 else 0

            if is_colab_mode:
                closed_s  = signals  # semua sudah closed
                wins      = sum(1 for s in closed_s if s["action"] == "WIN (TP)")
            else:
                closed_s  = [s for s in signals if s["pnl"] is not None]
                wins      = sum(1 for s in closed_s if (s["pnl"] or 0) > 0)

            win_rate     = round(wins / len(closed_s) * 100, 1) if closed_s else 0.0
            total_pnl_bt = round(sum(s["pnl"] for s in closed_s), 4)

            k1, k2, k3, k4, k5 = st.columns(5)
            k1.metric("Total Trade", len(closed_s))
            k2.metric("Win Rate", f"{win_rate}%")
            k3.metric("Total PnL", f"{'+' if total_pnl_bt >= 0 else ''}{total_pnl_bt} USDT")
            k4.metric("Return", f"{'+' if ret_pct >= 0 else ''}{ret_pct}%")
            k5.metric("Max Drawdown", f"{max_dd}%")

            st.divider()
            st.markdown("#### 📈 Equity Curve Backtest")
            st.line_chart(eq_df.set_index("bar")[["equity", "balance"]], height=260)

            st.divider()
            st.markdown("#### 📋 Daftar Sinyal")
            sig_df = pd.DataFrame(signals)
            if not sig_df.empty:
                sig_df["pnl"] = pd.to_numeric(sig_df["pnl"], errors="coerce")
                st.dataframe(sig_df, hide_index=True, use_container_width=True)
        else:
            st.warning("Tidak ada data equity. Mungkin minimum candle belum cukup untuk strategi ini.")
