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
# Default = awal tahun ini sampai sekarang → mewakili rezim pasar terkini
# (tempat strategy.py di-tuning untuk profit maksimum).
START = "2026-01-01"     # tanggal mulai
END   = "end"            # "end" → data terbaru sampai sekarang; atau "YYYY-MM-DD"

# File strategi yang di-backtest (tanpa .py). Default "strategy" = sama dgn live.
STRATEGY_FILE = "strategy"

# Model posisi simulasi:
#   "single" → 1 posisi, strategy handle entry+exit (cermin bot live).
#   "multi"  → tiap buy/sell trade independen; simulator handle SL/TP/TimeStop.
POSITION_MODEL = "single"

# Realisme eksekusi 1-menit (intrabar).
#   False (default) → cepat. Eksekusi (SL/TP/exit) dievaluasi di level candle
#                     TIMEFRAME. Cukup untuk TF kecil / cek lifecycle.
#   True            → unduh candle 1m untuk RANGE yang sama, lalu eksekusi
#                     ditelusuri per-1m DI DALAM tiap candle TIMEFRAME. SINYAL
#                     strategi TETAP di TIMEFRAME tinggi — candle-nya cuma
#                     "keliatan naik-turun" lewat sub-bar 1m. Lebih realistis
#                     (urutan SL-vs-TP benar, exit intrabar), lebih lambat +
#                     butuh unduh data 1m (di-cache kalau END tanggal tetap).
# Kapan perlu True: TF tinggi (15m/2h) DENGAN SL/TP yang bisa kena di tengah
# candle. Kalau TIMEFRAME sudah "1m", flag ini diabaikan (tak ada gunanya).
BACKTEST_REALISM = False

# Laporan HTML lengkap (riwayat trade, profit vs loss, drawdown, kurva ekuitas
# SVG, dll). True → generate file .html ke logs/backtest_data/ tiap run.
# Murni laporan — nol pengaruh ke hasil backtest. Bisa dibuka offline.
BACKTEST_HTML_REPORT = False
