# =============================================================================
# backtest_config.py — Pengaturan Backtest CLI (Edit sebelum: python backtest.py)
# =============================================================================
# Instrumen & akun (SYMBOL, modal=INITIAL_BALANCE, LEVERAGE, FEE_RATE) diambil
# dari config.py → satu sumber, dibagi dengan live bot & tuning (nol drift).
# Di sini HANYA parameter eksperimen backtest.
# =============================================================================

# Timeframe data. Pilihan: 1m, 5m, 15m, 30m, 1h, 2h, 4h, 1d
TIMEFRAME = "15m"

# Rentang history (UTC). Format 'YYYY-MM-DD'.
START = "2026-04-22"     # tanggal mulai
END   = "end"            # "end" → data terbaru sampai sekarang; atau "YYYY-MM-DD"

# File strategi yang di-backtest (tanpa .py). Default "strategy" = sama dgn live.
STRATEGY_FILE = "strategy"

# Model posisi simulasi:
#   "single" → 1 posisi, strategy handle entry+exit (cermin bot live).
#   "multi"  → tiap buy/sell trade independen; simulator handle SL/TP/TimeStop.
POSITION_MODEL = "single"
