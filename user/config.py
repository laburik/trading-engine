#██████╗ ██╗███████╗██╗  ██╗██╗   ██╗    ██╗  ██╗███████╗███╗   ██╗ ██████╗ ██╗  ██╗███████╗██████╗ ██████╗ ██████╗  ██████╗ 
#██╔══██╗██║╚══███╔╝██║ ██╔╝╚██╗ ██╔╝    ██║  ██║██╔════╝████╗  ██║██╔════╝ ██║ ██╔╝██╔════╝██╔══██╗██╔══██╗██╔══██╗██╔═══██╗
#██████╔╝██║  ███╔╝ █████╔╝  ╚████╔╝     ███████║█████╗  ██╔██╗ ██║██║  ███╗█████╔╝ █████╗  ██████╔╝██████╔╝██████╔╝██║   ██║
#██╔══██╗██║ ███╔╝  ██╔═██╗   ╚██╔╝      ██╔══██║██╔══╝  ██║╚██╗██║██║   ██║██╔═██╗ ██╔══╝  ██╔══██╗██╔═══╝ ██╔══██╗██║   ██║
#██║  ██║██║███████╗██║  ██╗   ██║       ██║  ██║███████╗██║ ╚████║╚██████╔╝██║  ██╗███████╗██║  ██║██║     ██║  ██║╚██████╔╝
#╚═╝  ╚═╝╚═╝╚══════╝╚═╝  ╚═╝   ╚═╝       ╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝  ╚═╝ ╚═════╝ 
# if you copy, haram, unless you ask permission from the author
# for personal use only, if you use it for commercial purposes, you will be responsible for your own actions
import os
from typing import TypeGuard

from dotenv import load_dotenv

# config.py ada di user/, tapi .env tetap di project ROOT. Muat eksplisit dari
# root (robust terhadap cwd maupun lokasi file) supaya API key tetap kebaca.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

# =============================================================================
# config.py — Central Configuration for Trading Bot System
# =============================================================================

# --- Exchange ---
# Nama exchange (CCXT). Pilihan umum yang di-test: bybit, binance, okx, bitget.
# Lihat daftar lengkap: https://docs.ccxt.com/#/?id=exchanges
#
# Catatan SYMBOL: tulis sesuai format raw exchange yang dipilih.
#   - bybit, binance: "XRPUSDT"
#   - okx           : "XRP-USDT-SWAP"
#   - bitget        : "XRPUSDT_UMCBL"
EXCHANGE = "bybit"

# --- API Credentials (Live) ---
# Variabel env generik (rekomendasi): EXCHANGE_API_KEY / EXCHANGE_API_SECRET
# Fallback ke BYBIT_LIVE_* untuk kompatibilitas dengan setup .env lama.
API_KEY    = os.getenv("EXCHANGE_API_KEY",    os.getenv("BYBIT_LIVE_API_KEY", ""))
API_SECRET = os.getenv("EXCHANGE_API_SECRET", os.getenv("BYBIT_LIVE_API_SECRET", ""))

# --- API Credentials (Demo/Testnet) ---
# Bybit: daftar di https://www.bybit.com/en/promo/global/demo-trading
# Exchange lain: buat API key di testnet site exchange tersebut
DEMO_API_KEY    = os.getenv("EXCHANGE_DEMO_API_KEY",    os.getenv("BYBIT_DEMO_API_KEY", ""))
DEMO_API_SECRET = os.getenv("EXCHANGE_DEMO_API_SECRET", os.getenv("BYBIT_DEMO_API_SECRET", ""))

# --- Symbol & Market ---
SYMBOL = "XRPUSDT"
CATEGORY = "linear"  # "linear" for USDT-perp, "inverse" for coin-margined

# --- Trading Mode ---
# "paper" = simulasi lokal, tidak perlu API key
# "demo"  = simulasi pakai exchange API. Behavior tergantung EXCHANGE:
#             - bybit       : Bybit Demo (data live REAL + fill simulasi) — paling realistis
#             - exchange lain: testnet (data testnet + fill testnet) — kurang realistis
#                              karena harga testnet bisa drift dari market real
# "live"  = Live API, uang nyata (perlu API_KEY)
MODE = "demo"

# --- Strategy ---
# Nama file strategi yang dijalankan live bot (tanpa .py), diambil dari folder user/.
# Boleh punya banyak file strategi (mis. strategy.py, strategy_ml.py, strategy_sqz.py)
# — cukup arahkan ke SATU di sini, sisanya tidak dijalankan.
# Konvensi sama persis dengan STRATEGY_FILE di backtest_config.py & tuning_config.py.
STRATEGY_FILE = "strategy"

# --- Account ---
INITIAL_BALANCE = 1000.0       # Paper trading starting balance (USDT) (atur 1000, jika ingin pytest hijau)
LEVERAGE = 3                  # Leverage multiplier
ORDER_SIZE_USDT = 2       # Size per trade in USDT (before leverage) (atur 2 jika ingin pytest hijau)

# --- Execution ---
SLIPPAGE_TOLERANCE = 0.0005    # 0.05% max slippage tolerance
MAX_RETRY = 3                  # Max order retry attempts
RETRY_DELAY_MS = 20           # Delay between retries in milliseconds
CANCEL_ON_PARTIAL = False      # True = cancel remainder on partial fill
FEE_RATE = 0.0002              # 0.02% per side (taker fee, applies on leveraged position size)

# --- Backtest realism (HANYA dipakai mesin backtest; tidak memengaruhi live) ---
# Biaya yang sering luput di backtest → bikin hasil terlalu optimis. Set realistis.
BACKTEST_SLIPPAGE     = 0.0002   # 0.02% per fill (market order menelusuri book)
BACKTEST_HALF_SPREAD  = 0.0001   # 0.01% tiap sisi (beli di ask, jual di bid)
BACKTEST_FUNDING_RATE = 0.0001   # funding perp per 8 jam (drag posisi yang ditahan)


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

# --- Runtime files (auto-generated, disimpan di logs/) ---
# Folder logs/ otomatis dibuat di bawah supaya bot tidak crash saat write pertama.
LOGS_DIR = "logs"
os.makedirs(LOGS_DIR, exist_ok=True)

# --- Heartbeat ---
HEARTBEAT_FILE = os.path.join(LOGS_DIR, "heartbeat.json")
HEARTBEAT_INTERVAL_SEC = 1     # How often heartbeat updates
HEARTBEAT_TIMEOUT_SEC = 5      # Dashboard considers bot OFFLINE after this

# --- Logging ---
TRADE_HISTORY_FILE = os.path.join(LOGS_DIR, "trade_history.csv")
EQUITY_CURVE_FILE = os.path.join(LOGS_DIR, "equity_curve.csv")

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


# =============================================================================
# VALIDASI INPUT — cegah salah ketik / nilai tak masuk akal SEBELUM bot jalan
# =============================================================================
# Dijalankan otomatis saat config.py di-import (paling bawah file). Kalau ada
# nilai yang salah → program berhenti dengan daftar masalah yang JELAS, bukan
# error misterius jauh di dalam engine saat bot sudah jalan.
#
# Cakupan: HANYA nilai yang kamu edit di file ini. TIDAK memvalidasi API key
# (datang dari .env, bukan input di sini) dan TIDAK menyentuh backtest_config.py.
_VALID_DEPTHS_BYBIT_LINEAR = (1, 50, 200, 500)


def validate_config(cfg=None) -> list[str]:
    """Cek tipe/range/enum tiap nilai config. Return list pesan error (kosong = OK).

    `cfg` default = modul config ini sendiri; bisa dioper objek lain (mis.
    SimpleNamespace) untuk unit test tanpa mengubah file asli.
    """
    import sys as _sys
    if cfg is None:
        cfg = _sys.modules[__name__]

    errors: list[str] = []
    _MISSING = object()

    def is_num(x) -> TypeGuard[int | float]:
        return isinstance(x, (int, float)) and not isinstance(x, bool)

    def is_int(x) -> TypeGuard[int]:
        return isinstance(x, int) and not isinstance(x, bool)

    def get(name):
        v = getattr(cfg, name, _MISSING)
        if v is _MISSING:
            errors.append(f"{name}: hilang dari config.py (jangan dihapus barisnya).")
        return v

    def check_num(name, *, gt=None, ge=None, lt=None, le=None):
        v = get(name)
        if v is _MISSING:
            return
        if not is_num(v):
            errors.append(f"{name}: harus angka, dapat {type(v).__name__} ({v!r}).")
            return
        if gt is not None and not v > gt:
            errors.append(f"{name}: harus > {gt}, dapat {v}.")
        if ge is not None and not v >= ge:
            errors.append(f"{name}: harus >= {ge}, dapat {v}.")
        if lt is not None and not v < lt:
            errors.append(f"{name}: harus < {lt}, dapat {v}.")
        if le is not None and not v <= le:
            errors.append(f"{name}: harus <= {le}, dapat {v}.")

    def check_int(name, *, gt=None, ge=None):
        v = get(name)
        if v is _MISSING:
            return
        if not is_int(v):
            errors.append(f"{name}: harus integer, dapat {type(v).__name__} ({v!r}).")
            return
        if gt is not None and not v > gt:
            errors.append(f"{name}: harus > {gt}, dapat {v}.")
        if ge is not None and not v >= ge:
            errors.append(f"{name}: harus >= {ge}, dapat {v}.")

    def check_str(name):
        v = get(name)
        if v is _MISSING:
            return None
        if not isinstance(v, str) or not v.strip():
            errors.append(f"{name}: harus string non-empty, dapat {v!r}.")
            return None
        return v

    def check_choice(name, choices):
        v = get(name)
        if v is _MISSING:
            return
        if v not in choices:
            errors.append(f"{name}: harus salah satu {list(choices)}, dapat {v!r}.")

    def check_bool(name):
        v = get(name)
        if v is _MISSING:
            return
        if not isinstance(v, bool):
            errors.append(f"{name}: harus True/False (bool), dapat {type(v).__name__} ({v!r}).")

    # --- Exchange & market ---
    check_str("EXCHANGE")
    check_str("SYMBOL")
    check_choice("CATEGORY", ("linear", "inverse"))
    check_choice("MODE", ("paper", "demo", "live"))

    # --- Strategy ---
    sf = check_str("STRATEGY_FILE")
    if sf and sf.endswith(".py"):
        errors.append("STRATEGY_FILE: cukup nama modul tanpa '.py' (mis. 'strategy').")

    # --- Account ---
    check_num("INITIAL_BALANCE", gt=0)
    check_num("LEVERAGE", ge=1)
    check_num("ORDER_SIZE_USDT", gt=0)

    # --- Execution ---
    check_num("SLIPPAGE_TOLERANCE", ge=0, lt=1)
    check_int("MAX_RETRY", ge=0)
    check_num("RETRY_DELAY_MS", ge=0)
    check_bool("CANCEL_ON_PARTIAL")
    check_num("FEE_RATE", ge=0, lt=1)

    # --- Backtest realism (knob di config.py, bukan backtest_config.py) ---
    check_num("BACKTEST_SLIPPAGE", ge=0, lt=1)
    check_num("BACKTEST_HALF_SPREAD", ge=0, lt=1)
    check_num("BACKTEST_FUNDING_RATE", ge=0, lt=1)

    # --- Data stream ---
    check_int("ORDERBOOK_DEPTH", gt=0)
    depth = getattr(cfg, "ORDERBOOK_DEPTH", _MISSING)
    if (is_int(depth) and depth > 0
            and getattr(cfg, "EXCHANGE", "") == "bybit"
            and getattr(cfg, "CATEGORY", "") == "linear"
            and depth not in _VALID_DEPTHS_BYBIT_LINEAR):
        errors.append(
            f"ORDERBOOK_DEPTH: untuk bybit linear harus salah satu "
            f"{list(_VALID_DEPTHS_BYBIT_LINEAR)}, dapat {depth}."
        )
    check_num("FUNDING_RATE_INTERVAL_SEC", gt=0)
    check_choice("DATA_MODE", ("kline", "tick"))
    check_int("TICK_BUFFER_SIZE", gt=0)
    check_int("ORDERBOOK_BUFFER_SIZE", gt=0)

    # --- Timeframes ---
    tfs = getattr(cfg, "TIMEFRAMES", _MISSING)
    if tfs is _MISSING:
        errors.append("TIMEFRAMES: hilang dari config.py.")
    elif not isinstance(tfs, dict) or not tfs:
        errors.append("TIMEFRAMES: harus dict non-empty (mis. {'1m': (60, 200)}).")
    else:
        for label, spec in tfs.items():
            if not isinstance(label, str) or not label.strip():
                errors.append(f"TIMEFRAMES: label {label!r} harus string non-empty.")
                continue
            if not isinstance(spec, (tuple, list)) or len(spec) != 2:
                errors.append(
                    f"TIMEFRAMES['{label}']: harus (interval_detik, max_candle), dapat {spec!r}."
                )
                continue
            interval, maxc = spec
            if not is_num(interval) or interval <= 0:
                errors.append(
                    f"TIMEFRAMES['{label}']: interval_detik harus angka > 0, dapat {interval!r}."
                )
            if not is_int(maxc) or maxc <= 0:
                errors.append(
                    f"TIMEFRAMES['{label}']: max_candle harus integer > 0, dapat {maxc!r}."
                )

    # --- Heartbeat / logging / sync ---
    check_str("LOGS_DIR")
    check_num("HEARTBEAT_INTERVAL_SEC", gt=0)
    check_num("HEARTBEAT_TIMEOUT_SEC", gt=0)
    hi = getattr(cfg, "HEARTBEAT_INTERVAL_SEC", _MISSING)
    ht = getattr(cfg, "HEARTBEAT_TIMEOUT_SEC", _MISSING)
    if is_num(hi) and is_num(ht) and not ht > hi:
        errors.append(
            f"HEARTBEAT_TIMEOUT_SEC ({ht}) harus > HEARTBEAT_INTERVAL_SEC ({hi}) "
            "supaya dashboard tidak salah lapor bot OFFLINE."
        )
    check_num("PNL_SYNC_INTERVAL", gt=0)

    return errors


# Jalankan validasi saat config di-import. Kalau ada masalah → stop seketika.
_config_errors = validate_config()
if _config_errors:
    print("=" * 70)
    print("  ❌ CONFIG TIDAK VALID — perbaiki config.py sebelum lanjut:")
    print("=" * 70)
    for _i, _e in enumerate(_config_errors, 1):
        print(f"  {_i}. {_e}")
    print("=" * 70)
    raise SystemExit(1)
