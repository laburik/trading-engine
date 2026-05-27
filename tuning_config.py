# =============================================================================
# tuning_config.py — Konfigurasi Tuning (Edit sebelum: python hypertune.py)
# =============================================================================
#
# Cara pakai:
#   1. Set STRATEGY_FILE ke nama file strategi tanpa .py (mis. "strategy1").
#   2. Set TIMEFRAME, DAYS_BACK, OPTIM_MODE, POSITION_MODEL.
#   3. Edit PARAMS — key harus sama dengan nama variabel di strategy file.
#   4. Jalankan: python hypertune.py
#
# Tidak ada CLI prompt — semua diatur di sini.
# =============================================================================

# =============================================================================
# 1. FILE STRATEGI
# =============================================================================
# Nama file strategi (tanpa .py). File harus ada di folder ini dan expose:
#   - generate_signal(data) -> {"action": "buy|sell|close|hold", "reason": "..."}
# Parameter strategi harus dideklarasi di atas file (gaya MQL5 input):
#   RSI_PERIOD = 14
#   STOP_LOSS_PCT = 0.008
STRATEGY_FILE = "strategy"


# =============================================================================
# 2. PENGATURAN BACKTEST
# =============================================================================
# Symbol diambil otomatis dari config.py → SYMBOL (tidak di sini).

# Timeframe data. Pilihan: 1m, 5m, 15m, 30m, 1h, 2h, 4h, 1d
TIMEFRAME = "2h"

# Berapa hari ke belakang data OHLC di-download dari Bybit.
DAYS_BACK = 90

# Mode optimasi:
#   "profit"    → skor = total PnL (cari profit terbesar)
#   "drawdown"  → skor = -max drawdown (cari DD terkecil)
#   "balanced"  → skor = Sharpe ratio (cari return/risk terbaik)
OPTIM_MODE = "balanced"

# Model posisi simulasi:
#   "single" → 1 posisi aktif (sama dengan bot live).
#              Strategy handle entry+exit (return "close" kalau mau exit).
#   "multi"  → setiap "buy"/"sell" = trade independen.
#              Simulator handle exit pakai STOP_LOSS_PCT/TAKE_PROFIT_PCT
#              (atau TARGET_SL_PCT/TARGET_TP_PCT) yang ada di strategy file.
POSITION_MODEL = "single"

# Cache age threshold (menit). Kalau CSV di folder data/ untuk SYMBOL+TIMEFRAME
# yang sama berusia < nilai ini, pakai cache tanpa download ulang.
# Set ke 0 untuk selalu force download.
MAX_CACHE_AGE_MINUTES = 60


# =============================================================================
# 3. PARAMETER GRID
# =============================================================================
# Setiap key harus PERSIS sama dengan nama variabel di file strategi
# (case-sensitive). Format dua macam:
#
#   A. RANGE → [start, end, iterasi]
#      Hasilkan `iterasi` nilai linspace inklusif start..end.
#      Auto-detect: kalau start & end keduanya int → hasil int (round + dedup).
#                   kalau salah satunya float → hasil float.
#      Contoh:
#        "RSI_PERIOD":     [10, 30, 5]        → [10, 15, 20, 25, 30]
#        "MA_PERIOD":      [10, 50, 9]        → [10, 15, 20, 25, 30, 35, 40, 45, 50]
#        "STOP_LOSS_PCT":  [0.005, 0.020, 4]  → [0.005, 0.01, 0.015, 0.02]
#
#   B. DISCRETE → list nilai langsung dipakai apa adanya.
#      Apa pun yang BUKAN format range (panjang ≠ 3, atau elemen bool/string,
#      atau iterasi bukan int positif) → dianggap discrete.
#      Contoh:
#        "USE_TRUE_RANGE":  [True, False]
#        "FILTER_MODE":     ["strict", "loose"]
#        "KC_LENGTH":       [10, 15, 20, 25]    (4 elemen → discrete)
#
# Parameter yang tidak ada di PARAMS akan pakai default dari strategy file.
# Total kombinasi = product semua list (jangan terlalu besar, makan waktu).
PARAMS = {
    # Contoh untuk strategy.py (Squeeze Momentum):
    "BB_LENGTH":       [15, 25, 3],            # [15, 20, 25]
    "KC_LENGTH":       [15, 25, 3],            # [15, 20, 25]
    "STOP_LOSS_PCT":   [0.005, 0.015, 3],      # [0.005, 0.01, 0.015]
    "TAKE_PROFIT_PCT": [0.010, 0.040, 4],      # [0.01, 0.02, 0.03, 0.04]

    # Tambah/hapus baris sesuai parameter di strategy file kamu.
    # Untuk strategy_ml.py contoh:
    # "TARGET_TP_PCT":   [0.003, 0.008, 4],
    # "TARGET_SL_PCT":   [0.003, 0.008, 4],
    # "TIME_STOP_SEC":   [1800, 3600, 7200],   (3 elemen tapi mau discrete? gunakan 4 elemen atau pakai format range)
}
