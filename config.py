#██████╗ ██╗███████╗██╗  ██╗██╗   ██╗    ██╗  ██╗███████╗███╗   ██╗ ██████╗ ██╗  ██╗███████╗██████╗ ██████╗ ██████╗  ██████╗ 
#██╔══██╗██║╚══███╔╝██║ ██╔╝╚██╗ ██╔╝    ██║  ██║██╔════╝████╗  ██║██╔════╝ ██║ ██╔╝██╔════╝██╔══██╗██╔══██╗██╔══██╗██╔═══██╗
#██████╔╝██║  ███╔╝ █████╔╝  ╚████╔╝     ███████║█████╗  ██╔██╗ ██║██║  ███╗█████╔╝ █████╗  ██████╔╝██████╔╝██████╔╝██║   ██║
#██╔══██╗██║ ███╔╝  ██╔═██╗   ╚██╔╝      ██╔══██║██╔══╝  ██║╚██╗██║██║   ██║██╔═██╗ ██╔══╝  ██╔══██╗██╔═══╝ ██╔══██╗██║   ██║
#██║  ██║██║███████╗██║  ██╗   ██║       ██║  ██║███████╗██║ ╚████║╚██████╔╝██║  ██╗███████╗██║  ██║██║     ██║  ██║╚██████╔╝
#╚═╝  ╚═╝╚═╝╚══════╝╚═╝  ╚═╝   ╚═╝       ╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝  ╚═╝ ╚═════╝ 
# if you copy, haram, unless you ask permission from the author
# for personal use only, if you use it for commercial purposes, you will be responsible for your own actions
import os
from dotenv import load_dotenv

load_dotenv()

# =============================================================================
# config.py — Central Configuration for Trading Bot System
# =============================================================================

# --- API Credentials (Live) ---
API_KEY    = os.getenv("BYBIT_LIVE_API_KEY", "")
API_SECRET = os.getenv("BYBIT_LIVE_API_SECRET", "")

# --- API Credentials (Demo) ---
# Daftar di: https://www.bybit.com/en/promo/global/demo-trading
# Lalu buat API Key di akun Demo kamu
DEMO_API_KEY    = os.getenv("BYBIT_DEMO_API_KEY", "")
DEMO_API_SECRET = os.getenv("BYBIT_DEMO_API_SECRET", "")

# --- Symbol & Market ---
SYMBOL = "XRPUSDT"
CATEGORY = "linear"  # "linear" for USDT-perp, "inverse" for coin-margined

# --- Trading Mode ---
# "paper" = simulasi lokal, tidak perlu API key
# "demo"  = Bybit Demo API, PnL dari Bybit (perlu DEMO_API_KEY)
# "live"  = Bybit Live API, uang nyata (perlu API_KEY)
MODE = "paper"

# --- Account ---
INITIAL_BALANCE = 1000.0       # Paper trading starting balance (USDT)
LEVERAGE = 10                  # Leverage multiplier
ORDER_SIZE_USDT = 50.0         # Size per trade in USDT (before leverage)

# --- Execution ---
SLIPPAGE_TOLERANCE = 0.0005    # 0.05% max slippage tolerance
MAX_RETRY = 3                  # Max order retry attempts
RETRY_DELAY_MS = 20           # Delay between retries in milliseconds
CANCEL_ON_PARTIAL = False      # True = cancel remainder on partial fill
FEE_RATE = 0.0002              # 0.02% per side (taker fee, applies on leveraged position size)


# --- Data Stream ---
ORDERBOOK_DEPTH = 50           # Valid Bybit linear depths: 1, 50, 200, 500
FUNDING_RATE_INTERVAL_SEC = 1200 # How often to poll funding rate (REST)

# Mode pengambilan data candle:
#   "kline" = Bybit native Kline WebSocket (RINGAN, candle instan, sinkron TV)
#   "tick"  = Tick-by-tick (BERAT, susun candle sendiri, data granular penuh)
DATA_MODE = "kline"

# --- Buffers ---
TICK_BUFFER_SIZE = 10000
ORDERBOOK_BUFFER_SIZE = 100

# --- Resampling Timeframes ---
# Format: "label": (interval_in_seconds, max_candles_in_buffer)
# Tambah atau hapus baris sesuai kebutuhan.
TIMEFRAMES = {
    #"1s":  (1,    500),   # 1 detik,  simpan 500 candle
    #"5s":  (5,    300),   # 5 detik,  simpan 300 candle. jgn dipakai tf < 1m kalau pake data mode kline
    "1m":  (60,   200),   # 1 menit,  simpan 200 candle
    # "5m":  (300,  100),   # 5 menit,  simpan 100 candle  ← contoh tambah
    "15m": (900,  50),    # 15 menit, simpan 50 candle   ← contoh tambah
    "2h":  (7200, 100),   # 2 jam,    simpan 100 candle   ← contoh tambah
}

# --- Heartbeat ---
HEARTBEAT_FILE = "heartbeat.json"
HEARTBEAT_INTERVAL_SEC = 1     # How often heartbeat updates
HEARTBEAT_TIMEOUT_SEC = 5      # Dashboard considers bot OFFLINE after this

# --- Logging ---
TRADE_HISTORY_FILE = "trade_history.csv"
EQUITY_CURVE_FILE = "equity_curve.csv"

# --- Bybit WebSocket URLs ---
WS_PUBLIC_URL    = "wss://stream.bybit.com/v5/public/linear"
REST_BASE_URL    = "https://api.bybit.com"
REST_TESTNET_URL = "https://api-testnet.bybit.com"
REST_DEMO_URL    = "https://api-demo.bybit.com"

# --- Demo/Live: PnL sync interval (detik) ---
# Seberapa sering bot fetch balance/posisi dari Bybit (mode demo/live)
PNL_SYNC_INTERVAL = 2

# Derived REST URL berdasarkan MODE
if MODE == "paper":
    REST_URL = REST_TESTNET_URL   # paper tidak pakai REST, tapi fallback ke testnet
elif MODE == "demo":
    REST_URL = REST_DEMO_URL
else:  # live
    REST_URL = REST_BASE_URL

# OLD-08 FIX: URL khusus untuk market data publik (kline, funding rate history).
# SELALU menggunakan live Bybit endpoint karena:
#   1. Testnet kline data terbatas/kosong untuk beberapa simbol
#   2. Data historis kline tidak butuh auth — aman diambil dari live
# Dengan konstanta ini, main.py tidak perlu hardcode REST_BASE_URL lagi.
REST_MARKET_URL = REST_BASE_URL

# Derived API credentials berdasarkan MODE
ACTIVE_API_KEY    = DEMO_API_KEY    if MODE == "demo" else API_KEY
ACTIVE_API_SECRET = DEMO_API_SECRET if MODE == "demo" else API_SECRET
