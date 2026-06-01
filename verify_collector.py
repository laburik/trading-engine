# =============================================================================
# verify_collector.py — Paralel Bybit Truth Collector
# =============================================================================
# Tujuan: fetch state nyata dari Bybit demo (balance + posisi) setiap N detik,
# simpan ke logs/bybit_truth.jsonl sebagai "source of truth" yang independen
# dari bot. Dipakai untuk cross-check dengan klaim bot (bot_health.json,
# trade_history.csv) setelah test selesai.
#
# Jalankan di TERMINAL TERPISAH dari bot:
#   python verify_collector.py
#
# Hentikan dengan Ctrl+C (akan flush snapshot terakhir lalu exit).
# =============================================================================
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

# Tambah engine/ ke sys.path supaya import ccxt_client jalan (sama pattern dengan main.py)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine"))

import ccxt.pro as ccxt
from dotenv import load_dotenv

load_dotenv()

# ── Config — bisa di-override via env ────────────────────────────────────────
SYMBOL_RAW: str        = os.getenv("SYMBOL", "XRPUSDT")     # format raw exchange
INTERVAL_SEC: int      = int(os.getenv("VERIFY_INTERVAL", "30"))
OUTPUT_FILE: str       = "logs/bybit_truth.jsonl"
EXCHANGE_NAME: str     = os.getenv("EXCHANGE", "bybit")

os.makedirs("logs", exist_ok=True)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_exchange():
    """Init CCXT bybit dengan API key demo."""
    api_key = os.getenv("EXCHANGE_DEMO_API_KEY") or os.getenv("BYBIT_DEMO_API_KEY")
    api_secret = os.getenv("EXCHANGE_DEMO_API_SECRET") or os.getenv("BYBIT_DEMO_API_SECRET")

    if not api_key or not api_secret:
        print("[VERIFY] FATAL: API key demo tidak ditemukan di .env")
        print("  Set EXCHANGE_DEMO_API_KEY dan EXCHANGE_DEMO_API_SECRET")
        sys.exit(1)

    ex_cls = getattr(ccxt, EXCHANGE_NAME)
    ex = ex_cls({
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "options": {
            "defaultType": "linear",
            # Auto-koreksi offset jam lokal vs server + lebarkan toleransi timestamp.
            # Observer ini yang sebelumnya kena InvalidNonce/10002 saat jam OS drift ~7s.
            "adjustForTimeDifference": True,
            "recvWindow": 10000,
        },
    })
    # Bybit Demo Trading endpoint. JANGAN pakai ex.hostname karena CCXT prefix
    # "api." otomatis → URL jadi api.api-demo.bybit.com (404). Pakai
    # enable_demo_trading() kalau ada, fallback ke override urls.
    if EXCHANGE_NAME == "bybit":
        if hasattr(ex, "enable_demo_trading"):
            ex.enable_demo_trading(True)
        else:
            api_urls = ex.urls.get("api", {})
            if isinstance(api_urls, dict):
                api_urls["public"]  = "https://api-demo.bybit.com"
                api_urls["private"] = "https://api-demo.bybit.com"
                ex.urls["api"] = api_urls
    return ex


async def _resolve_symbol(ex) -> str:
    """Resolve raw symbol (XRPUSDT) ke unified CCXT symbol PERPETUAL (XRP/USDT:USDT)."""
    await ex.load_markets()
    market = ex.markets_by_id.get(SYMBOL_RAW)
    # Sama dengan bug #4 di ccxt_client.py: markets_by_id["XRPUSDT"] return
    # list dengan SPOT + PERPETUAL. Tanpa filter, ambil arbitrary → kadang spot.
    # Bot trading di linear perp, jadi WAJIB filter swap+linear.
    if isinstance(market, list):
        picked = None
        for m in market:
            if isinstance(m, dict) and m.get("swap") and m.get("linear"):
                picked = m
                break
        market = picked or (market[0] if market else None)
    if not market:
        raise RuntimeError(f"Symbol {SYMBOL_RAW} tidak ditemukan di {EXCHANGE_NAME} markets")
    return market["symbol"]


async def _snapshot(ex, ccxt_symbol: str) -> dict:
    """Fetch posisi + balance + flag error per request."""
    snap: dict = {
        "timestamp":   _iso_now(),
        "epoch":       time.time(),
        "balance_usdt": None,
        "positions":    [],
        "errors":       [],
    }

    # --- Balance ---
    try:
        bal = await ex.fetch_balance()
        usdt = bal.get("USDT", {}) if isinstance(bal, dict) else {}
        snap["balance_usdt"] = usdt.get("total")
    except Exception as e:
        snap["errors"].append(f"fetch_balance: {type(e).__name__}: {e}")

    # --- Positions ---
    # Bybit perlu param `category='linear'` eksplisit untuk perpetual.
    # Tanpa ini, fetch_positions default ke spot → retCode 181001.
    try:
        positions = await ex.fetch_positions([ccxt_symbol], params={"category": "linear"})
        for p in positions:
            if not isinstance(p, dict):
                continue
            contracts = p.get("contracts")
            # Skip yang gak punya posisi aktif
            try:
                if not contracts or float(contracts) <= 0:
                    continue
            except (TypeError, ValueError):
                continue
            snap["positions"].append({
                "side":           p.get("side"),
                "contracts":      contracts,
                "entryPrice":     p.get("entryPrice"),
                "markPrice":      p.get("markPrice"),
                "unrealizedPnl":  p.get("unrealizedPnl"),
                "leverage":       p.get("leverage"),
            })
    except Exception as e:
        snap["errors"].append(f"fetch_positions: {type(e).__name__}: {e}")

    return snap


async def _main() -> None:
    ex = _build_exchange()
    try:
        ccxt_symbol = await _resolve_symbol(ex)
        print(f"[VERIFY] Started. Watching {ccxt_symbol} every {INTERVAL_SEC}s. Output: {OUTPUT_FILE}")
        print(f"[VERIFY] Tekan Ctrl+C untuk stop.\n")

        with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
            # Tulis header session (membantu identifikasi run yang berbeda)
            session_start = {
                "_session_start": _iso_now(),
                "symbol":         ccxt_symbol,
                "interval_sec":   INTERVAL_SEC,
            }
            f.write(json.dumps(session_start) + "\n")
            f.flush()

        while True:
            snap = await _snapshot(ex, ccxt_symbol)
            with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(snap) + "\n")
                f.flush()

            # Cetak ringkas ke terminal supaya kamu bisa monitor
            bal = snap["balance_usdt"]
            pos_count = len(snap["positions"])
            err = "" if not snap["errors"] else f" | ERR: {snap['errors'][0][:60]}"
            print(f"[{snap['timestamp']}] balance={bal} positions={pos_count}{err}")

            await asyncio.sleep(INTERVAL_SEC)
    finally:
        try:
            await ex.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\n[VERIFY] Stopped by user. Final snapshot di " + OUTPUT_FILE)
