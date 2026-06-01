# PRD — Backtest CLI Mandiri + Refactor Hypertune

| | |
|---|---|
| **Status** | Draft (menunggu persetujuan untuk implementasi) |
| **Tanggal** | 2026-05-31 |
| **Penulis** | laburik (+ Claude) |
| **Ruang lingkup** | Tooling backtest & tuning. **TIDAK** menyentuh engine live (`execution.py`, `data_stream.py`, dll). |

---

## 1. Latar Belakang & Masalah

Saat ini ada **tiga notasi "sedang menguji apa"** yang terpisah dan bisa berbeda-sendiri (silent drift):

1. `config.py` → `SYMBOL`, `INITIAL_BALANCE`, `FEE_RATE`, `LEVERAGE` (dipakai bot live).
2. `tuning_config.py` → `TIMEFRAME`, `DAYS_BACK`, `PARAMS` (SYMBOL sudah ditarik dari config.py).
3. `engine/backtest.py` → **hardcode `SYMBOL="DOGEUSDT"`, `TF="2h"`** — lepas dari keduanya.

Masalah turunan:

- **`engine/backtest.py` CLI hanya signal-checker** — posisi di-mock `"none"`, **tidak menghitung PnL** ([engine/backtest.py:141](../engine/backtest.py#L141)). Tidak bisa menjawab "untung berapa".
- **Mesin backtest PnL yang sebenarnya terkubur di dua tempat:**
  - `pages/1_backtest.py` (Streamlit) — `run_live_mode()`/`run_colab_mode()`, terikat UI (`st.*`).
  - `hypertune.py` — monolit ~875 baris berisi mesin lengkap (`simulate_single`, `compute_metrics`, dll) tapi tidak bisa dipanggil mandiri.
- Tidak ada CLI backtest beneran (dengan PnL) di folder utama. User harus lewat Streamlit untuk metrik performa.

## 2. Tujuan & Non-Tujuan

### Tujuan
- **T1** — CLI backtest mandiri di **folder utama**: `python backtest.py`, selesai. Output metrik performa lengkap (teks).
- **T2** — **Satu mesin backtest bersama** dipakai oleh `backtest.py` **dan** `hypertune.py` → nol drift, hasil konsisten.
- **T3** — **Launcher tipis** di root; logika pindah ke `engine/`.
- **T4** — Config: parameter eksperimen jelas; instrumen/akun **single-source** di `config.py`.

### Non-Tujuan (eksplisit di luar scope)
- ❌ Migrasi ke package Python penuh (`__init__.py` + import `from engine...`). Proyek tetap **flat**.
- ❌ Migrasi ke NautilusTrader.
- ❌ Mengubah perilaku/logika simulasi yang sudah ada (ini refactor "pindah tempat", bukan ubah hasil).
- ❌ Menghapus/merombak `pages/1_backtest.py` Streamlit (boleh menyusul diarahkan ke core yang sama, terpisah dari PRD ini).

## 3. Kondisi Sekarang (terverifikasi)

**Arsitektur: FLAT.** Hanya `tests/__init__.py` yang ada; **`engine/` bukan package** — ditaruh di `sys.path`, modul saling import datar (`import config`, `import position_manager`).

**`hypertune.py` sudah berisi mesin backtest lengkap:**

| Fungsi | Baris | Peran |
|---|---|---|
| `download_candles`, `_try_load_cache`, `save_candles_csv` | [291](../hypertune.py#L291) | Data + cache CSV |
| `build_data_dict` | [434](../hypertune.py#L434) | Bentuk data utk `generate_signal()` |
| `load_strategy`, `patch_params`, `reset_bot_state`, `sync_strategy_state` | [197](../hypertune.py#L197) | Harness strategi |
| `simulate_single` (cermin live), `simulate_multi` | [454](../hypertune.py#L454) | **Simulator PnL** |
| `compute_metrics` | [670](../hypertune.py#L670) | **Metrik** |
| `expand_grid`, `run_grid_search`, `score_metrics`, `print_top_results` | [186](../hypertune.py#L186) | **Tuning-only** |

**`compute_metrics` sudah menghasilkan 5 dari 6 metrik wishlist:**

| Wishlist | Status di `compute_metrics` |
|---|---|
| PnL | ✅ `pnl`, `pnl_pct` |
| Profit factor | ✅ `profit_factor` |
| Drawdown | ✅ `max_dd_pct` |
| Jumlah trade | ✅ `num_trades` |
| Consecutive win/loss | ✅ `max_consec_wins`, `max_consec_losses` |
| **Total fee** | ❌ **belum di-surface** (fee sudah dihitung di simulator, belum diagregasi) |

## 4. Desain Solusi

### 4.1 Struktur File (split — pendekatan flat)

```
program bot utama/
├── backtest.py            ← BARU, TIPIS (launcher: baca config, panggil core)
├── hypertune.py           ← jadi TIPIS (launcher tuning)
├── config.py              ← sumber bersama: SYMBOL, INITIAL_BALANCE, FEE_RATE, LEVERAGE
├── backtest_config.py     ← BARU: TIMEFRAME, START, END
├── tuning_config.py       ← tetap: DAYS_BACK, OPTIM_MODE, POSITION_MODEL, PARAMS
└── engine/
    ├── backtest_data.py    ← BARU: download_candles, cache, build_data_dict, ccxt client
    ├── backtest_core.py    ← BARU: simulate_single/multi, harness strategi
    ├── backtest_metrics.py ← BARU: compute_metrics (+ total_fee), print_metric_block
    ├── tuning_core.py      ← BARU: expand_grid, run_grid_search, score_metrics
    └── (execution.py, data_stream.py, dll — TAK TERSENTUH)
```

Catatan: tetap flat (tanpa `__init__.py`). Root launcher menambahkan `engine/` ke `sys.path` lalu `import backtest_core` — sama persis pola `main.py`/`recap_run.py` sekarang.

### 4.2 Desain Config (single-source untuk yang dibagi)

| Setting | Pemilik | Alasan |
|---|---|---|
| `SYMBOL`, `INITIAL_BALANCE` (modal), `LEVERAGE`, `FEE_RATE` | **`config.py`** | Dibagi live + backtest + tuning → nol drift |
| `TIMEFRAME`, `START`, `END` | **`backtest_config.py`** | Range tetap, reproducible |
| `TIMEFRAME`, `DAYS_BACK`, `OPTIM_MODE`, `POSITION_MODEL`, `PARAMS` | **`tuning_config.py`** | Rolling window + train/test split + search space |

Backtest & tuning **mewarisi** instrumen/akun dari `config.py`; masing-masing hanya menulis parameter eksperimennya sendiri.

### 4.3 Spesifikasi `backtest.py` (CLI)

**Config (di `backtest_config.py`):**
```python
TIMEFRAME = "15m"
START     = "2026-01-01"   # mulai history (UTC)
END       = "end"          # "end" → data terbaru sampai sekarang
# SYMBOL, MODAL (INITIAL_BALANCE), LEVERAGE, FEE → dari config.py
```

**Cara jalan:** `python backtest.py`

**Output (teks):**
```
=== BACKTEST XRPUSDT 15m | 2026-01-01 → now (LEV 3x, modal 1000) ===
Net PnL         : +84.20 USDT (+8.42%)
Profit Factor   : 1.67
Max Drawdown    : -12.3%
Total Trades    : 142  (W 81 / L 61, win-rate 57.0%)
Max Consecutive : 7 win / 4 loss
Total Fee       : 11.36 USDT
winrate         : 50%
```

### 4.4 Penambahan yang Diperlukan

| # | Item | Detail |
|---|---|---|
| A1 | **Param START/END** di `download_candles` | Saat ini pakai `days_back`. Tambah dukungan rentang tanggal; `END="end"` → sampai candle terbaru (strip candle terbuka). |
| A2 | **Pagination Bybit kline** | Maks **1000 candle/request**. Range panjang perlu loop paging mundur sampai `START`. |
| A3 | **Wiring `LEVERAGE`** ke sizing `simulate_single` | Konfirmasi model sizing simulator; samakan dengan live (`ORDER_SIZE_USDT × LEVERAGE`). **Open question (Q1).** |
| A4 | **Surface `total_fee`** di `compute_metrics` | Agregasi fee dari trade-ledger; tambah ke dict hasil + `print_metric_block`. |

## 5. Kriteria Penerimaan

- **AC1** — `python backtest.py` dari folder utama berjalan tanpa atur PYTHONPATH manual, mencetak 6 metrik (PnL, profit factor, drawdown, jumlah trade, consecutive W/L, total fee).
- **AC2** — Ganti `SYMBOL` di `config.py` → backtest **dan** tuning ikut berubah; **tidak ada** lagi `DOGEUSDT` hardcoded.
- **AC3** — `python hypertune.py` menghasilkan output **identik** dengan sebelum refactor (param grid sama → hasil sama).
- **AC4** — `backtest.py` dan `hypertune.py` memanggil **fungsi simulator & metrik yang sama** (satu sumber).
- **AC5** — Root `backtest.py` dan `hypertune.py` masing-masing **launcher tipis** (≲ 15 baris logika).
- **AC6** — Engine live (`execution.py`, `data_stream.py`) + suite tes yang ada **tidak berubah & tetap hijau**.

## 6. Rencana Verifikasi (anti-regresi)

`hypertune.py` **tidak punya tes langsung**. Maka:

1. **Sebelum** refactor: jalankan `python hypertune.py` dengan `PARAMS` kecil deterministik → simpan output (baseline).
2. **Sesudah** refactor: jalankan ulang → **diff** harus identik.
3. Jalankan suite pytest yang ada → semua tetap hijau (memastikan engine live tak tersentuh).
4. Smoke-test `python backtest.py` di SYMBOL demo (XRPUSDT) → metrik masuk akal.

## 7. Risiko & Mitigasi

| Risiko | Mitigasi |
|---|---|
| Refactor mengubah hasil tuning diam-diam | Baseline diff sebelum/sesudah (§6) |
| Import flat pecah setelah pindah file | Pertahankan pola `sys.path.insert(engine/)`; uji import tiap modul baru |
| Pagination kline salah (gap/duplikat candle) | Sort + dedup by `open_time`; strip candle terbuka; uji jumlah candle vs rentang |
| Semantik leverage/sizing beda dari live | Selesaikan Q1 sebelum coding A3; samakan dengan `ORDER_SIZE_USDT × LEVERAGE` |

## 8. Urutan Kerja (fase)

1. **Fase 0** — Baca `simulate_single` tuntas; selesaikan Q1 (sizing/leverage). Ambil baseline hypertune (§6.1).
2. **Fase 1** — Ekstrak core: `backtest_data.py`, `backtest_core.py`, `backtest_metrics.py`. `hypertune.py` import dari sana. **Verifikasi diff identik (§6.2).**
3. **Fase 2** — Ekstrak `tuning_core.py`; ramping-kan root `hypertune.py` jadi launcher.
4. **Fase 3** — Tambah A4 (total_fee), A1+A2 (start/end + pagination), A3 (leverage).
5. **Fase 4** — Bangun root `backtest.py` + `backtest_config.py`. Smoke-test.
6. **Fase 5** — (opsional, terpisah) arahkan `pages/1_backtest.py` Streamlit ke core yang sama.

## 9. Open Questions

- **Q1** — Model sizing `simulate_single` saat ini: notional per trade dihitung bagaimana? Apakah sudah pakai leverage? Perlu dibaca sebelum wiring `LEVERAGE` (A3).
- **Q2** — `START/END` format: string tanggal (`"2026-01-01"`) saja, atau dukung epoch juga? Default: tanggal UTC.
- **Q3** — Kalau range `START→END` melebihi data tersedia exchange, perilaku: warn + pakai data terlama yang ada, atau error? Default usul: warn + lanjut.
