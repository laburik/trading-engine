# 🔬 Panduan Hyperparameter Tuning (`hypertune.py`)

Cari parameter strategi terbaik lewat **grid search** dengan validasi out-of-sample.
Semua diatur lewat `user/tuning_config.py` — **tidak ada CLI prompt**.

> Mesin simulasi & metrik **dibagi** dengan `backtest.py` (`engine/backtest_*.py`) →
> hasil tuning konsisten dengan backtest, **nol drift**. Biaya realistis (spread,
> slippage, funding) dari `config.py` ikut dihitung.

---

## Daftar Isi
1. [Quick Start](#quick-start)
2. [Anatomi `tuning_config.py`](#anatomi-tuning_configpy)
3. [Format `PARAMS`](#format-params)
4. [Mode optimasi & model posisi](#mode-optimasi--model-posisi)
5. [FAST_TUNING (percepatan)](#fast_tuning-percepatan)
6. [OOS_SPLIT (rasio split)](#oos_split-rasio-split)
7. [Grid multi-timeframe](#grid-multi-timeframe)
8. [Walk-forward (rolling window)](#walk-forward-rolling-window)
9. [Cara baca output](#cara-baca-output)
10. [Alur kerja yang disarankan](#alur-kerja-yang-disarankan)
11. [Contoh konfigurasi siap pakai](#contoh-konfigurasi-siap-pakai)
12. [Tips & jebakan](#tips--jebakan)
13. [Troubleshooting](#troubleshooting)

---

## Quick Start

1. **Deklarasikan parameter di atas file strategi** (gaya MQL5 input), mis. di `user/strategy.py`:
   ```python
   EMA_FAST  = 10
   SL_PCT    = 0.022
   TP_PCT    = 0.040
   ```
2. **Edit `user/tuning_config.py`** — set strategi, timeframe, rentang data, mode, dan `PARAMS`.
3. **Jalankan dari folder `user/`:**
   ```bash
   cd user
   python hypertune.py
   ```

`SYMBOL`, `INITIAL_BALANCE`, `LEVERAGE`, `FEE_RATE` diambil dari `config.py` (satu sumber).

---

## Anatomi `tuning_config.py`

| Setting | Arti | Default |
|---|---|---|
| `STRATEGY_FILE` | Nama file strategi tanpa `.py` (di folder `user/`). | `"strategy"` |
| `TIMEFRAME` | TF data **kalau `TUNE_TIMEFRAMES` kosong**. | `"15m"` |
| `TUNE_TIMEFRAMES` | List TF yang "diadu" (grid multi-TF). `[]` = pakai `TIMEFRAME`. | `[]` |
| `DAYS_BACK` | Berapa hari data OHLC ke belakang (dari sekarang). | `90` |
| `OPTIM_MODE` | `profit` \| `drawdown` \| `balanced`. | `"balanced"` |
| `POSITION_MODEL` | `single` (cermin bot live) \| `multi`. | `"single"` |
| `MAX_CACHE_AGE_MINUTES` | Pakai CSV cache di `data/` kalau lebih muda dari ini. `0` = selalu download. | `60` |
| `FAST_TUNING` | Percepat dgn windowing (lihat bawah). | `False` |
| `FAST_WINDOW` | Lebar window FAST_TUNING. `0` = auto. | `0` |
| `OOS_SPLIT` | Rasio split (int N): test = 1/N. | `5` (=80/20) |
| `WALK_FORWARD` | Mode rolling window. | `False` |
| `WF_WINDOW_DAYS` | Panjang tiap blok (train+test) saat walk-forward. | `20` |
| `WF_STEP_DAYS` | Geser jendela tiap iterasi (hari). | `5` |
| `PARAMS` | Grid parameter (key = nama variabel di strategy file). | — |

> ⚠️ Key di `PARAMS` **harus persis** sama dengan nama variabel di strategy file
> (case-sensitive). Kalau tidak, hypertune berhenti dengan pesan jelas (bukan
> diam-diam tidak ngefek).

---

## Format `PARAMS`

| Format | Contoh | Hasil |
|---|---|---|
| **Range `[start, end, N]`** | `[10, 30, 5]` | `[10, 15, 20, 25, 30]` (linspace N nilai) |
| **Range float** | `[0.005, 0.020, 4]` | `[0.005, 0.01, 0.015, 0.02]` (auto-float) |
| **Discrete (≠3 elemen)** | `[10, 15, 20, 25]` | dipakai apa adanya |
| **Discrete (bool/string)** | `[True, False]` / `["a","b"]` | dipakai apa adanya |

Auto-detect tipe: kalau `start` & `end` keduanya `int` → hasil int (dedup setelah
round). Kalau salah satu float → hasil float.

**Total kombinasi = perkalian semua list.** Parameter yang TIDAK ada di `PARAMS`
pakai nilai default dari strategy file.

```python
PARAMS = {
    "TREND_GAP": [0.0, 0.006, 3],    # [0.0, 0.003, 0.006]
    "SL_PCT":    [0.015, 0.025, 3],  # [0.015, 0.02, 0.025]
    "TP_PCT":    [0.03, 0.05, 3],    # [0.03, 0.04, 0.05]
}                                    # → 3 × 3 × 3 = 27 kombinasi
```

---

## Mode optimasi & model posisi

**`OPTIM_MODE`** — skor yang dimaksimalkan:

| Mode | Skor | Kapan |
|---|---|---|
| `profit` | Total PnL (USDT) | Kejar profit terbesar |
| `drawdown` | `−max_drawdown%` | Kejar DD terkecil |
| `balanced` | Sharpe ratio | **Direkomendasikan** (return/risk seimbang) |

> Kombinasi dengan **< 3 trade** otomatis dianggap tidak valid (skor `−inf`) →
> mencegah "menang karena 1 trade hoki".

**`POSITION_MODEL`:**

| Mode | Cara kerja | Cocok |
|---|---|---|
| `single` | 1 posisi; strategi handle entry+exit (`close`). | **Tuning realistis untuk live** |
| `multi` | Tiap `buy`/`sell` = trade independen; simulator handle SL/TP. | Analisa kualitas sinyal mentah |

> Bot live selalu **single-position** → pakai `single` untuk hasil yang akan dieksekusi live.

---

## FAST_TUNING (percepatan)

**Masalah:** strategi menghitung ulang indikator (EMA, dll) dari **seluruh history
tiap bar** → O(n²), lambat di TF kecil / data panjang.

**Solusi:** `FAST_TUNING=True` → engine cuma men-feed `FAST_WINDOW` candle **terakhir**
ke strategi tiap bar → O(n). Bisa **puluhan kali lebih cepat**.

```python
FAST_TUNING = True
FAST_WINDOW = 0   # 0 = AUTO (MIN_CANDLES strategi × 5, minimal 200)
```

- ✅ **Cocok**: indikator rolling/konvergen — EMA, SMA, RSI, ATR, Donchian,
  Bollinger, MACD. Hasil **praktis identik** full-history (selisih jauh di bawah noise).
- ❌ **TIDAK cocok** (set `False`): indikator yang butuh history sejak titik tertentu
  (anchored VWAP, statistik kumulatif) atau `strategy_ml` (biaya di inferensi model).
- 🔒 **Realisme backtest TIDAK terpengaruh** — slippage/spread/funding/eksekusi tetap
  utuh; yang diaproksimasi hanya nilai indikator.

> **Alur sehat:** tuning cepat (`True`) untuk cari kandidat → **kunci angka final**
> dengan `FAST_TUNING=False` atau lewat `backtest.py`.

---

## OOS_SPLIT (rasio split)

Integer **N**: porsi **test = 1/N**, **train = (N−1)/N**. Berlaku di mode biasa
maupun di tiap jendela walk-forward.

| `OOS_SPLIT` | Train / Test |
|---|---|
| `2` | 50% / 50% |
| `3` | 67% / 33% |
| `4` | 75% / 25% |
| `5` | 80% / 20% (default) |

---

## Grid multi-timeframe

Adu beberapa TF untuk cari yang paling cocok buat strategi ini:

```python
TUNE_TIMEFRAMES = ["5m", "15m", "1h"]
```

Grid `PARAMS` yang sama dijalankan di **tiap TF**, lalu engine cetak **leaderboard
antar-TF** (urut out-of-sample score) dengan penanda `<-- TF TERBAIK`.

> `[]` = nonaktif → pakai `TIMEFRAME` tunggal.

---

## Walk-forward (rolling window)

Menguji strategi di **banyak jendela bergulir**, bukan 1 split → jauh lebih jujur
& anti-hoki. Tiap jendela: **tune di train → uji di test berikutnya yang UNSEEN →
geser maju → ulang**. Ini cermin nyata cara operasi live ("tune di masa lalu,
trade ke depan, re-tune").

```python
WALK_FORWARD   = True
WF_WINDOW_DAYS = 20    # 1 blok = train + test
WF_STEP_DAYS   = 5     # geser tiap iterasi (= seberapa sering re-tune)
OOS_SPLIT      = 4     # → train 15 hari / test 5 hari (dari window 20)
```

**Rumus penting:**
- Train/test di tiap blok dibagi pakai `OOS_SPLIT`:
  `WF_WINDOW_DAYS=20` + `OOS_SPLIT=4` → test 5d, train 15d.
- Jumlah jendela ≈ `(DAYS_BACK − WF_WINDOW_DAYS) / WF_STEP_DAYS + 1`.
  Contoh `DAYS_BACK=120, window 20, step 5` → ~21 jendela.

**Beda penting dari mode biasa:** pemilihan param **hanya pakai skor in-sample**
(tidak mengintip test) → anti look-ahead.

> Kombinasikan dengan `FAST_TUNING=True` — walk-forward menjalankan grid berkali-kali
> (sekali per jendela), jadi kecepatan penting.

---

## Cara baca output

### Mode biasa (single-split)
Untuk **top 3** kombinasi: parameter + blok **In-Sample** & **Out-of-Sample**
(PnL, Trades, Win Rate, Max DD, Profit Factor, Sharpe). Urut by **OOS score**.
- Out-of-sample **jelek** padahal in-sample bagus = **overfitting** → jangan dipakai.
- Multi-TF → tambahan **leaderboard antar-TF**.

### Mode walk-forward
Tabel **per-jendela** + blok **DISTRIBUSI** out-of-sample:
```
DISTRIBUSI OUT-OF-SAMPLE (21 jendela):
  Jendela profit : 67%   (median PnL +0.40 | mean +0.35 | total +7.30 USDT)
  PnL terburuk   : -1.10   terbaik +2.05 USDT
  Max DD         : terburuk 18.1%   median 7.4%
  Verdict        : ROBUST
```
Verdict otomatis: **ROBUST** (%profit ≥ 60 & median > 0) · **RAPUH** (%profit < 50)
· **CAMPUR** (di antaranya). Multi-TF → tambahan **ringkasan walk-forward antar-TF**
(urut median PnL → "TF paling tahan").

**Patokan layak pakai:** %jendela profit ≥ 60%, median PnL > 0, **worst DD < 65%**,
verdict ROBUST.

> ⚠️ Walk-forward **TIDAK** memberi 1 set param untuk di-copy (tiap jendela punya
> param terbaiknya sendiri). Fungsinya **menjawab "strategi ini robust atau tidak"**.

---

## Alur kerja yang disarankan

```
1. WALK_FORWARD=True   → cek strategi ROBUST atau RAPUH (lampu hijau/merah)
                         (+ multi-TF untuk pilih TF terbaik)
        │ robust?
        ▼
2. WALK_FORWARD=False  → tuning biasa di DATA TERBARU → dapat 1 set param terbaik
        │
        ▼
3. Copy param ke strategy.py  (hypertune TIDAK mengubah file otomatis)
        │
        ▼
4. python backtest.py  → konfirmasi angka final (FAST_TUNING off / full)
        │
        ▼
5. Demo (MODE="demo") → baru Live
```

Singkatnya: **walk-forward = lampu hijau/merah**, **single-split = ambil angka finalnya**.

---

## Contoh konfigurasi siap pakai

### A. Cek robust dulu (walk-forward, 5m, train 15d / test 5d)
```python
TIMEFRAME       = "5m"
TUNE_TIMEFRAMES = []
DAYS_BACK       = 120
OPTIM_MODE      = "balanced"
POSITION_MODEL  = "single"
FAST_TUNING     = True
OOS_SPLIT       = 4
WALK_FORWARD    = True
WF_WINDOW_DAYS  = 20
WF_STEP_DAYS    = 5
PARAMS = {
    "TREND_GAP": [0.0, 0.004, 3],
    "SL_PCT":    [0.015, 0.025, 3],
    "TP_PCT":    [0.03, 0.05, 3],
}
```

### B. Cari TF terbaik (multi-TF, single-split)
```python
TUNE_TIMEFRAMES = ["5m", "15m", "1h"]
DAYS_BACK       = 120
FAST_TUNING     = True
WALK_FORWARD    = False
OOS_SPLIT       = 5
PARAMS = { ... }
```

### C. Ambil param final untuk deploy (single-split, data terbaru)
```python
TIMEFRAME       = "15m"
TUNE_TIMEFRAMES = []
DAYS_BACK       = 90
FAST_TUNING     = False   # full, untuk angka final
WALK_FORWARD    = False
OOS_SPLIT       = 5
PARAMS = { ... }          # grid sempit di sekitar kandidat
```

---

## Tips & jebakan

- **Jumlah jendela / trade**: walk-forward butuh banyak jendela (≥ ~10) dan tiap
  jendela cukup trade (≥ ~30) agar metrik bermakna. Kalau sedikit → perbesar `DAYS_BACK`.
- **Kecepatan**: total run ≈ `kombinasi × TF × jendela`. Pakai `FAST_TUNING` + grid
  yang wajar. Untuk eksplor luas, kecilkan `DAYS_BACK` dulu (pass kasar).
- **Overfitting**: PF > 3 dengan trade < 15 = hoki, bukan edge. Percaya distribusi
  walk-forward, bukan 1 angka in-sample.
- **Regime-specific**: strategi tren bisa rugi di pasar sideways — itu wajar. Tuning
  satu mekanisme tidak menciptakan edge di regime yang tak ada edge-nya.
- **Tidak ada constraint DD langsung**: `OPTIM_MODE` optimasi 1 skor. Untuk "profit
  asal DD < 65%", pakai `balanced` lalu **lihat kolom DD** di kandidat teratas.
- **Cache**: data tersimpan di `data/<exchange>_<SYMBOL>_<TF>_*.csv`. `MAX_CACHE_AGE_MINUTES`
  atur kapan dipakai ulang; `0` = selalu fresh.

---

## Troubleshooting

| Pesan / gejala | Sebab & solusi |
|---|---|
| `Parameter PARAMS ini tidak ada di strategy.py` | Nama key salah eja / beda huruf besar-kecil, atau variabel belum dideklarasi di atas strategy file. |
| `Tidak ada kombinasi yang valid (min 3 trade)` | Grid terlalu ketat / data sedikit → strategi jarang trade. Perluas range atau perbesar `DAYS_BACK`. |
| `Tidak ada TF dengan data cukup` | `DAYS_BACK` terlalu kecil untuk TF besar, atau (walk-forward) `WF_WINDOW_DAYS` > data. Perbesar `DAYS_BACK`. |
| Walk-forward cuma sedikit jendela | Perbesar `DAYS_BACK` atau perkecil `WF_STEP_DAYS`/`WF_WINDOW_DAYS`. |
| Tuning lambat | Aktifkan `FAST_TUNING=True`; kecilkan grid / `DAYS_BACK`. |
| Hasil bagus di tuning, jelek di live | Wajar sebagian — backtest dioptimasi ke data lampau. Validasi via walk-forward + demo dulu. |

---

## Hypertune vs Backtest

| | `hypertune.py` | `backtest.py` |
|---|---|---|
| Tujuan | Cari param terbaik (grid search) | Uji **satu** set param |
| Data | `DAYS_BACK` (relatif sekarang) | `START`/`END` eksplisit (`backtest_config.py`) |
| Split | In-sample/out-of-sample (`OOS_SPLIT`) atau walk-forward | Satu rentang penuh |
| Output | Top params / distribusi | Laporan metrik lengkap (+ HTML opsional) |

> Keduanya pakai **simulator & metrik yang sama** (`engine/backtest_*.py`) → konsisten.
```
