# Trading Bot System

Sistem bot trading berbasis Python yang modular, real-time, dan production-ready untuk Bybit Perpetual Futures.

---

##  Arsitektur

```
Bybit (WebSocket/REST)
        │
        ▼
  data_stream.py        ← OrderBook, Funding Rate, Heartbeat
        │
        ├─[DATA_MODE="tick"]──► data_resampler.py  ← Susun candle dari tick mentah
        │
        └─[DATA_MODE="kline"]─► candle_stream.py   ← Candle instan Bybit Kline WS
                                        │
                                        ▼
                               strategy.py           ←  USER EDITS HERE ONLY
                                        │
                                        ▼
                               execution.py          ← Order placement (paper/live)
                                        │
                                        ▼
                               position_manager.py   ← Realtime PnL (bid/ask based)
                                        │
                                        ▼
                               trade_logger.py       ← CSV logging (trades + equity)
                                        │
                                        ▼
                               dashboard.py          ← Streamlit monitoring dashboard
```

---

##  Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure

Buat file `.env` di folder utama (copy dari `.env.example`) dan isi kunci API Anda:

```env
BYBIT_LIVE_API_KEY="api_key_live_kamu"
BYBIT_LIVE_API_SECRET="secret_live_kamu"
BYBIT_DEMO_API_KEY="api_key_demo_kamu"
BYBIT_DEMO_API_SECRET="secret_demo_kamu"
```

Lalu edit konfigurasi tambahan di `config.py`:

SYMBOL = "DOGEUSDT"
MODE   = "paper"   # "paper" | "demo" | "live"

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

### 3. Validasi strategy.py (opsional tapi sangat disarankan)

```bash
python preflight_check.py
```

Ini akan menjalankan **16 pengecekan otomatis** terhadap `strategy.py`, terbagi dua level:

| Level | Contoh | Efek |
|---|---|---|
|  **Error Fatal** | Syntax error, `on_tick()` tidak ada, `time.sleep()` di generate_signal | Bot **DIBLOKIR** |
|  **Saran** | HTTP request di tiap tick, file I/O, data parsial | Bot **tetap bisa jalan**, hanya diingatkan |

>  Validasi ini juga tersedia langsung di halaman **⚙️ Control Panel** — klik saja tombol **▶️ Start Bot** dan sistem akan memvalidasi otomatis sebelum bot dinyalakan.

### 4. Run the bot

```bash
python main.py
```

>  `main.py` otomatis menjalankan pre-flight check saat startup. Jika `strategy.py` punya **error fatal**, bot akan berhenti dan tampilkan pesan errornya. **Saran (warning)** tetap ditampilkan tapi tidak menghentikan bot.

Untuk backtest:
```bash
streamlit run dashboard.py
# lalu buka menu 🔬 Backtest di sidebar
```

### 4. Run the dashboard (separate terminal)

```bash
python -m streamlit run dashboard.py
```

Then open: `http://localhost:8501`

### 🎩 Memantau Banyak Bot Sekaligus (Master Dashboard)
Jika Anda men-duplikasi folder bot untuk menjalankan strategi/koin berbeda (Misal: `Bot_DOGE`, `Bot_BTC`), Anda **TIDAK PERLU** menjalankan banyak dashboard. 
Cukup buka 1 halaman Streamlit, lalu pada kolom teks **Sidebar**, masukkan path folder-folder bot Anda (setiap path beda baris). Aplikasi secara ajaib akan membaca rekaman mereka serentak dan menggambar **1 Portofolio Gabungan** untuk seluruh kekayaan bot Anda!

## 🔀 Perbandingan Mode

| | Paper | Demo | Live |
|---|---|---|---|
| API Key | Tidak perlu | DEMO_API_KEY | API_KEY |
| Order | Simulasi lokal | Masuk server Bybit Demo | Uang nyata |
| PnL/Balance | Hitung lokal | Fetch dari Bybit tiap 2s | Fetch dari Bybit tiap 2s |
| Reset saldo | Restart bot | UI Bybit Demo | - |
| Cocok untuk | Dev & debug strategi | Validasi eksekusi API | Trading nyata |

### Setup Mode Demo
1. Login Bybit → **Trade → Demo Trading**
2. Buat API Key: **Account → API Management** (pilih System-generated)
3. Isi `config.py`:
```python
MODE = "demo"
DEMO_API_KEY    = "api_key_dari_akun_demo"
DEMO_API_SECRET = "secret_dari_akun_demo"
```

---

##  Cara Edit Strategi

**Kamu HANYA perlu edit `strategy.py`.**

Strategi bebas menggunakan **Machine Learning** atau **indikator teknikal biasa** — tidak ada kewajiban. Yang penting, fungsi `generate_signal()` harus ada dan return format yang benar.

### Fungsi wajib

| Fungsi | Deskripsi |
|---|---|
| `generate_signal(data)` | Analisa data dan return sinyal trading |
| `on_tick(data)` | Entry point — dipanggil setiap candle baru oleh `main.py` |

### Format return — WAJIB

```python
# generate_signal() HARUS return dict dengan key "action"
{"action": "buy" | "sell" | "close" | "hold", "reason": "alasan (opsional)"}
```

> **Pre-flight check** akan menjalankan `generate_signal()` dengan data dummy saat startup.
> Jika return formatnya salah atau ada error, bot tidak akan jalan dan tampilkan pesan error.

### Contoh strategy tanpa ML (pure indicator)

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

def on_tick(data):
    import execution
    signal = generate_signal(data)
    if signal["action"] != "hold":
        execution.place_order(signal)
```

### Data tersedia di `data` dict

| Key | Isi |
|---|---|
| `data["candles"]["1m"]` | List candle 1m tertutup |
| `data["current"]["1m"]` | Candle 1m live (update tiap tick) |
| `data["candles"]["15m"]` | List candle 15m tertutup |
| `data["current"]["15m"]` | Candle 15m live (update tiap tick) |
| `data["best_bid"]` / `data["best_ask"]` | `{"price": float, "qty": float}` |
| `data["bid_ask_spread"]` | ask - bid |
| `data["orderbook_imbalance"]` | -1.0 (sell) to +1.0 (buy) |
| `data["volume_delta"]` | buy_vol - sell_vol (50 tick terakhir) |
| `data["funding_rate"]` | Funding rate saat ini |
| `data["latest_tick"]` | Tick terakhir `{timestamp, price, qty, side}` |
| `data["is_warmup"]` | `True` saat preload historis berlangsung — strategi tidak boleh order |

> Label `"1m"`, `"15m"` dst sesuai key yang kamu isi di `TIMEFRAMES` dalam `config.py`.

---

##  Cara Kerja Sistem

### Historical Candle Preload
Saat startup, `main.py` mengambil candle historis via Bybit REST API (`/v5/market/kline`) untuk semua timeframe ≥ 1m dan mengisi buffer resampler secara langsung. Selama proses ini `data["is_warmup"] = True` — strategi otomatis return `hold` dan tidak akan generate order. Setelah selesai, flag di-set `False` dan bot masuk mode live.

### Data Mode: Tick vs Kline
Pilihan mode diatur via `config.py → DATA_MODE`:

| | `DATA_MODE = "kline"` (Default) | `DATA_MODE = "tick"` |
|---|---|---|
| **Sumber** | Bybit Kline WebSocket | Bybit publicTrade WebSocket |
| **RAM** | Ringan | Berat (ribuan tick/menit) |
| **Sinkronisasi TV** |  100% sama |  Bisa beda (repainting) |
| **Volume Delta** |  Tidak tersedia |  Tersedia |
| **Cocok untuk** | SMA, BB, RSI, MACD | Footprint, Order Flow |

### Realtime Candle
`candle_stream.py` (kline mode): menerima update candle live dari Bybit.
Field `"confirm": true` = candle resmi ditutup → disimpan ke buffer historis.

`data_resampler.py` (tick mode): mengkonversi setiap tick menjadi candle OHLCV
sesuai timeframe yang dikonfigurasi di `TIMEFRAMES`.

### Eksekusi Realistis
```
BUY  → entry @ ASK,  exit @ BID
SELL → entry @ BID,  exit @ ASK
```
Slippage tolerance configurable. Retry 3x dengan delay ms.

### PnL Realtime
```python
# LONG
unrealized_pnl = (bid_price - entry_price) * qty

# SHORT
unrealized_pnl = (entry_price - ask_price) * qty

equity = balance + unrealized_pnl
```

### Heartbeat System
`heartbeat.json` diupdate setiap detik. Dashboard baca file ini:
- **ONLINE** jika `time.time() - last_update < 5`
- **OFFLINE** jika tidak diupdate lebih dari 5 detik

---

##  File Output

| File | Isi |
|---|---|
| `trade_history.csv` | Semua trade: entry/exit price, qty, PnL, alasan |
| `equity_curve.csv` | Snapshot balance/equity/unrealized tiap tick |
| `heartbeat.json` | Timestamp terakhir bot aktif |

---

## Risiko

| Risiko | Mitigasi |
|---|---|
| **Latency** | Gunakan VPS di region Bybit server (Singapore) |
| **Slippage** | `SLIPPAGE_TOLERANCE` di `config.py` |
| **Market Volatility** | Set `TARGET_SL_PCT` dan `TARGET_TP_PCT` di bagian paling atas `strategy.py` |
| **Disconnect** | Auto-reconnect WebSocket dengan exponential backoff |
| **Partial Fill** | Retry otomatis atau cancel (via `CANCEL_ON_PARTIAL`) |

---

## 🖥️ VPS Setup

```bash
# Install Python 3.10+
sudo apt update && sudo apt install python3.10 python3-pip -y

# Clone / upload project
cd /your/project

# Install deps
pip install -r requirements.txt

# Run bot in background
nohup python main.py > bot.log 2>&1 &

# Run dashboard
python -m streamlit run dashboard.py
```

---

##  Struktur File

```
bot/
├── main.py               ← Entry point (jalankan pre-flight check otomatis)
├── preflight_check.py    ←  Validator strategy.py (16 cek: error + warning)
├── config.py             ←  Konfigurasi semua parameter (termasuk DATA_MODE)
├── ft_types.py           ←  Pusat definisi tipe data (TypedDict: Candle, Signal, dll)
├── .env.example          ←  Template API Key — copy ke .env dan isi
├── data_stream.py        ← WebSocket orderbook + funding rate + heartbeat
├── candle_stream.py      ← Bybit Kline WebSocket (aktif jika DATA_MODE="kline")
├── data_resampler.py     ← Tick → candle realtime (aktif jika DATA_MODE="tick")
├── strategy.py           ←  USER EDIT DI SINI (ML atau pure indicator)
├── strategy_sqz_backup.py← Backup strategi Squeeze Momentum asli
├── execution.py          ← Order execution (paper / live)
├── position_manager.py   ← PnL tracking
├── bot_monitor.py        ←  Health monitor: watchdog, error rate, data quality
├── trade_logger.py       ← CSV logging (non-blocking)
├── dashboard.py          ← Streamlit: Live Monitor Main Page
├── pages/                ← Streamlit: Menu tambahan (Multi-Page App)
│   ├── 1_backtest.py     ← Simulasi backtest (Mode Colab & Mode Simulasi Live)
│   └── 2_control_panel.py←  Start/Stop bot + validasi strategy sebelum run
├── trading_model_15m.pkl ← Model ML (opsional — jika tidak ada, strategy jalan tanpa ML)
├── trading_scaler_15m.pkl← Scaler ML (opsional)
├── requirements.txt
├── heartbeat.json        ← Auto-generated
├── bot_health.json       ← Auto-generated (health monitor metrics)
├── trade_history.csv     ← Auto-generated
└── equity_curve.csv      ← Auto-generated
```

---

## Type Safety (Strict Typing)

Seluruh engine inti telah di-refactor dengan **Mypy-compliant strict typing** (Mei 2026). Tidak ada logika bisnis, formula matematika, atau integrasi API yang diubah — hanya anotasi tipe yang ditambahkan.

### File `ft_types.py` — Single Source of Truth

Semua struktur data kompleks didefinisikan sebagai `TypedDict` di satu tempat:

| TypedDict | Dipakai oleh |
|---|---|
| `Candle` | `data_resampler`, `candle_stream`, `strategy` |
| `MarketDataSnapshot` | Return type `get_live_data()` — kontrak antar semua modul |
| `Signal` | `strategy → execution` |
| `OrderResult` | `execution` |
| `PositionState` | `position_manager` |
| `PnlSummary` | `position_manager → strategy → dashboard` |
| `TradeLogRow` / `EquityLogRow` | `trade_logger` |
| `BotState` | `strategy` (state Triple Barrier) |

### Status Mypy

```
python -m mypy ft_types.py data_stream.py data_resampler.py execution.py \
               strategy.py position_manager.py main.py trade_logger.py \
               candle_stream.py --ignore-missing-imports --no-strict-optional
```

| File | Status |
|---|---|
| `ft_types.py` |  0 error |
| `data_stream.py` |  0 error |
| `data_resampler.py` |  0 error |
| `execution.py` |  0 error |
| `strategy.py` |  0 error |
| `position_manager.py` |  0 error |
| `main.py` |  0 error |
| `trade_logger.py` |  0 error |
| `candle_stream.py` | 0 error |
| `bot_monitor.py` |  Belum (monitoring only, tidak kritis) |
| `preflight_check.py` |  Belum (dynamic import pattern, false positives) |

> Typing berada di **Level 3 (Pragmatic Strict)**: semua function signature dan variabel lokal dianotasi, menggunakan `TypedDict` terpusat. Tidak menggunakan `mypy --strict` atau Pydantic karena data WebSocket dari Bybit bersifat dynamic JSON yang tidak bisa diverifikasi sepenuhnya saat compile time. link flowchart mermaid.
short: https://h1.nu/1pCx7

 https://mermaid.live/edit#pako:eNqNV1tv2zYU_iucgA0bkLq-5FIbaAtbci6rHBmW2q6TDYGWGJuzRAoU1TZzAuxle9rTsKddsP_WX7CfsENSUmInG5aHiDLPdy4fD8852loxT4g1sK5S_iFeYyFR4MwZgj8_GM6CL8O___rtB5RfyzVnKMOUtfLrxVfoyZMXaDobh7kgVyldrWUUr0m8gc35nHWO0Xuc0gQXFBVSYElW1wpm9AJMwW8-_fkzOsUSp2gsBBc3YNCbfhl--v1XNOISjYhYEybp4qs92B8_Iu8VSL-7tMOcF1RSzqIMM7wiokUZvOM0Kq5ZDJ748EBLnGIWE_QFUuLgU4IFRaPrJZWVS0pXHZLrDR0VVspxEuE0jSTNyJXAGSlAocM_MLWDYsySlKA1LSQXtEDvKUazsR-g4fTiLlClTCt-O5xNXk-3tIg-YJGVOXqOAlGS2zkzsmZfiwZD_5Uffq2c3mCGCpKVGGEVEOVI4mJToBwLnJJ0UaM1RIMdP0yA0whoJzgzx_GWLH0eb4g0MaNpuUxpvHgAnWydYTCMJp4zvt3ffDsM7PNwyWWUcaCYi0o1lvE64SsUrMFe0jjkGJQ3Cj2RELHkfIMckkoMmCUpZLSkCXqK9BIiQmUOXpPFDjiYhbn2NBA4IQCUNN5Ey_LqiohHAeej8JxABi8JliC-rtet7wrI3ccQp7PwtGQJZSs0g00AXZnXSCXtHaQCTXQCblLKyA2y_dDkwA7XhuFXSgQ1vC928CqMG-TMzEEJUuAsT0lFaACb6NNPvyDv3LXfoFm92zhha89vYs6uqMieS0gi8GXUqZ0x_BSV76DRNokqSwlZl-Gi3CAjs3hU4RVOC6XR7oRRXAoBVzCqcn1fZUrfkz1aZ02ExpRyrfu4a7sQpUtZ7f6L1bv4Rx24yKBV_bf12u7qwzxznXBFZKRURYpb8HSCBfDvwIvPcF6suWwUgbiGuZ43DesqFaWc54Cb8jRVWdFpt7MCEjWHlFNRVU4rjEnwoIG2oAopEUAr4wP0H7a9wFQbTxcxn7KNgFtVqHppSpRWIszb_fpWVxavqVhnPgTNiPIhKuiK4fT_OHBW3Wr7fGy_uqtKL6t7b37XBxPoBDv3XKcTCiJLwfSLvvu6kC2JgEq1Kkq2WjxAn5psGr0-NZbMmRboxXN01K6t1bvGHrSNjTHY3TM4Mld_SdIyQ3G5KfPFIwre4Rt0cQkMbSmLav5eNoVWbzWCo-FsdjGehYGgcMvQCAtBwYStelmt3Ihovnx3ew7X23dr1323UWW7nj9uONJvix2hKrJgqnUE01pHMN3V0X1MRyVU6_C3AXQl5EvenFng76rpParG3ye5t0fyFDOJyyoNFw9Zq6ATdztxo_HlcOSOndqFyR0bp-NhEJ5C8S0FQWO2gnpIBNwosDDzLw7QZGg7B-gNh6MkB8jFde4oXN2JnTCD2SRtQTNOaCyjXPClutQzSCKeoVMOtVPe9VqnwnmjrZJEL9Bx-_PaNfWzdo5hujFbKi3f1dHDcrEnCb-XrBb1x65by6r1A2FFC8IJNqwe3md1scPOPe6P9rlXZFFoQDGGDotU9M0JgINQ65RpVfJ0rtWLbr3oaQYm3uVOoxYk5iKpa5MN5UCijEgBPECLTOW69s-71PihHVx4l1scq4uDPnuO1jxNah7N5v1AVC18sKdSYPzN2A7JRxKXSlErT3FMIq7GATWY6VI1QObZRKkwVRDOeKv-NbkFa606x1CMb9B0OIV7G-m3yFghdfX2aVamqpimfKOV7yhISMafmn7jXrwZh6Zh3HMvwtX0aJr5vamuOnRl2iTbpPNgBAUczwl7Gqe8IE0BqhsHGKyRjbpJp2pEZ6FUow6EsVKzLDwiWc0-tv8GMc6eLFOYKGhTamvs9NJ9PXUejsOmeUY5U10BZq6natha4oIkaMrcu4g03EyBQ_8cJpNiveRYJGYo8fWAkwIXE5NTTRs8azC1qibC-zyDft3pCgkjmfJGz-fNcakh3XANs3uOLlFCJK3rb4XdZ013JEh83SiqZ696HlbPo6bBz5l1YK0ETayBmpgOrIwI-JiBV2ur9M0tuSYZmVsDWELn3cytObsFTI7Zt5xnNUzwcrW2BnpIOrAMuw7FK_g8aH6F4QWSyOYlk9bgpN_WSqzB1vpoDTqHJ61er3_SPuz2O73Dk-7xgXVtDZ50ep1W__DZ8bOj7mH_pNc9uT2wvtd2e61O97jd77d7x8dHz_rtPiCgHMIhTMyHm_5-u_0HsrN8mg
