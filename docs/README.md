# Trading Bot System

Sistem bot trading berbasis Python yang modular, real-time, dan production-ready untuk Bybit Perpetual Futures (CCXT Pro). Kode infrastruktur (engine) terpisah dari kode user (strategi) — **kamu cukup edit `strategy.py`.**

---

## Arsitektur

```
Bybit (WebSocket / REST, via CCXT Pro)
        │
        ▼
  engine/data_stream.py     ← OrderBook + Funding Rate + Heartbeat
        │                     (dua timestamp: ts_event exchange / ts_init terima)
        ├─[DATA_MODE="tick"]──► engine/data_resampler.py  ← Susun candle dari tick mentah
        │
        └─[DATA_MODE="kline"]─► engine/candle_stream.py   ← Candle instan Bybit Kline WS
                                        │
                                        ▼
                               user/strategy.py            ←  USER EDIT (default; ganti via STRATEGY_FILE)
                                        │
                                        ▼
                          engine/strategy_runtime.py       ← Orkestrasi: signal → record → execute
                                        │
                                        ▼
                          engine/execution.py              ← Order (paper/live)
                                        │                    + in-flight guard + optimistic state
                                        │                    + precision (Decimal) + stale-data check
                                        ▼
                          engine/position_manager.py       ← Realtime PnL + sync Bybit
                                        │
                                        ▼
                          engine/trade_logger.py           ← CSV logging (trades + equity)
                                        │
                                        ▼
                               user/dashboard.py           ← Streamlit monitoring
```

> File user-facing ada di folder **`user/`** (`main.py`, `config.py`, `strategy.py`, dst);
> modul infrastruktur di **`engine/`**. `user/main.py` otomatis menambahkan project root +
> `engine/` + `user/` ke `sys.path`, jadi import tetap flat (`import execution`,
> `import position_manager`, dst).

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

> Python **3.12+** disarankan (diuji di 3.14).

### 2. Configure

Buat file `.env` di folder utama dan isi kunci API Anda. Didukung dua penamaan
(generik `EXCHANGE_*` direkomendasikan; `BYBIT_*` tetap kompatibel):

```env
# Generik (rekomendasi)
EXCHANGE_API_KEY="api_key_live_kamu"
EXCHANGE_API_SECRET="secret_live_kamu"
EXCHANGE_DEMO_API_KEY="api_key_demo_kamu"
EXCHANGE_DEMO_API_SECRET="secret_demo_kamu"

# (Fallback lama — masih dibaca)
# BYBIT_LIVE_API_KEY / BYBIT_LIVE_API_SECRET
# BYBIT_DEMO_API_KEY / BYBIT_DEMO_API_SECRET
```

Lalu edit konfigurasi di `config.py`:

```python
EXCHANGE = "bybit"      # bybit, binance, okx, bitget, ...
SYMBOL   = "XRPUSDT"
MODE     = "demo"       # "paper" | "demo" | "live"

# Strategi yang dijalankan live bot — nama file di user/ tanpa .py.
# Boleh punya banyak file strategi; cukup arahkan SATU di sini.
STRATEGY_FILE = "strategy"   # mis. "strategy" atau "strategy_ml"

# Mode pengambilan data candle:
#   "kline" = Bybit native Kline WebSocket (RINGAN, sinkron TradingView)
#   "tick"  = Tick-by-tick resampler (BERAT, data granular penuh)
DATA_MODE = "kline"

# Resampling timeframe — edit di sini, otomatis berlaku di seluruh sistem
TIMEFRAMES = {
    "1m":  (60,  200),   # 1 menit,  simpan 200 candle
    "15m": (900,  50),   # 15 menit, simpan 50 candle
    # "1h": (3600, 30),  # aktifkan dengan hapus tanda #
}
```

### 3. Jalankan test suite (opsional, cepat)

```bash
pip install pytest pytest-cov pytest-asyncio   # sekali install saja
python -m pytest                               # ~376 test
python -m pytest --cov                         # plus coverage report
```

Test cover: **PnL/money math, signal generation, engine orchestration, monitoring,
order execution, precision, stale-data, + stress/concurrency (race & lockout) engine.**
Berguna setelah refactor untuk pastikan tidak ada regresi.

> Catatan: 9 test di `tests/test_strategy_sqz.py` saat ini merah karena `strategy.py`
> aktif adalah varian MA-crossover (bukan Squeeze) — utang teknis lama, tidak terkait engine.

### 4. Validasi strategy.py

Pre-flight check **berjalan otomatis** saat `python user/main.py` startup (dan saat klik
**▶️ Start Bot** di Control Panel) — tidak perlu dijalankan manual. Menjalankan pengecekan
terhadap strategi yang dipilih (`STRATEGY_FILE` di `config.py`, default `strategy.py`), dua level:

| Level | Contoh | Efek |
|---|---|---|
| **Error Fatal** | Syntax error, `on_tick()` tidak ada, `time.sleep()` di generate_signal | Bot **DIBLOKIR** |
| **Saran** | HTTP request tiap tick, file I/O, data parsial | Bot **tetap jalan**, hanya diingatkan |

> Validasi ini juga tersedia di halaman **⚙️ Control Panel** — klik **▶️ Start Bot**, sistem
> memvalidasi otomatis sebelum bot dinyalakan.

### 5. Run the bot

```bash
python user/main.py
```

> `user/main.py` otomatis menjalankan pre-flight check saat startup. **Error fatal** → bot berhenti +
> tampilkan pesan. **Saran (warning)** → ditampilkan tapi tidak menghentikan bot.

### 6. Run the dashboard (terminal terpisah)

```bash
python -m streamlit run user/dashboard.py
```

Buka: `http://localhost:8501`

### 🎩 Memantau Banyak Bot Sekaligus (Master Dashboard)
Jika Anda men-duplikasi folder bot untuk strategi/koin berbeda (mis. `Bot_DOGE`, `Bot_BTC`),
Anda **TIDAK PERLU** menjalankan banyak dashboard. Cukup buka 1 halaman Streamlit, lalu pada
kolom teks **Sidebar** masukkan path folder-folder bot (tiap path beda baris). Aplikasi membaca
rekaman mereka serentak dan menggambar **1 Portofolio Gabungan**.

## 🔀 Perbandingan Mode

| | Paper | Demo | Live |
|---|---|---|---|
| API Key | Tidak perlu | DEMO key | LIVE key |
| Order | Simulasi lokal | Masuk server Bybit Demo | Uang nyata |
| PnL/Balance | Hitung lokal | Fetch dari Bybit tiap 2s | Fetch dari Bybit tiap 2s |
| Reset saldo | Restart bot | UI Bybit Demo | - |
| Cocok untuk | Dev & debug strategi | Validasi eksekusi API | Trading nyata |

### Setup Mode Demo
1. Login Bybit → **Trade → Demo Trading**
2. Buat API Key: **Account → API Management** (System-generated)
3. Isi `.env` (`EXCHANGE_DEMO_API_KEY` / `EXCHANGE_DEMO_API_SECRET`) dan set `MODE = "demo"` di `config.py`.

---

## 🛡️ Reliability & Engine Hardening

Engine sudah melewati perbaikan korektnes + pengujian di luar happy-path. Bagian ini
mendokumentasikan apa yang menjaga engine tetap konsisten (terutama di demo/live).

### Guard eksekusi
- **In-flight order guard** — hanya 1 order live in-flight pada satu waktu. Mencegah
  *duplicate-fire*: tick 100ms berikutnya tidak men-dispatch order kembar saat state
  belum sync. (Bug nyata yang dulu bikin posisi menumpuk 2×.)
- **Optimistic state update** — setelah fill terkonfirmasi, `position_manager` di-update
  SEKARANG (bukan nunggu sync ~2s), menutup window di mana strategi bisa fire ulang.
  Akunting uang tetap milik sync loop dari data exchange (sumber kebenaran).
- **No-retry pada desync `110017`** — kalau exchange bilang posisi sudah zero, engine
  clear state lokal & tidak retry (cegah spam + state ngawur).
- **Zero-qty guard** — order yang membulat ke 0 (akibat harga/config) ditolak fail-fast
  sebelum dikirim ke exchange.

### Korektnes uang & data
- **Fixed-point precision (`engine/precision.py`)** — harga/qty/uang lewat `Decimal`,
  bukan float. Menutup bug sizing laten (mis. float `0.3 / 0.1` → 0.2, kehilangan 1 step).
  `make_qty()` raise kalau hasil nol; `round_money()` menghapus drift di fee/PnL.
- **Stale-data 2 lapis** — selain cek waktu-terima (deteksi disconnect), engine cek
  `ts_event` (waktu exchange) untuk menolak trade saat **feed lag** (pesan masuk tapi
  data tua) — kasus yang luput dari cek waktu-terima. Lihat `engine/data_stream.py`
  (`best_bid_ts_event`, `data_latency_ms()`).

### Observability & verifikasi (jalankan di terminal terpisah dari bot)
- **`recap_run.py`** — observer independen: catat modal awal + posisi, snapshot tiap N
  detik dari Bybit langsung, lalu rekap (delta modal, **deteksi stacking via ground-truth
  Bybit**, scan log error). Contoh: `python recap_run.py --minutes 60`.
- **`verify_collector.py`** — observer truth: snapshot balance/posisi Bybit demo →
  `logs/bybit_truth.jsonl`, untuk cross-check klaim bot.
- **`tests/test_engine_stress.py`** — stress/concurrency: paksa race (10–100 order fire
  serempak) & buktikan guard mencegah dispatch kembar + tak ada lockout permanen.

---

## Cara Edit Strategi

**Default-nya kamu cukup edit `strategy.py`.** Kalau mau punya beberapa strategi,
buat file lain (mis. `strategy_ml.py`, `strategy_sqz.py`) lalu arahkan live bot ke
salah satunya lewat `STRATEGY_FILE` di `config.py` — nama file tanpa `.py`. Backtest
& tuning juga punya `STRATEGY_FILE` sendiri di config masing-masing, jadi tiap tool
bisa nunjuk strategi berbeda.

Strategi bebas pakai **Machine Learning** atau **indikator teknikal biasa** — tidak wajib.
Yang penting, fungsi `generate_signal()` ada dan return format yang benar.

### Fungsi wajib

| Fungsi | Deskripsi |
|---|---|
| `generate_signal(data)` | **SATU-SATUNYA fungsi wajib.** Analisa data dan return sinyal. |

> 🎉 **Sejak refactor `strategy_runtime`:** Anda **TIDAK PERLU** menulis `on_tick()`,
> `execute_trade()`, atau `manage_position()`. Engine otomatis panggil `generate_signal()`
> tiap tick → catat ke dashboard → forward ke `execution.place_order()` jika action ≠ hold.

### Format return — WAJIB

```python
{"action": "buy" | "sell" | "close" | "hold", "reason": "alasan (opsional)"}
```

> **Pre-flight check** menjalankan `generate_signal()` dengan data dummy saat startup.
> Jika return formatnya salah atau ada error, bot tidak jalan dan tampilkan pesan error.

### Contoh strategi minimal (cukup 1 fungsi!)

```python
def generate_signal(data):
    candles = list(data["candles"].values())[0]   # ambil timeframe apapun
    if len(candles) < 20:
        return {"action": "hold", "reason": "Data belum cukup"}

    closes = [c["close"] for c in candles]
    sma_fast = sum(closes[-5:]) / 5
    sma_slow = sum(closes[-20:]) / 20

    if sma_fast > sma_slow:
        return {"action": "buy", "reason": "SMA5 cross above SMA20"}
    return {"action": "hold", "reason": "Tidak ada sinyal"}
```

Itu saja. Tidak perlu import `execution`, `bot_monitor`, atau menulis `on_tick`. Engine handle semuanya.

### Mode advanced (opsional): override `on_tick()`

Hanya kalau butuh kontrol penuh — mis. sync state custom dengan `position_manager` seperti di
`strategy_ml.py` (Random Forest + Triple Barrier). Jika `on_tick()` didefinisikan, engine bypass
orkestrasi default dan serahkan eksekusi ke Anda.

```python
def on_tick(data):
    import execution, bot_monitor
    signal = generate_signal(data)
    bot_monitor.record_tick(signal, data=data)   # WAJIB di mode advanced
    if signal["action"] != "hold":
        execution.place_order(signal)
```

---

## 🔬 Hyperparameter Tuning

Cari parameter strategi terbaik via grid search dengan validasi out-of-sample. Semua diatur
lewat `tuning_config.py` — tidak ada CLI prompt.

### Cara Pakai

1. **Deklarasikan parameter di atas file strategi** (gaya MQL5 input):
   ```python
   BB_LENGTH       = 20
   KC_LENGTH       = 20
   STOP_LOSS_PCT   = 0.008
   TAKE_PROFIT_PCT = 0.020
   ```

2. **Edit `tuning_config.py`** — set strategy file, timeframe, rentang data, mode optimasi,
   model posisi, dan range parameter:
   ```python
   STRATEGY_FILE  = "strategy"        # nama file tanpa .py
   TIMEFRAME      = "2h"              # 1m, 5m, 15m, 30m, 1h, 2h, 4h, 1d
   DAYS_BACK      = 90                # rentang data dari sekarang
   OPTIM_MODE     = "balanced"        # profit | drawdown | balanced
   POSITION_MODEL = "single"          # single | multi
   PARAMS = {
       "BB_LENGTH":       [15, 25, 3],            # [start, end, jumlah] → [15, 20, 25]
       "KC_LENGTH":       [15, 25, 3],
       "STOP_LOSS_PCT":   [0.005, 0.015, 3],      # float → [0.005, 0.01, 0.015]
       "TAKE_PROFIT_PCT": [0.010, 0.040, 4],
   }
   ```

3. **Jalankan**:
   ```bash
   python user/hypertune.py
   ```

   Program otomatis: download OHLC dari Bybit → save CSV ke `data/` → split 80% in-sample /
   20% out-of-sample → grid search → validasi top 10 di OOS → **print top 3 di CMD**.

### Format `PARAMS`

| Format | Contoh | Hasil |
|---|---|---|
| **Range `[start, end, N]`** | `[10, 30, 5]` | `[10, 15, 20, 25, 30]` — linspace N nilai |
| **Range float** | `[0.005, 0.020, 4]` | `[0.005, 0.01, 0.015, 0.02]` — auto-detect float |
| **Discrete list** | `[True, False]` | `[True, False]` — apa pun yang bukan format range |
| **4 elemen+** | `[10, 15, 20, 25]` | discrete, pakai apa adanya |

Auto-detect tipe: kalau start & end keduanya `int` → hasil int (dedup setelah round). Selain itu → float.

### Mode Optimasi

| Mode | Skor |
|---|---|
| `profit` | Total PnL (USDT) — cari profit terbesar |
| `drawdown` | `-max_drawdown_pct` — cari drawdown terkecil |
| `balanced` | Sharpe ratio (return/risk) — paling direkomendasikan |

### Model Posisi

| Mode | Cara kerja | Cocok untuk |
|---|---|---|
| `single` | 1 posisi aktif. Strategy handle entry + exit (return `close` saat keluar). | **Tuning realistis** untuk live |
| `multi` | Tiap `buy`/`sell` = trade independen. Simulator handle exit pakai `STOP_LOSS_PCT` & `TAKE_PROFIT_PCT`. | Analisa kualitas sinyal mentah |

> ⚠️ **Multi-position tidak realistis untuk bot ini** (bot fisik selalu single-position via
> `position_manager`). Pakai single untuk tuning yang akan dieksekusi live.

### Output (di CMD)

Untuk tiap top 3: **Parameter** yang ditemukan + **In-Sample (80%)** & **Out-of-Sample (20%)**
(PnL USDT+%, Trades, Win Rate, Max DD, Profit Factor, Sharpe, Max Consecutive Wins/Losses).
CSV OHLC tersimpan di `data/<SYMBOL>_<TF>_<startdate>_<enddate>.csv`.

### Catatan Penting

- Hasil **hanya tampil di CMD** — strategy file TIDAK diubah otomatis. Copy nilai best manual.
- Out-of-sample jelek padahal in-sample bagus = **overfitting**, jangan dipakai.
- **Support strategi apa pun** yang: ada di folder ini, expose `generate_signal(data)`, parameter
  dideklarasi di atas file, nama param di `PARAMS` match variabel di strategy file. Ganti strategi
  cukup ubah `STRATEGY_FILE`, tidak perlu edit `hypertune.py`.

### Data tersedia di `data` dict

| Key | Isi |
|---|---|
| `data["candles"]["1m"]` | List candle 1m tertutup |
| `data["current"]["1m"]` | Candle 1m live (update tiap tick) |
| `data["candles"]["15m"]` | List candle 15m tertutup |
| `data["best_bid"]` / `data["best_ask"]` | `{"price": float, "qty": float}` |
| `data["bid_ask_spread"]` | ask - bid |
| `data["orderbook_imbalance"]` | -1.0 (sell) to +1.0 (buy) |
| `data["volume_delta"]` | buy_vol - sell_vol (50 tick terakhir) |
| `data["funding_rate"]` | Funding rate saat ini |
| `data["latest_tick"]` | Tick terakhir `{timestamp, price, qty, side}` |
| `data["is_warmup"]` | `True` saat preload historis — strategi tidak boleh order |

> Label `"1m"`, `"15m"` dst sesuai key di `TIMEFRAMES` dalam `config.py`.

---

## Cara Kerja Sistem

### Historical Candle Preload
Saat startup, `user/main.py` mengambil candle historis via Bybit REST (`/v5/market/kline`) untuk semua
timeframe ≥ 1m dan mengisi buffer. Selama proses ini `data["is_warmup"] = True` — strategi
otomatis return `hold`. Setelah selesai, flag di-set `False` dan bot masuk mode live.

### Data Mode: Tick vs Kline

| | `DATA_MODE = "kline"` (Default) | `DATA_MODE = "tick"` |
|---|---|---|
| **Sumber** | Bybit Kline WebSocket | Bybit publicTrade WebSocket |
| **RAM** | Ringan | Berat (ribuan tick/menit) |
| **Sinkronisasi TV** | 100% sama | Bisa beda (repainting) |
| **Volume Delta** | Tidak tersedia | Tersedia |
| **Cocok untuk** | SMA, BB, RSI, MACD | Footprint, Order Flow |

### Dua Timestamp pada Data (ts_event / ts_init)
Setiap update orderbook menyimpan dua waktu: `ts_event` (kapan kejadian di **exchange**) dan
`ts_init`/`*_updated_at` (kapan **kita terima**). Latensi feed = `ts_init - ts_event`
(`data_stream.data_latency_ms()`). Dipakai untuk deteksi feed lag & monitoring.

### Eksekusi Realistis
```
BUY  → entry @ ASK,  exit @ BID
SELL → entry @ BID,  exit @ ASK
```
Validasi pre-order: stale-data (2 lapis), VWAP slippage depth-aware, & liquidity check.
Qty dibulatkan fixed-point (Decimal) ke step instrumen.

### PnL Realtime
```python
# LONG
unrealized_pnl = (bid_price - entry_price) * qty
# SHORT
unrealized_pnl = (entry_price - ask_price) * qty

equity = balance + unrealized_pnl
```
Mode paper: dihitung lokal tiap tick. Mode demo/live: di-sync dari Bybit tiap `PNL_SYNC_INTERVAL`
detik (default 2s); state posisi juga di-update optimistic setelah fill.

### Heartbeat System
`logs/heartbeat.json` diupdate tiap detik. Dashboard baca file ini:
- **ONLINE** jika `time.time() - last_update < 5`
- **OFFLINE** jika tidak diupdate > 5 detik

---

## File Output (folder `logs/`)

| File | Isi |
|---|---|
| `logs/trade_history.csv` | Trade: entry/exit price, qty, PnL, alasan (terisi di mode paper) |
| `logs/equity_curve.csv` | Snapshot balance/equity/unrealized |
| `logs/heartbeat.json` | Timestamp terakhir bot aktif |
| `logs/bot_health.json` | Metrics health monitor (watchdog, error rate) |
| `logs/bybit_truth.jsonl` | Snapshot kebenaran Bybit (dari `verify_collector.py`) |
| `logs/recap_*.md` | Laporan rekap run (dari `recap_run.py`) |

> Catatan: di mode **demo/live**, trade dilog ke exchange (bukan `trade_history.csv`). Untuk
> rekap demo/live pakai `recap_run.py` + snapshot Bybit.

---

## Risiko

| Risiko | Mitigasi |
|---|---|
| **Duplicate-fire** | In-flight order guard + optimistic state update (lihat Reliability) |
| **Sizing salah** | Fixed-point precision (Decimal) + zero-qty guard |
| **Data basi / feed lag** | Stale-data 2 lapis (waktu-terima + ts_event exchange) |
| **Latency** | Gunakan VPS di region server exchange (mis. Singapore) |
| **Slippage** | `SLIPPAGE_TOLERANCE` + VWAP depth-aware check di `config.py` |
| **Disconnect** | Supervisor auto-restart task (exponential backoff) + CCXT Pro reconnect |
| **Partial Fill** | Deteksi + (opsional) cancel sisa via `CANCEL_ON_PARTIAL` |
| **State desync** | PnL sync loop (otoritatif dari Bybit) + clear pada reject `110017` |

---

## 🖥️ VPS Setup

```bash
# Install Python 3.12+
sudo apt update && sudo apt install python3.12 python3-pip -y

# Upload / clone project, lalu:
cd /your/project
pip install -r requirements.txt

# Jalankan bot di background
nohup python user/main.py > logs/bot.log 2>&1 &

# Jalankan dashboard
python -m streamlit run user/dashboard.py
```

---

## Struktur File

```
bot/
├── recap_run.py             ← 🔭 Observer: modal awal → rekap N menit (deteksi duplicate-fire)
├── verify_collector.py      ← 🔭 Observer: snapshot Bybit truth → logs/bybit_truth.jsonl
├── requirements.txt
├── pytest.ini
├── .env                     ←  API keys (EXCHANGE_* / BYBIT_*) — JANGAN commit
│
├── user/                    ←  USER-FACING (edit di sini)
│   ├── main.py              ← Entry point (auto pre-flight; tambah root+engine/+user/ ke sys.path)
│   ├── config.py            ← Konfigurasi akun (EXCHANGE, SYMBOL, MODE, DATA_MODE, STRATEGY_FILE)
│   ├── strategy.py          ←  USER EDIT DI SINI (default; cukup tulis generate_signal)
│   ├── strategy_ml.py       ←  Variant ML (Random Forest + Triple Barrier) — referensi advanced
│   ├── dashboard.py         ← Streamlit: Live Monitor (halaman utama)
│   ├── backtest.py          ← 📊 Backtest CLI mandiri (PnL + metrik lengkap)
│   ├── backtest_config.py   ← 📊 Konfigurasi backtest (TIMEFRAME, START/END, STRATEGY_FILE)
│   ├── hypertune.py         ← 🔬 Hyperparameter tuning CLI (grid search + 80/20 split)
│   ├── tuning_config.py     ← 🔬 Konfigurasi tuning (STRATEGY_FILE, PARAMS, OPTIM_MODE, ...)
│   ├── pages/               ← Streamlit multi-page
│   │   ├── 1_backtest.py      ← Simulasi backtest (Mode Colab & Live)
│   │   └── 2_control_panel.py ← Start/Stop bot + validasi strategy sebelum run
│   └── logs/                ← Auto-generated runtime (dibuat saat bot jalan)
│       ├── heartbeat.json     ← Timestamp bot aktif
│       ├── bot_health.json    ← Health monitor metrics
│       ├── trade_history.csv  ← Trade (paper mode)
│       └── equity_curve.csv   ← Snapshot balance/equity
│
├── engine/                  ←  INFRASTRUKTUR (jarang disentuh user)
│   ├── data_stream.py       ← WebSocket orderbook + funding + heartbeat (ts_event/ts_init)
│   ├── candle_stream.py     ← Bybit Kline WebSocket (DATA_MODE="kline")
│   ├── data_resampler.py    ← Tick → candle realtime (DATA_MODE="tick")
│   ├── strategy_runtime.py  ← Orkestrator (signal → record → execute)
│   ├── execution.py         ← Order paper/live + in-flight guard + optimistic state + precision
│   ├── position_manager.py  ← PnL tracking + optimistic state + sync Bybit
│   ├── precision.py         ←  Fixed-point Decimal (make_price / make_qty / round_money)
│   ├── ccxt_client.py       ← Wrapper CCXT Pro (init exchange, demo endpoint)
│   ├── connectivity.py      ← Probe konektivitas Bybit (deteksi blokir ISP TLS/DNS)
│   ├── trade_logger.py      ← CSV logging (non-blocking, daemon thread)
│   ├── bot_monitor.py       ← Health monitor: watchdog, error rate, data quality
│   ├── preflight_check.py   ← Validator strategi terpilih (config.STRATEGY_FILE)
│   ├── backtest_core.py     ← Mesin simulasi backtest/tuning (dibagi backtest + hypertune)
│   ├── backtest_metrics.py  ← Hitung metrik (PnL, win rate, drawdown, profit factor)
│   ├── backtest_data.py     ← Download + cache candle historis
│   ├── tuning_core.py       ← Mesin grid-search tuning
│   └── ft_types.py          ← Pusat definisi TypedDict (single source of truth)
│
├── models/                  ← Model ML (opsional — jika tidak ada, strategy jalan tanpa ML)
│   ├── trading_model_15m.pkl
│   └── trading_scaler_15m.pkl
│
├── tests/                   ←  pytest suite — `python -m pytest`
│   ├── test_execution.py        ← Order fill, in-flight guard, optimistic, stale-data
│   ├── test_engine_stress.py    ←  Stress/concurrency (race & lockout) — engine
│   ├── test_precision.py        ←  Fixed-point precision (Decimal)
│   ├── test_position_manager.py, test_data_stream.py, test_strategy_ml.py, ... (dll)
│   └── conftest.py
│
└── docs/
    ├── README.md            ← (file ini)
    └── penjelasan (1).txt
```

---

## Type Safety (Strict Typing)

Engine inti memakai **typing pragmatis (Mypy-compliant)**. Semua function signature & variabel
lokal dianotasi; struktur data kompleks didefinisikan sebagai `TypedDict` terpusat di
`engine/ft_types.py`.

| TypedDict | Dipakai oleh |
|---|---|
| `Candle` | `data_resampler`, `candle_stream`, `strategy` |
| `MarketDataSnapshot` | Return `get_live_data()` — kontrak antar modul |
| `Signal` | `strategy → execution` |
| `OrderResult` | `execution` |
| `PositionState` | `position_manager` |
| `PnlSummary` | `position_manager → strategy → dashboard` |
| `TradeLogRow` / `EquityLogRow` | `trade_logger` |

```bash
python -m mypy engine/ --ignore-missing-imports --no-strict-optional
```

> Typing **Level 3 (Pragmatic Strict)**: tidak pakai `mypy --strict` atau Pydantic karena data
> WebSocket Bybit bersifat dynamic JSON yang tak bisa diverifikasi penuh saat compile time.

### Flowchart (mermaid)
short: https://h1.nu/1pCx7

https://mermaid.live/edit#pako:eNqNV1tv2zYU_iucgA0bkLq-5FIbaAtbci6rHBmW2q6TDYGWGJuzRAoU1TZzAuxle9rTsKddsP_WX7CfsENSUmInG5aHiDLPdy4fD8852loxT4g1sK5S_iFeYyFR4MwZgj8_GM6CL8O___rtB5RfyzVnKMOUtfLrxVfoyZMXaDobh7kgVyldrWUUr0m8gc35nHWO0Xuc0gQXFBVSYElW1wpm9AJMwW8-_fkzOsUSp2gsBBc3YNCbfhl--v1XNOISjYhYEybp4qs92B8_Iu8VSL-7tMOcF1RSzqIMM7wiokUZvOM0Kq5ZDJ748EBLnGIWE_QFUuLgU4IFRaPrJZWVS0pXHZLrDR0VVspxEuE0jSTNyJXAGSlAocM_MLWDYsySlKA1LSQXtEDvKUazsR-g4fTiLlClTCt-O5xNXk-3tIg-YJGVOXqOAlGS2zkzsmZfiwZD_5Uffq2c3mCGCpKVGGEVEOVI4mJToBwLnJJ0UaM1RIMdP0yA0whoJzgzx_GWLH0eb4g0MaNpuUxpvHgAnWydYTCMJp4zvt3ffDsM7PNwyWWUcaCYi0o1lvE64SsUrMFe0jjkGJQ3Cj2RELHkfIMckkoMmCUpZLSkCXqK9BIiQmUOXpPFDjiYhbn2NBA4IQCUNN5Ey_LqiohHAeej8JxABi8JliC-rtet7wrI3ccQp7PwtGQJZSs0g00AXZnXSCXtHaQCTXQCblLKyA2y_dDkwA7XhuFXSgQ1vC928CqMG-TMzEEJUuAsT0lFaACb6NNPvyDv3LXfoFm92zhha89vYs6uqMieS0gi8GXUqZ0x_BSV76DRNokqSwlZl-Gi3CAjs3hU4RVOC6XR7oRRXAoBVzCqcn1fZUrfkz1aZ02ExpRyrfu4a7sQpUtZ7f6L1bv4Rx24yKBV_bf12u7qwzxznXBFZKRURYpb8HSCBfDvwIvPcF6suWwUgbiGuZ43DesqFaWc54Cb8jRVWdFpt7MCEjWHlFNRVU4rjEnwoIG2oAopEUAr4wP0H7a9wFQbTxcxn7KNgFtVqHppSpRWIszb_fpWVxavqVhnPgTNiPIhKuiK4fT_OHBW3Wr7fGy_uqtKL6t7b37XBxPoBDv3XKcTCiJLwfSLvvu6kC2JgEq1Kkq2WjxAn5psGr0-NZbMmRboxXN01K6t1bvGHrSNjTHY3TM4Mld_SdIyQ3G5KfPFIwre4Rt0cQkMbSmLav5eNoVWbzWCo-FsdjGehYGgcMvQCAtBwYStelmt3Ihovnx3ew7X23dr1323UWW7nj9uONJvix2hKrJgqnUE01pHMN3V0X1MRyVU6_C3AXQl5EvenFng76rpParG3ye5t0fyFDOJyyoNFw9Zq6ATdztxo_HlcOSOndqFyR0bp-NhEJ5C8S0FQWO2gnpIBNwosDDzLw7QZGg7B-gNh6MkB8jFde4oXN2JnTCD2SRtQTNOaCyjXPClutQzSCKeoVMOtVPe9VqnwnmjrZJEL9Bx-_PaNfWzdo5hujFbKi3f1dHDcrEnCb-XrBb1x65by6r1A2FFC8IJNqwe3md1scPOPe6P9rlXZFFoQDGGDotU9M0JgINQ65RpVfJ0rtWLbr3oaQYm3uVOoxYk5iKpa5MN5UCijEgBPECLTOW69s-71PihHVx4l1scq4uDPnuO1jxNah7N5v1AVC18sKdSYPzN2A7JRxKXSlErT3FMIq7GATWY6VI1QObZRKkwVRDOeKv-NbkFa606x1CMb9B0OIV7G-m3yFghdfX2aVamqpimfKOV7yhISMafmn7jXrwZh6Zh3HMvwtX0aJr5vamuOnRl2iTbpPNgBAUczwl7Gqe8IE0BqhsHGKyRjbpJp2pEZ6FUow6EsVKzLDwiWc0-tv8GMc6eLFOYKGhTamvs9NJ9PXUejsOmeUY5U10BZq6natha4oIkaMrcu4g03EyBQ_8cJpNiveRYJGYo8fWAkwIXE5NTTRs8azC1qibC-zyDft3pCgkjmfJGz-fNcakh3XANs3uOLlFCJK3rb4XdZ013JEh83SiqZ696HlbPo6bBz5l1YK0ETayBmpgOrIwI-JiBV2ur9M0tuSYZmVsDWELn3cytObsFTI7Zt5xnNUzwcrW2BnpIOrAMuw7FK_g8aH6F4QWSyOYlk9bgpN_WSqzB1vpoDTqHJ61er3_SPuz2O73Dk-7xgXVtDZ50ep1W__DZ8bOj7mH_pNc9uT2wvtd2e61O97jd77d7x8dHz_rtPiCgHMIhTMyHm_5-u_0HsrN8mg
