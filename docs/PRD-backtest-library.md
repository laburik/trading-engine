# PRD — Library Backtest Portable (Riset di Colab → Migrasi Mulus ke Live)

| | |
|---|---|
| **Status** | Draft (menunggu persetujuan untuk implementasi) |
| **Tanggal** | 2026-06-02 |
| **Penulis** | laburik (+ Claude) |
| **Ruang lingkup** | Pembungkusan mesin backtest jadi library importable. **TIDAK** mengubah logika simulasi & **TIDAK** menyentuh engine live (`execution.py`, `data_stream.py`, `position_manager.py`). |

---

## 1. Latar Belakang & Masalah

Riset strategi sekarang **terikat ke repo bot**: harus dijalankan dari struktur folder spesifik, dan `engine/backtest_core.py` **wajib bisa `import config`** (yang berisi setting akun + jalur API). Akibatnya:

- **Tidak bisa dipakai bersih di Google Colab / notebook.** Mau eksplorasi market + backtest cepat di Colab (compute gratis, plotting, iterasi cepat) tapi `import` mesin backtest gagal tanpa `config.py` lengkap.
- **Kopling ke config live.** [backtest_core.py:71](../engine/backtest_core.py#L71) → `from config import FEE_RATE, INITIAL_BALANCE, ORDER_SIZE_USDT, LEVERAGE`. Di Colab tidak ada `config.py` (apalagi API key).
- **Risiko drift bila disalin.** Kalau dibuat salinan terpisah untuk Colab, mesin backtest bisa berbeda-sendiri dari live — persis masalah duplikasi yang berulang (mis. definisi win-rate yang tersebar di 4 tempat).

Padahal fondasinya **sudah ada**: mesin backtest sudah dipecah ke `engine/` (PRD sebelumnya), kontrak strategi sudah bersih (`generate_signal(data)`), dan `pages/1_backtest.py` bahkan sudah punya `is_colab_mode`.

## 2. Tujuan & Non-Tujuan

### Tujuan
- **T1** — Mesin backtest bisa **`import`** & dipakai di Colab/notebook **tanpa `config.py` live, tanpa API key**.
- **T2** — **Migrasi kode nol-rewrite.** Strategi yang diriset di Colab = **byte-identik** dengan yang dijalankan live. Pindah ke live = copy file `.py` ke `user/` + set `STRATEGY_FILE`.
- **T3** — **Satu sumber kontrak.** Skema `data` (`ft_types`), interface strategi, dan rumus metrik didefinisikan **sekali**, dipakai library **dan** live → nol drift.
- **T4** — **Fleksibel sumber data.** Bisa fetch dari exchange, **atau** umpan `DataFrame`/CSV milik sendiri (riset offline).
- **T5** — **API publik ringkas** (≤ ~5 fungsi/kelas inti) yang jelas di-import.

### Non-Tujuan (eksplisit di luar scope)
- ❌ **Menjamin hasil backtest == live.** Itu masalah *fidelity* terpisah (fill/slippage/intra-candle). Library hanya menjamin **migrasi KODE** mulus, bukan kesamaan angka.
- ❌ Mengubah perilaku/logika simulasi (ini pembungkusan, bukan ubah hasil).
- ❌ Menyentuh engine live.
- ❌ Publish ke PyPI publik (cukup `pip install` dari git / copy folder untuk sekarang).
- ❌ Migrasi ke NautilusTrader / framework lain.

## 3. Kondisi Sekarang (terverifikasi)

**Mesin backtest sudah modular di `engine/`:** `backtest_core.py` (simulator + harness), `backtest_metrics.py` (metrik), `backtest_data.py` (download + cache), `tuning_core.py` (grid search). Dipakai bersama oleh `user/backtest.py` & `user/hypertune.py`.

**Kopling ke config live (penghalang utama portabilitas):**

| Lokasi | Isi | Masalah utk library |
|---|---|---|
| [backtest_core.py:71](../engine/backtest_core.py#L71) | `from config import FEE_RATE, INITIAL_BALANCE, ORDER_SIZE_USDT, LEVERAGE` | Wajib ada `config.py` live |
| [backtest_core.py:55-66](../engine/backtest_core.py#L55) | Pasang mock `execution`, `position_manager`, `bot_monitor` ke `sys.modules` | OK — tapi perlu jadi API publik library |

**Mock `position_manager` SUDAH setia** ([backtest_core.py:60-61](../engine/backtest_core.py#L60)): `get_position` & `get_pnl_summary` di-wire ke fungsi asli yang membaca state simulator — bukan `MagicMock` telanjang. Jadi strategi yang memakai `get_position()` untuk exit **benar-benar berfungsi** saat backtest. Ini aset yang harus dipertahankan.

**Kontrak strategi (stabil, jadi jaminan migrasi):**
- `generate_signal(data) -> {"action": "buy"|"sell"|"close"|"hold", "reason": str}` (+ `on_tick` opsional).
- `data` = `MarketDataSnapshot` (TypedDict di [engine/ft_types.py](../engine/ft_types.py)), dibangun `build_data_dict` ([backtest_core.py:145](../engine/backtest_core.py#L145)).

**Arsitektur live FLAT** (tanpa `__init__.py` kecuali `tests/`). Inilah ketegangan utama: library yang baik biasanya butuh *packaging*, tapi live ingin tetap flat.

## 4. Desain Solusi

### 4.1 Prinsip kunci: SATU sumber, dua pembungkus

Library **bukan salinan** mesin backtest — library **adalah** modul backtest yang sama, dibuat importable + bebas-config. Live tetap memakai modul yang sama. Tidak ada fork → tidak ada drift.

```
                ┌─────────────────────────────┐
   Colab/NB  ───►   botbacktest (package)      │  pip install / copy
                │   - API publik ringkas       │
                └──────────────┬──────────────┘
                               │  re-export (BUKAN copy)
                ┌──────────────▼──────────────┐
   Bot live  ───►   engine/backtest_core.py    │  modul yang SAMA
   (CLI)         │   engine/backtest_metrics.py │
                │   engine/ft_types.py (kontrak)│
                └─────────────────────────────┘
```

### 4.2 Decoupling config → `BacktestConfig`

Ganti `from config import ...` dengan **objek config yang dipassing eksplisit**:

```python
@dataclass
class BacktestConfig:
    symbol: str = "XRPUSDT"
    modal: float = 1000.0        # = INITIAL_BALANCE
    leverage: float = 3.0
    fee_rate: float = 0.0002
    order_size_usdt: float = 2.0
```

- **Library/Colab:** user bikin `BacktestConfig(...)` sendiri (tanpa `config.py`).
- **CLI live (`user/backtest.py`):** bangun `BacktestConfig` **dari** `config.py` → perilaku & output CLI **tidak berubah**.

Fungsi simulator/metrik menerima `cfg: BacktestConfig` sebagai argumen, bukan membaca global modul.

### 4.3 Struktur package (additive — live tetap flat)

```
program bot utama/
├── botbacktest/             ← BARU (package importable)
│   ├── __init__.py          ← API publik (lihat §4.4)
│   ├── _engine.py           ← jembatan import engine/backtest_* (single-source)
│   ├── loaders.py           ← from_dataframe / from_csv / fetch (opsional)
│   └── mocks.py             ← stub position_manager/execution/bot_monitor (publik)
├── pyproject.toml           ← BARU: bikin `pip install .` / `pip install git+...`
├── engine/                  ← TAK BERUBAH STRUKTUR (cuma config-decoupled)
└── user/                    ← TAK TERSENTUH
```

`botbacktest/__init__.py` me-re-export dari `engine/backtest_*` (modul yang sama yang dipakai live), **bukan** menyalinnya. `pyproject.toml` memasukkan `engine/backtest_*.py` + `ft_types.py` sebagai bagian paket.

### 4.4 API publik (target pemakaian)

**Riset di Colab:**
```python
!pip install git+https://github.com/<kamu>/<repo>
from botbacktest import BacktestConfig, run_backtest, compute_metrics, from_csv

cfg     = BacktestConfig(symbol="XRPUSDT", modal=1000, leverage=3, fee_rate=0.0002, order_size_usdt=2)
candles = from_csv("xrp_15m.csv")          # atau from_dataframe(df) / fetch(...)
result  = run_backtest("strategy_riset", candles, timeframe="15m", model="single", config=cfg)
metrics = compute_metrics(result, cfg)
print(metrics)                              # PnL, winrate, drawdown, profit factor, fee, ...
```

**Migrasi ke live (nol rewrite):**
```text
1. Copy strategy_riset.py  →  user/
2. Set di user/config.py:  STRATEGY_FILE = "strategy_riset"
3. python user/main.py     (preflight memvalidasi otomatis)
```
Strateginya **tidak diubah satu baris pun** — kontrak `generate_signal(data)` & skema `data` identik.

### 4.5 Fleksibilitas sumber data (`loaders.py`)
- `from_dataframe(df)` — kolom minimal: `open_time, open, high, low, close, volume`.
- `from_csv(path)` — wrapper `from_dataframe(pd.read_csv(...))`.
- `fetch(symbol, timeframe, start, end)` — pakai `backtest_data` (butuh jaringan; **extra opsional** supaya Colab ringan tanpa `ccxt` bila hanya pakai CSV).

## 5. Kriteria Penerimaan

- **AC1** — Di **virtualenv bersih tanpa `config.py`** (mensimulasikan Colab): `pip install .` lalu `from botbacktest import run_backtest` + jalankan backtest pada CSV contoh **berhasil**.
- **AC2** — **Migrasi nol-rewrite**: file strategi yang dipakai di Colab, di-copy ke `user/` + set `STRATEGY_FILE`, **lolos preflight** & jalan di `main.py` **tanpa diedit**.
- **AC3** — `run_backtest` menerima **`DataFrame`**, **CSV**, dan (opsional) **fetch**.
- **AC4** — **Tanpa regresi**: `python user/backtest.py` & `python user/hypertune.py` menghasilkan output **identik** dengan sebelum decoupling (param sama → hasil sama).
- **AC5** — **Single-source**: package me-re-export modul `engine/backtest_*` yang sama (bukan salinan). Skema `data` & rumus metrik terdefinisi **sekali**.
- **AC6** — Engine live + suite pytest yang ada **tidak berubah & tetap hijau**.

## 6. Rencana Verifikasi (anti-regresi)

1. **Baseline diff:** simpan output `user/backtest.py` & `user/hypertune.py` (param kecil deterministik) **sebelum** decoupling; **sesudah** → diff harus identik.
2. **Clean-venv import:** buat venv tanpa `config.py` di PATH, `pip install .`, jalankan backtest CSV → sukses (membuktikan T1).
3. **Migration test:** ambil `strategy_example.py`, jalankan via library, lalu copy ke `user/` + `STRATEGY_FILE` → preflight PASS + `main.py` jalan (membuktikan T2).
4. **Full pytest** tetap hijau (kecuali 9 sqz pre-existing yang sudah diketahui).

## 7. Risiko & Mitigasi

| Risiko | Mitigasi |
|---|---|
| Decoupling config mengubah hasil diam-diam | Baseline diff sebelum/sesudah (§6.1) |
| Package menyalin modul → drift dari live | **Single-source**: re-export, bukan copy; uji import dari satu path |
| Friksi flat↔package | Live tetap flat; package **additive** (wrapper), tidak mengubah `main.py` |
| Versi `pandas/numpy` di Colab beda | Pin dependensi minimal di `pyproject.toml`; uji di env bersih |
| Strategi impor modul live-only (`ccxt_client`, dll) | Sediakan stub di `mocks.py` + dokumentasikan modul yang aman dipakai |
| User kira "lolos backtest = siap live" | Dokumen tegas (Non-Tujuan): library jamin migrasi **kode**, bukan kesamaan **angka** |

## 8. Urutan Kerja (fase)

1. **Fase 0** — Inventaris semua pemakaian `config.*` di `engine/backtest_*`; tetapkan field `BacktestConfig`. Ambil baseline (§6.1).
2. **Fase 1** — Decouple config → `BacktestConfig` argumen. `user/backtest.py`/`hypertune.py` bangun config dari `config.py`. **Verifikasi diff identik.**
3. **Fase 2** — Tambah `loaders.py` (`from_dataframe`/`from_csv`; `fetch` opsional).
4. **Fase 3** — Bikin package `botbacktest/` (re-export + API publik) + `pyproject.toml` (installable).
5. **Fase 4** — Clean-venv import test + migration test (§6.2–6.3).
6. **Fase 5** — Docs: README section + **notebook Colab contoh** (riset → metrik → plot → migrasi).

## 9. Open Questions

- **Q1** — Package **wrapper tipis** (`botbacktest/` re-export `engine/`) **vs** jadikan `engine/` package penuh yang live ikut depend? Usul: **wrapper** (risiko ke live paling kecil).
- **Q2** — Sertakan `fetch` (CCXT/Bybit) di core library, atau jadikan **extra opsional** (`pip install botbacktest[fetch]`) supaya Colab ringan kalau hanya pakai CSV?
- **Q3** — Distribusi: `pip install git+...` dari repo **privat**, atau cukup **copy folder** ke Colab? (mempengaruhi kebutuhan auth di notebook)
- **Q4** — Sertakan **helper plot** (equity curve, drawdown) bawaan untuk Colab, atau biarkan user pakai matplotlib sendiri?
- **Q5** — Apakah `tuning_core` (grid search) ikut diekspos ke library, atau cukup `run_backtest` + `compute_metrics` dulu (tuning menyusul)?
