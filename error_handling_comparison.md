# Error Handling Architecture Comparison: Freqtrade vs. Your Trading Engine

Berdasarkan analisis pada arsitektur folder `freqtrade-develop` dan program trading engine Anda, terdapat perbedaan signifikan dalam cara menangani *error* (Error Handling). Berikut adalah perbandingan dan hal-hal yang masih kurang dari program Anda.

## 1. Custom Exception Hierarchy vs. Generic Exception Catching

**Freqtrade:**
Menggunakan hierarki *exception* kustom yang sangat terstruktur (lihat `freqtrade/exceptions.py`). Mereka membedakan error menjadi berbagai kategori spesifik:
*   `OperationalException`: Error konfigurasi atau hal fatal yang mengharuskan bot berhenti (misal: API key salah).
*   `TemporaryError` (termasuk `DDosProtection`): Error jaringan atau *exchange* yang bersifat sementara. Bot tahu bahwa ia hanya perlu menunggu (*sleep*) dan mencoba lagi.
*   `DependencyException` (seperti `PricingError`, `ExchangeError`, `InsufficientFundsError`): Error operasional spesifik saat trading.

**Trading Engine Anda:**
Sangat bergantung pada penangkapan error secara umum menggunakan `except Exception as e:`.
*Contoh di `main.py` & `execution.py`:*
```python
except Exception as e:
    logger.error(f"Strategy error: {e}", exc_info=True)
```
**Kekurangan Anda:** Menangkap base `Exception` sangat berbahaya karena Anda tidak bisa membedakan mana error jaringan yang bisa di-*retry*, mana error validasi API (misal saldo tidak cukup), dan mana error *bug* pada kode (misal `TypeError` atau `KeyError`). Semua diperlakukan sama.

## 2. State Management & Graceful Degradation

**Freqtrade:**
Memiliki sistem *State Machine* (`RUNNING`, `STOPPED`, `RELOAD_CONFIG`). Jika terjadi `OperationalException`, *worker* tidak langsung *crash*, melainkan mengubah *state* bot menjadi `STOPPED`, mengirim notifikasi ke user (Telegram/UI), dan menunggu intervensi manual (perintah `/start`). Jika terjadi `TemporaryError`, bot hanya akan *sleep* sejenak dan *retry*.

**Trading Engine Anda:**
*   **Blind Retry:** Pada `_live_place_order_async` di `execution.py`, bot melakukan loop `MAX_RETRY`. Jika terjadi error apa saja (termasuk error fatal), bot akan terus mencoba `retry`.
*   **Abrupt Shutdown:** Di `main.py`, Anda menggunakan `asyncio.wait(..., return_when=asyncio.FIRST_EXCEPTION)`. Jika salah satu *task* (seperti `data_stream`) mati karena *unhandled exception*, seluruh program langsung membatalkan semua *task* dan *shutdown*.
*   **Zombie Strategy:** Jika terjadi error pada fungsi `strategy.on_tick()` (misal pembagian dengan nol), error tersebut hanya di-log dan *loop* berlanjut. Ini bisa menyebabkan strategi berjalan dalam kondisi data korup/rusak secara terus-menerus.

## 3. Standardisasi Error dari Exchange

**Freqtrade:**
Menggunakan *library* seperti `ccxt` yang menstandarkan ratusan error dari berbagai *exchange* menjadi error yang mudah dibaca (seperti `InsufficientFunds` atau `NetworkError`).

**Trading Engine Anda:**
Secara manual mengurai JSON dari Bybit. Memang Anda sudah melakukan penanganan khusus untuk `ret_code == 10001` (Hedge Mode reduceOnly ditolak), yang merupakan langkah bagus. Namun, *network timeout*, DNS failure, atau HTTP 500 error masih tertangkap oleh *catch-all* `Exception`.

---

## Rekomendasi Perbaikan untuk Trading Engine Anda

1. **Buat Class Exception Khusus:**
   Buat file `exceptions.py` di proyek Anda:
   ```python
   class TradingException(Exception): pass
   class NetworkError(TradingException): pass
   class APIError(TradingException): pass
   class FatalConfigError(TradingException): pass
   ```
2. **Ganti Catch-All `Exception` dengan Exception Spesifik:**
   Di `execution.py` saat melakukan *request* aiohttp, tangkap spesifik `aiohttp.ClientError` atau `asyncio.TimeoutError` sebagai `NetworkError` untuk di-*retry*, namun jika `APIError` (misal *Insufficient Margin*) jangan di-*retry* lagi, langsung batalkan order.
3. **Implementasikan State Machine atau Pause Mechanism:**
   Daripada bot langsung *crash* atau strategi terus jalan dengan error, buat mekanisme agar bot berpindah ke status `PAUSED` jika terjadi error logika beruntun, lalu kirim *alert* ke Dashboard Streamlit/Telegram.
