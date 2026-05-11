#██████╗ ██╗███████╗██╗  ██╗██╗   ██╗    ██╗  ██╗███████╗███╗   ██╗ ██████╗ ██╗  ██╗███████╗██████╗ ██████╗ ██████╗  ██████╗ 
#██╔══██╗██║╚══███╔╝██║ ██╔╝╚██╗ ██╔╝    ██║  ██║██╔════╝████╗  ██║██╔════╝ ██║ ██╔╝██╔════╝██╔══██╗██╔══██╗██╔══██╗██╔═══██╗
#██████╔╝██║  ███╔╝ █████╔╝  ╚████╔╝     ███████║█████╗  ██╔██╗ ██║██║  ███╗█████╔╝ █████╗  ██████╔╝██████╔╝██████╔╝██║   ██║
#██╔══██╗██║ ███╔╝  ██╔═██╗   ╚██╔╝      ██╔══██║██╔══╝  ██║╚██╗██║██║   ██║██╔═██╗ ██╔══╝  ██╔══██╗██╔═══╝ ██╔══██╗██║   ██║
#██║  ██║██║███████╗██║  ██╗   ██║       ██║  ██║███████╗██║ ╚████║╚██████╔╝██║  ██╗███████╗██║  ██║██║     ██║  ██║╚██████╔╝
#╚═╝  ╚═╝╚═╝╚══════╝╚═╝  ╚═╝   ╚═╝       ╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝  ╚═╝ ╚═════╝                                                                                                                             
# if you copy, haram, unless you ask permission from the author
# for personal use only, if you use it for commercial purposes, you will be responsible for your own actions

import time
import json
import os
import sys
import pandas as pd
import streamlit as st

# Default config fallback
from config import HEARTBEAT_TIMEOUT_SEC

st.set_page_config(
    page_title="🤖 Master Dashboard",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    .main { background-color: #0e1117; }
    .pnl-positive { color: #69f0ae; font-size: 1.35rem; font-weight: bold; }
    .pnl-negative { color: #ff5252; font-size: 1.35rem; font-weight: bold; }
</style>
""", unsafe_allow_html=True)

# =============================================================================
# DYNAMIC PATH LOADERS
# =============================================================================
@st.cache_data(ttl=5)
def load_trade_history(path: str) -> pd.DataFrame:
    file = os.path.join(path, "trade_history.csv")
    if not os.path.exists(file):
        return pd.DataFrame()
    try:
        df = pd.read_csv(file)
        if "timestamp" in df.columns:
            df["time"] = pd.to_datetime(df["timestamp"], unit="s")
        return df
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=5)
def load_equity_curve(path: str) -> pd.DataFrame:
    file = os.path.join(path, "equity_curve.csv")
    if not os.path.exists(file):
        return pd.DataFrame()
    try:
        df = pd.read_csv(file)
        if "timestamp" in df.columns:
            df["time"] = pd.to_datetime(df["timestamp"], unit="s")
        return df.tail(5000)
    except Exception:
        return pd.DataFrame()

def read_heartbeat(path: str) -> dict:
    file = os.path.join(path, "heartbeat.json")
    try:
        if os.path.exists(file):
            with open(file, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {"last_update": 0}

def bot_is_online(heartbeat: dict) -> bool:
    last = heartbeat.get("last_update", 0)
    return (time.time() - last) < HEARTBEAT_TIMEOUT_SEC

@st.cache_data(ttl=5, show_spinner=False)
def load_health(path: str) -> dict:
    """Baca bot_health.json dari folder bot."""
    fpath = os.path.join(path, "bot_health.json")
    try:
        if os.path.exists(fpath):
            with open(fpath, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def calc_winrate(df: pd.DataFrame) -> float:
    closed = df[df["pnl"].notna() & (df["pnl"] != "")]
    if closed.empty:
        return 0.0
    closed = closed.copy()
    closed["pnl"] = pd.to_numeric(closed["pnl"], errors="coerce")
    winners = (closed["pnl"] > 0).sum()
    return round(winners / len(closed) * 100, 2)

# =============================================================================
# SIDEBAR - BOT FINDER
# =============================================================================
st.sidebar.markdown("## 📡 Multi-Bot Master")
st.sidebar.caption("Masukkan path folder bot Anda (1 path per baris). Contoh:`../Bot_BTC`")

default_paths = ".\n"
raw_paths = st.sidebar.text_area("Daftar Folder Bot:", value=default_paths, height=120)

valid_bots = {}
for p in raw_paths.strip().split("\n"):
    p = p.strip()
    if not p: continue
    
    if os.path.exists(os.path.join(p, "trade_history.csv")) or os.path.exists(os.path.join(p, "heartbeat.json")) or p == ".":
        if p == ".":
            name = os.path.basename(os.path.abspath(".")) + " (Current)"
        else:
            name = os.path.basename(os.path.abspath(p))
            if not name:
                name = p  # Fallback if path is just drive letter or pure root

        # Hindari duplikasi nama di dictionary
        original_name = name
        counter = 1
        while name in valid_bots and valid_bots[name] != p:
            name = f"{original_name} ({counter})"
            counter += 1
            
        valid_bots[name] = p

if not valid_bots:
    st.error("⚠️ Tidak ada path bot valid yang dimasukkan. Silakan cek sidebar.")
    st.stop()

selected_bot_name = st.sidebar.selectbox("🔎 Fokus Pantau Bot:", list(valid_bots.keys()))
active_path = valid_bots[selected_bot_name]

st.sidebar.divider()
st.sidebar.info("Halaman ini merangkum total eksekusi & profit dari seluruh bot, sekaligus memonitor pergerakan live sub-bot satu per satu.")


# =============================================================================
# MAIN TABS
# =============================================================================
tab_portfolio, tab_single = st.tabs(["💼 Portofolio Gabungan", "🔎 Detail Bot Terpilih"])

# -----------------------------------------------------------------------------
# TAB 1: PORTFOLIO GABUNGAN
# -----------------------------------------------------------------------------
with tab_portfolio:
    st.markdown("## 💼 Master Portofolio Multi-Coin")
    st.caption("Menjumlahkan kurva Equity dari seluruh bot aktif yang ditaruh di Sidebar.")
    
    all_equity = []
    total_balance_now = 0.0
    total_equity_now = 0.0
    total_pnl_now = 0.0

    for b_name, b_path in valid_bots.items():
        df = load_equity_curve(b_path)
        if not df.empty and "time" in df.columns:
            try:
                # Group by minute to align mis-matched timestamps from various bots
                df_resampled = df.set_index("time")[["equity", "balance"]].resample("1min").last().ffill()
                df_resampled.columns = [f"{b_name}_equity", f"{b_name}_balance"]
                all_equity.append(df_resampled)
                
                total_balance_now += df_resampled[f"{b_name}_balance"].iloc[-1]
                total_equity_now += df_resampled[f"{b_name}_equity"].iloc[-1]
                
                # Fetch Realized PnL
                trades = load_trade_history(b_path)
                if not trades.empty:
                    cl = trades[trades["pnl"].notna() & (trades["pnl"] != "")].copy()
                    cl["pnl"] = pd.to_numeric(cl["pnl"], errors="coerce").fillna(0)
                    total_pnl_now += cl["pnl"].sum()
            except Exception as e:
                pass

    if all_equity:
        portfolio_df = pd.concat(all_equity, axis=1).ffill().fillna(0)
        
        eq_cols = [c for c in portfolio_df.columns if c.endswith("_equity")]
        bal_cols = [c for c in portfolio_df.columns if c.endswith("_balance")]
        
        portfolio_df["Total Equity"] = portfolio_df[eq_cols].sum(axis=1)
        portfolio_df["Total Balance"] = portfolio_df[bal_cols].sum(axis=1)

        c1, c2, c3 = st.columns(3)
        c1.metric("Total Equity Gabungan", f"{total_equity_now:.2f} USDT", f"{(total_equity_now - total_balance_now):.2f} floating")
        c2.metric("Total Balance Modal", f"{total_balance_now:.2f} USDT")
        c3.metric("Total Realized PnL", f"{total_pnl_now:.2f} USDT")

        st.markdown("### 📈 Grafik Equity Gabungan All-Bot")
        st.line_chart(portfolio_df[["Total Equity", "Total Balance"]], height=350)
    else:
        st.info("Menunggu rekaman `equity_curve.csv` terisi terlebih dahulu...")

# -----------------------------------------------------------------------------
# TAB 2: SINGLE BOT MONITOR
# -----------------------------------------------------------------------------
with tab_single:
    st.markdown(f"## 📊 Live Monitor: `{selected_bot_name}`")
    st.divider()

    heartbeat = read_heartbeat(active_path)
    online    = bot_is_online(heartbeat)
    trade_df  = load_trade_history(active_path)
    equity_df = load_equity_curve(active_path)

    col_status, col_pnl, col_equity, col_winrate, col_trades = st.columns([1.5, 1.5, 1.5, 1, 1])

    with col_status:
        last_hb = heartbeat.get("last_update", 0)
        age = round(time.time() - last_hb, 1) if last_hb else "—"
        if online:
            st.success(f"🟢 BOT ONLINE  ·  {age}s ago")
        else:
            st.error(f"🔴 BOT OFFLINE  ·  last seen {age}s ago")
            
        last_prices = heartbeat.get("last_prices", {})
        if last_prices:
            # Display primary timeframe price (first key)
            tf_label = list(last_prices.keys())[0]
            st.markdown(f"**Harga Terakhir ({tf_label}):** `{last_prices[tf_label]}`")

    current_equity   = equity_df["equity"].iloc[-1] if not equity_df.empty else 0.0
    current_balance  = equity_df["balance"].iloc[-1] if not equity_df.empty else 0.0
    unrealized       = equity_df["unrealized_pnl"].iloc[-1] if not equity_df.empty else 0.0

    closed_trades = trade_df[trade_df["pnl"].notna() & (trade_df["pnl"] != "")].copy() if not trade_df.empty else pd.DataFrame()
    total_pnl = 0.0
    if not closed_trades.empty:
        closed_trades["pnl"] = pd.to_numeric(closed_trades["pnl"], errors="coerce").fillna(0)
        total_pnl = round(closed_trades["pnl"].sum(), 4)

    winrate      = calc_winrate(trade_df) if not trade_df.empty else 0.0
    total_trades = len(closed_trades) if not closed_trades.empty else 0

    with col_pnl:
        color = "pnl-positive" if total_pnl >= 0 else "pnl-negative"
        sign  = "+" if total_pnl >= 0 else ""
        st.markdown("**Total PnL**")
        st.markdown(f"<span class='{color}'>{sign}{total_pnl} USDT</span>", unsafe_allow_html=True)

    with col_equity:
        st.metric("Equity", f"{round(current_equity, 4)} USDT",
                  delta=f"{round(unrealized, 4)} unrealized")

    with col_winrate:
        st.metric("Win Rate", f"{winrate}%")

    with col_trades:
        st.metric("Total Trades", str(total_trades))

    # ─────────────────────────────────────────────────────────────────────
    # HEALTH MONITOR PANEL
    # ─────────────────────────────────────────────────────────────────────
    health = load_health(active_path)
    st.divider()
    st.markdown("### 🩺 Strategy Health Monitor")

    if not health:
        st.info("⏳ Menunggu data health... Bot belum pernah memanggil strategy, atau `bot_health.json` belum dibuat.")
    else:
        # Status utama
        status       = health.get("status", "UNKNOWN")
        status_reason = health.get("status_reason", "")
        COLORS = {
            "OK":      ("🟢", "success"),
            "WARN":    ("🟡", "warning"),
            "ERROR":   ("🔴", "error"),
            "FROZEN":  ("🔴", "error"),
            "STARTING":("⚪", "info"),
            "UNKNOWN": ("⚫", "info"),
        }
        icon, alert_type = COLORS.get(status, ("⚫", "info"))
        msg = f"{icon} **Strategy Status: {status}**"
        if status_reason:
            msg += f"  ·  {status_reason}"
        getattr(st, alert_type)(msg)

        # Row 1: metrik waktu
        h1, h2, h3, h4 = st.columns(4)
        last_tick  = health.get("last_tick_sec_ago")
        last_trade = health.get("last_trade_sec_ago")
        uptime     = health.get("uptime_sec", 0)
        total_tick = health.get("total_ticks", 0)

        def _fmt_sec(sec):
            if sec is None: return "—"
            if sec < 60:   return f"{sec}s lalu"
            if sec < 3600: return f"{sec//60}m {sec%60}s lalu"
            return f"{sec//3600}j {(sec%3600)//60}m lalu"

        h1.metric("Last Tick",  _fmt_sec(last_tick))
        h2.metric("Last Trade", _fmt_sec(last_trade))
        h3.metric("Uptime",     _fmt_sec(uptime))
        h4.metric("Total Ticks", f"{total_tick:,}")

        st.divider()

        # Row 2: distribusi sinyal + error
        counts = health.get("signal_counts", {})
        consec_err  = health.get("consecutive_errors", 0)
        total_err   = health.get("total_errors", 0)
        last_err    = health.get("last_error_reason", "")

        sc1, sc2, sc3, sc4, sc5 = st.columns(5)
        sc1.metric("🟢 BUY",   counts.get("buy",   0))
        sc2.metric("🔴 SELL",  counts.get("sell",  0))
        sc3.metric("⬜ CLOSE", counts.get("close", 0))
        sc4.metric("⏸️ HOLD",  counts.get("hold",  0))

        err_label = f"⚠️ {counts.get('error', 0)} (berturut: {consec_err})"
        sc5.metric("❌ ERROR", err_label,
                   delta="‼️ Perlu Perhatian" if consec_err >= 3 else None,
                   delta_color="inverse" if consec_err >= 3 else "normal")

        if last_err:
            st.caption(f"Error terakhir: `{last_err}`")

        # Row 3: ML probability (hanya jika ada)
        last_prob = health.get("last_ml_prob")
        avg_prob  = health.get("avg_ml_prob_20")
        if last_prob is not None:
            st.divider()
            p1, p2, p3 = st.columns(3)
            p1.metric("ML Prob Terakhir", f"{last_prob:.2%}")
            p2.metric("Avg ML Prob (20 tick)", f"{avg_prob:.2%}" if avg_prob else "—")
            entry_thresh = 0.60
            p3.metric("Jarak ke Threshold (60%)",
                      f"{(last_prob - entry_thresh)*100:+.1f}%",
                      delta="BUY Zone" if last_prob > entry_thresh else "HOLD Zone")

        # Row 4: Data Quality (candle buffer + bid/ask)
        dq = health.get("data_quality", {})
        if dq:
            st.divider()
            st.markdown("**📡 Kualitas Data Real-Time**")

            data_issues = dq.get("data_issues", [])
            if data_issues:
                for issue in data_issues:
                    st.warning(f"⚠️ {issue}")
            else:
                st.success("✅ Semua data valid — candle, bid, ask normal")

            # Candle buffer per timeframe
            candles_tf  = dq.get("candles_per_tf", {})
            candle_ages = dq.get("last_candle_age_sec", {})
            bid_price   = dq.get("bid_price", 0)
            ask_price   = dq.get("ask_price", 0)
            bid_valid   = dq.get("bid_valid", False)
            ask_valid   = dq.get("ask_valid", False)
            spread_pct  = dq.get("spread_pct", 0.0)

            # Baris candle per timeframe
            if candles_tf:
                tf_cols = st.columns(len(candles_tf) + 2)
                for idx, (tf, count) in enumerate(candles_tf.items()):
                    age = candle_ages.get(tf)
                    age_str = f"{age}s lalu" if age is not None else "—"
                    tf_cols[idx].metric(
                        f"📊 Candle {tf}",
                        f"{count} bar",
                        delta=age_str,
                        delta_color="normal" if (age or 0) < 1800 else "inverse"
                    )

                # Bid/Ask
                tf_cols[-2].metric(
                    "📈 Bid / Ask",
                    f"{bid_price} / {ask_price}",
                    delta="✅ Valid" if (bid_valid and ask_valid) else "❌ Tidak valid",
                    delta_color="normal" if (bid_valid and ask_valid) else "inverse"
                )
                tf_cols[-1].metric(
                    "↔️ Spread",
                    f"{spread_pct:.4f}%",
                    delta="Normal" if spread_pct < 0.1 else "⚠️ Lebar",
                    delta_color="normal" if spread_pct < 0.1 else "inverse"
                )

    # ─────────────────────────────────────────────────────────────────────

    st.divider()
    st.markdown("### 📈 Equity Curve Individual")
    if not equity_df.empty and "time" in equity_df.columns:
        chart_df = equity_df[["time", "equity", "balance"]].set_index("time")
        st.line_chart(chart_df, width='stretch', height=280)
    else:
        st.info("No equity data yet. Start the bot to see the curve.")

    st.divider()
    st.markdown("### 📋 Trade History")
    if not trade_df.empty:
        display_df = trade_df.copy()
        if "time" in display_df.columns:
            display_df = display_df.drop(columns=["timestamp"], errors="ignore")
        st.dataframe(
            display_df.sort_values("time", ascending=False).head(100),
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("No trades recorded yet.")

# =============================================================================
# AUTO-REFRESH MASTER (Trigger global)
# =============================================================================
time.sleep(5)
st.rerun()
