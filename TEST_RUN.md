# Test Run — 1 Jam Demo Validation

Setup buat validasi bot end-to-end di Bybit Demo. Target: 5–15 trade dalam 1 jam, full lifecycle (entry → hold → exit) tervalidasi, behavior bot cross-check dengan kebenaran exchange.

## 1. Pre-flight checklist

- [ ] API key demo Bybit sudah di `.env`:
  ```
  EXCHANGE_DEMO_API_KEY=xxx
  EXCHANGE_DEMO_API_SECRET=yyy
  ```
- [ ] Balance demo Bybit minimal 50 USDT (top-up via Bybit dashboard kalau kurang)
- [ ] Internet stabil, PC gak akan sleep selama 1 jam
- [ ] Disk space minimal 50 MB di folder `logs/`

## 2. Config yang HARUS di-set

Edit [config.py](config.py):

```python
MODE = "demo"              # ganti dari "paper"
ORDER_SIZE_USDT = 2        # kecil tapi cukup buat fee tangible
LEVERAGE = 3               # konservatif (default 10 terlalu agresif buat test)
SLIPPAGE_TOLERANCE = 0.001 # 0.1% — longgar buat demo
MAX_RETRY = 2              # cepat fail kalau bermasalah
DATA_MODE = "kline"        # lebih simple buat verify
```

`TIMEFRAMES` biarkan default — strategy butuh `"1m"` yang sudah ada.

## 3. Jalankan test

**Buka 2 terminal di folder bot.**

### Terminal 1 — Bot
```powershell
python main.py 2>&1 | Tee-Object -FilePath logs/bot.log
```

Tunggu sampai muncul log `Strategy loop started` (biasanya 30-60 detik untuk warmup historical kline).

### Terminal 2 — Verifier
```powershell
python verify_collector.py 2>&1 | Tee-Object -FilePath logs/verify.log
```

Akan print 1 baris setiap 30 detik dengan balance + jumlah posisi dari Bybit langsung.

## 4. Observasi selama 1 jam

Yang perlu kamu perhatikan (sambil ngopi, gak perlu fokus):

- **Bybit demo dashboard** di browser — cek trade muncul real-time di sana
- **Terminal bot** — error spam atau exception traceback?
- **Terminal verifier** — balance turun wajar (ada fee), posisi muncul-hilang sesuai trade?

Kalau ada yang aneh BANGET (bot loop error, balance drop drastis), Ctrl+C kedua terminal, screenshot, lapor.

## 5. Stop test

Setelah 1 jam:

1. **Terminal Bot**: Ctrl+C → tunggu graceful shutdown (bot tutup posisi terbuka? Tergantung implementasi)
2. **Terminal Verifier**: Ctrl+C → flush snapshot terakhir

Catatan: kalau bot masih punya posisi terbuka saat di-Ctrl+C, **close manual via Bybit dashboard** supaya gak menggantung.

## 6. Yang harus kamu kirim ke saya

Zip folder `logs/` saja + `config.py`. Isi yang saya butuhkan:

```
logs/
├── bot.log                  ← terminal bot
├── bot_health.json          ← state terakhir
├── trade_history.csv        ← klaim trade bot
├── equity_curve.csv         ← snapshot equity per menit
├── verify.log               ← terminal verifier
├── bybit_truth.jsonl        ← kebenaran exchange
└── heartbeat.json           ← (kalau ada)

config.py                    ← settings tanpa API key (akan saya cek)
```

**Tambahan info di message:**
- Tanggal & jam start–end (`2026-05-29 14:00 – 15:00 WIB`)
- Observasi kamu (kalau ada yang aneh)
- Balance demo di Bybit dashboard di start vs end

## 7. Yang saya kasih balik

Setelah saya analisis, kamu dapat:

1. **Verdict** — Normal / Issues detected / Critical bug
2. **Cross-check report** — bot claim vs Bybit truth: berapa % match, mismatch di mana
3. **PnL reconciliation** — apakah math accountingnya benar
4. **Error pattern** — kalau ada error, klasifikasi (transient/permanent/fatal)
5. **Rekomendasi** — bug yang harus difix sebelum naik ke fase berikutnya

## 8. Catatan jujur

**1 jam test ini valid untuk:**
- ✅ Verify code path bot match kenyataan exchange
- ✅ Detect bug execution / state sync / accounting
- ✅ Sanity check sebelum kamu invest waktu testing yang lebih lama

**1 jam test ini TIDAK valid untuk:**
- ❌ Validasi strategi profitable (MA crossover sederhana TIDAK winning strategy)
- ❌ Stress test (volume rendah dibanding production)
- ❌ Long-running stability (memory leak, dll baru muncul setelah jam-jaman)

**Setelah test 1 jam PASS:** lanjut ke `strategy_ml.py` di paper mode 3–7 hari sebelum demo trade.

## 9. Troubleshooting

| Gejala | Solusi |
| --- | --- |
| Bot stuck di "STARTING" lebih dari 2 menit | Cek `logs/bot.log` — kemungkinan API key salah atau WebSocket gak connect |
| `ModuleNotFoundError: No module named 'strategy'` | Pastikan `strategy.py` ada di root folder bot |
| Verifier print "API key demo tidak ditemukan" | Cek `.env` — variable name harus `EXCHANGE_DEMO_API_KEY` |
| Order rejected dengan "insufficient balance" | Top-up demo balance di Bybit dashboard |
| Bot tutup berkali-kali tanpa entry | Wajar 5–15 menit pertama (warmup + nunggu MA cross) |
| `bybit_truth.jsonl` ada ERR di banyak baris | Internet drop / rate limit Bybit demo; kalau jarang OK, kalau sering ada masalah jaringan |
