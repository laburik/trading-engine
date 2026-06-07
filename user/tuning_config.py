# =============================================================================
# tuning_config.py — Konfigurasi Tuning (Edit sebelum: python hypertune.py)
# =============================================================================
#
# Cara pakai:
#   1. Set STRATEGY_FILE ke nama file strategi tanpa .py (mis. "strategy").
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
#   EMA_FAST = 20
#   SL_PCT   = 0.025
STRATEGY_FILE = "strategy"


# =============================================================================
# 2. PENGATURAN BACKTEST
# =============================================================================
# Symbol diambil otomatis dari config.py → SYMBOL (tidak di sini).

# Timeframe data. WAJIB sama dengan TF yang dipakai strategy.py (= "15m").
# Tuning di TF lain akan meng-optimasi untuk TF itu, bukan yang dipakai live.
# Dipakai HANYA kalau TUNE_TIMEFRAMES (di bawah) kosong.
TIMEFRAME = "15m"

# Grid multi-timeframe: daftar TF yang mau "diadu" untuk cari TF paling cocok
# buat strategi ini. Grid PARAMS yang sama dijalankan di tiap TF, lalu engine
# cetak LEADERBOARD antar-TF (urut out-of-sample score) → ketahuan TF terbaik.
#   []                      → pakai TIMEFRAME tunggal di atas (perilaku lama).
#   ["15m", "1h", "2h"]     → adu 3 TF.
# Valid: 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d.
TUNE_TIMEFRAMES = []

# Berapa hari ke belakang data OHLC di-download dari Bybit.
# Catatan kecepatan: strategy menghitung EMA tiap bar → makin panjang DAYS_BACK
# dan makin kecil TIMEFRAME, makin lama per kombinasi. 90 hari 15m ≈ belasan
# detik/kombinasi. Kalau ingin grid besar, kecilkan DAYS_BACK dulu (mis. 60).
DAYS_BACK = 90

# Mode optimasi:
#   "profit"    → skor = total PnL (cari profit terbesar)
#   "drawdown"  → skor = -max drawdown (cari DD terkecil)
#   "balanced"  → skor = Sharpe ratio (cari return/risk terbaik)
# "balanced" disarankan: cari profit yang STABIL (return/risk) → DD ikut terjaga.
OPTIM_MODE = "balanced"

# Model posisi simulasi:
#   "single" → 1 posisi aktif (sama dengan bot live).
#              Strategy handle entry+exit (return "close" kalau mau exit).
#   "multi"  → setiap "buy"/"sell" = trade independen.
# strategy.py pakai exit sendiri (channel/SL/TP/trend-flip) → WAJIB "single".
POSITION_MODEL = "single"

# Cache age threshold (menit). Kalau CSV di folder data/ untuk SYMBOL+TIMEFRAME
# yang sama berusia < nilai ini, pakai cache tanpa download ulang.
# Set ke 0 untuk selalu force download.
MAX_CACHE_AGE_MINUTES = 60


# =============================================================================
# 2b. PERCEPATAN TUNING (windowing) — opsional
# =============================================================================
# Masalah: strategi menghitung ulang indikator (EMA, dll) dari SELURUH history
# tiap bar → makin panjang data makin lambat (O(n^2)). FAST_TUNING menyuruh
# engine cuma men-feed FAST_WINDOW candle TERAKHIR ke strategi tiap bar → tiap
# run jadi O(n) → grid besar / TF kecil jauh lebih cepat.
#
# PENTING:
#   - Realisme backtest (slippage/spread/funding/eksekusi) TIDAK terpengaruh —
#     ini cuma memangkas history yang "dilihat" strategi, bukan model fill.
#   - Yang BISA berubah cuma nilai indikator, tapi untuk indikator ROLLING/
#     KONVERGEN (EMA, SMA, RSI, ATR, Donchian, Bollinger, MACD) selisihnya jauh
#     di bawah noise → hasil praktis identik.
#   - TIDAK cocok (set False): indikator yang butuh history sejak titik tertentu
#     (anchored VWAP, statistik kumulatif) atau strategy_ml (biaya di inferensi
#     model, bukan di indikator).
#   - Alur sehat: tuning cepat (True) untuk cari kandidat → verifikasi angka
#     final dengan FAST_TUNING=False atau lewat backtest.py.
FAST_TUNING = False
# Jumlah candle terakhir yang di-feed saat FAST_TUNING=True.
#   0  → AUTO = MIN_CANDLES strategi × 5 (minimal 200). Aman untuk kebanyakan kasus.
#   >0 → manual (di-clamp minimal MIN_CANDLES). Patokan: ≥ lookback indikator
#        terpanjang × 5–10 (mis. EMA 200 → window ≥ 1000).
FAST_WINDOW = 0


# =============================================================================
# 2c. SPLIT in-sample/out-of-sample + WALK-FORWARD
# =============================================================================
# Rasio split (integer N): porsi TEST = 1/N, porsi TRAIN = (N-1)/N.
#   2 → 50/50    3 → 67/33    4 → 75/25    5 → 80/20 (default)
# Dipakai di mode biasa MAUPUN di tiap jendela walk-forward.
OOS_SPLIT = 5

# Walk-forward (rolling window) — uji strategi di BANYAK jendela bergulir, bukan
# 1 split. Tiap jendela: tune di train → uji di test berikutnya yang UNSEEN →
# geser maju → ulang. Hasil OOS dikumpulkan jadi DISTRIBUSI (% jendela profit,
# median, worst DD) → jauh lebih jujur & anti-hoki daripada 1 tes pendek.
# Ini cermin nyata cara operasi live: "tune di masa lalu, trade ke depan, re-tune".
#   False → 1 split tunggal (perilaku lama).
#   True  → rolling walk-forward.
WALK_FORWARD = False
WF_WINDOW_DAYS = 20    # panjang tiap blok (train+test) dalam hari
WF_STEP_DAYS   = 5     # geser jendela tiap iterasi (hari) = seberapa sering re-tune
# Contoh: WF_WINDOW_DAYS=20 + OOS_SPLIT=4 → train 15 hari / test 5 hari per blok.
# Pastikan DAYS_BACK >> WF_WINDOW_DAYS supaya dapat banyak jendela (mis. DAYS_BACK
# 90, window 20, step 5 → ~15 jendela). Kombinasikan dengan FAST_TUNING biar cepat.


# =============================================================================
# 3. PARAMETER GRID
# =============================================================================
# Key harus PERSIS sama dengan nama variabel di strategy.py (case-sensitive).
# Format:
#   A. RANGE → [start, end, iterasi]  (linspace inklusif; int→int, ada float→float)
#        "EXIT_N":  [8, 12, 3]          → [8, 10, 12]
#        "SL_PCT":  [0.020, 0.030, 3]   → [0.020, 0.025, 0.030]
#   B. DISCRETE → list nilai langsung dipakai (panjang ≠ 3 atau berisi bool/str)
#        "BREAKOUT_N": [36, 48, 60, 72]
#
# Parameter yang TIDAK ada di PARAMS → pakai default dari strategy.py.
# Total kombinasi = product semua list (jangan kebesaran — makan waktu).
#
# Grid di bawah (81 kombinasi) berpusat di nilai default strategy.py yang sudah
# dituning untuk profit maksimum di rezim sekarang. Ini untuk RE-TUNING saat
# regime pasar berubah. EMA_FAST(10), EMA_SLOW(30), BREAKOUT_N(36) dibiarkan
# default — tambahkan ke sini kalau mau ikut dituning (tiap baris mengalikan
# jumlah kombinasi).
PARAMS = {
    "TREND_GAP": [0.000, 0.006, 3],   # [0.000, 0.003, 0.006] — ketat = makin anti-sideways
    "EXIT_N":    [16, 24, 3],         # [16, 20, 24]          — turtle exit channel
    "SL_PCT":    [0.018, 0.026, 3],   # [0.018, 0.022, 0.026] — hard stop loss
    "TP_PCT":    [0.035, 0.045, 3],   # [0.035, 0.040, 0.045] — hard take profit

    # Contoh menambah param lain untuk dituning (akan memperbesar grid):
    # "EMA_FAST":   [8, 16, 3],        # [8, 12, 16]
    # "EMA_SLOW":   [25, 35, 3],       # [25, 30, 35]
    # "BREAKOUT_N": [24, 48, 3],       # [24, 36, 48]
}
