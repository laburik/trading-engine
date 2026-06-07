# =============================================================================
# backtest_data.py — Data layer untuk backtest & tuning (CCXT, multi-exchange)
# =============================================================================
# Diekstrak dari hypertune.py supaya bisa dipakai bersama oleh backtest.py CLI
# dan hypertune.py. TIDAK ada dependensi ke _MOCK_STATE / strategy harness.
#
# Berisi: download candle historis (pagination), cache CSV, resolve symbol.
# =============================================================================
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import ccxt  # sync CCXT untuk one-shot download historis (bukan ccxt.pro)
import pandas as pd

from config import EXCHANGE

# =============================================================================
# CONSTANTS
# =============================================================================
DATA_DIR = "data"

# CCXT menerima timeframe string langsung ("1m", "15m", "2h", dst).
# Map hanya dipakai untuk validasi + perhitungan cache age + pagination step.
TF_SECONDS_MAP = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200, "1d": 86400,
}


def _ts_str(ts_sec: float) -> str:
    return datetime.fromtimestamp(ts_sec, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def date_to_ms(date_str: str) -> int:
    """
    Parse tanggal 'YYYY-MM-DD' (UTC) → epoch milidetik. Dipakai backtest.py untuk
    START/END. 'end'/'now' (case-insensitive) → waktu sekarang.
    """
    s = str(date_str).strip().lower()
    if s in ("end", "now", ""):
        return int(time.time() * 1000)
    try:
        dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise ValueError(
            f"Tanggal '{date_str}' invalid. Pakai format 'YYYY-MM-DD' atau 'end'."
        ) from e
    return int(dt.timestamp() * 1000)


def _build_ccxt_client():
    """Buat instance CCXT sync untuk one-shot REST fetch (bukan WebSocket)."""
    try:
        cls = getattr(ccxt, EXCHANGE)
    except AttributeError as e:
        print(f"[ERROR] EXCHANGE '{EXCHANGE}' tidak ada di ccxt. "
              f"Lihat https://docs.ccxt.com/#/?id=exchanges")
        raise SystemExit(1) from e
    ex = cls({"enableRateLimit": True, "options": {"defaultType": "linear"}})
    try:
        ex.load_markets()
    except Exception as e:
        print(f"[ERROR] {EXCHANGE}.load_markets() gagal: {e}")
        raise SystemExit(1) from e
    return ex


def _resolve_symbol(ex, raw_symbol: str) -> str:
    """Resolve raw symbol (mis. 'XRPUSDT') ke CCXT unified ('XRP/USDT:USDT')."""
    market = ex.markets_by_id.get(raw_symbol)
    if isinstance(market, list):
        market = market[0]
    if market:
        return market["symbol"]
    # Fallback: anggap user sudah menulis format unified
    return raw_symbol


def _paginate_ohlcv(ex, ccxt_symbol: str, tf: str, start_ms: int, end_ms: int) -> list[list]:
    """
    Fetch OHLCV via pagination (Bybit/CCXT maks 1000 candle/request).
    Loop maju dari start_ms sampai end_ms. Return raw rows (belum dedup/strip).

    Terminasi via: since >= end_ms (sampai tujuan), batch kosong (habis data),
    atau next_since tak maju (anti-infinite). SENGAJA TIDAK break saat
    len(batch) < 1000 — beberapa exchange/TF (mis. Bybit kline 1h/2h) hanya
    mengembalikan ~999 baris per request walau masih banyak data di depan;
    break-by-size dulu menghentikan paginasi terlalu dini (cuma ~999 candle →
    tuning/backtest kekurangan data).
    """
    interval_sec = TF_SECONDS_MAP[tf]
    all_rows: list[list] = []
    since: int = start_ms

    while since < end_ms:
        try:
            batch: list[list] = ex.fetch_ohlcv(ccxt_symbol, timeframe=tf, since=since, limit=1000)
        except Exception as e:
            print(f"[ERROR] {EXCHANGE}.fetch_ohlcv gagal: {e}")
            break

        if not batch:
            break  # tidak ada data lagi → selesai

        all_rows.extend(batch)
        last_ts: int = int(batch[-1][0])
        next_since: int = last_ts + interval_sec * 1000
        if next_since <= since:
            break  # cegah infinite loop kalau exchange return stale data
        since = next_since

    return all_rows


def _rows_to_candles(rows: list[list], tf: str, trim_end_ms: int | None = None) -> list[dict]:
    """Dedup by timestamp, sort, (opsional) trim ke end_ms, strip candle berjalan."""
    interval_sec = TF_SECONDS_MAP[tf]

    seen: set = set()
    uniq: list[list] = []
    for r in sorted(rows, key=lambda r: int(r[0])):
        ts: int = int(r[0])
        if ts in seen:
            continue
        seen.add(ts)
        uniq.append(r)

    # Trim candle yang open_time-nya melewati end_ms (untuk END tanggal tetap).
    # fetch_ohlcv(since=...) bisa overshoot karena batch 1000 candle tak hormati end.
    if trim_end_ms is not None:
        uniq = [r for r in uniq if int(r[0]) <= trim_end_ms]

    # Strip candle yang sedang berjalan (belum tutup)
    if uniq:
        newest_start_ms: int = int(uniq[-1][0])
        if newest_start_ms + interval_sec * 1000 > int(time.time() * 1000):
            uniq = uniq[:-1]

    return [{
        "timeframe":   tf,
        "open_time":   int(r[0]) / 1000.0,
        "open":        float(r[1]),
        "high":        float(r[2]),
        "low":         float(r[3]),
        "close":       float(r[4]),
        "volume":      float(r[5]),
        "buy_volume":  0.0,
        "sell_volume": 0.0,
        "tick_count":  0,
    } for r in uniq]


def download_candles(symbol: str, tf: str, days_back: int) -> list[dict]:
    """Download OHLC via CCXT fetch_ohlcv dengan pagination (window = days_back hari)."""
    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - days_back * 86400 * 1000

    ex = _build_ccxt_client()
    ccxt_symbol = _resolve_symbol(ex, symbol)
    rows = _paginate_ohlcv(ex, ccxt_symbol, tf, start_ms, end_ms)
    return _rows_to_candles(rows, tf)


def download_candles_range(symbol: str, tf: str, start_ms: int, end_ms: int) -> list[dict]:
    """
    Download OHLC untuk rentang [start_ms, end_ms] eksplisit (dipakai backtest.py
    yang punya START/END). end_ms biasanya = sekarang kalau END='end'.
    """
    ex = _build_ccxt_client()
    ccxt_symbol = _resolve_symbol(ex, symbol)
    rows = _paginate_ohlcv(ex, ccxt_symbol, tf, start_ms, end_ms)
    return _rows_to_candles(rows, tf, trim_end_ms=end_ms)


def _bt_cache_path(symbol: str, tf: str, start_ms: int, end_ms: int) -> str:
    """Path cache backtest CLI, di-key oleh RANGE yang diminta (bukan range aktual).
    Prefix '_bt_' supaya TIDAK tertangkap scan cache hypertune (_try_load_cache)."""
    start_d = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).strftime("%Y%m%d")
    end_d   = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).strftime("%Y%m%d")
    fname = f"{EXCHANGE}_bt_{symbol}_{tf}_{start_d}_{end_d}.csv"
    return os.path.join(DATA_DIR, fname)


def download_candles_range_cached(symbol: str, tf: str, start_ms: int, end_ms: int,
                                  allow_cache: bool) -> tuple[list[dict], str]:
    """
    Versi cached dari download_candles_range untuk backtest CLI.
      allow_cache=True  (END tanggal tetap → data immutable): pakai CSV kalau ada,
                        kalau belum → download lalu simpan.
      allow_cache=False (END='end' → data tumbuh): selalu fetch fresh, tidak simpan.
    Return (candles, source_msg).
    """
    path = _bt_cache_path(symbol, tf, start_ms, end_ms)
    if allow_cache and os.path.isfile(path):
        try:
            df = pd.read_csv(path)
            if len(df) > 0:
                return df.to_dict("records"), f"cache hit: {os.path.basename(path)} ({len(df)} candles)"
        except Exception:
            pass  # cache korup → download ulang

    candles = download_candles_range(symbol, tf, start_ms, end_ms)
    if candles and allow_cache:
        if not os.path.exists(DATA_DIR):
            os.makedirs(DATA_DIR)
        try:
            pd.DataFrame(candles).to_csv(path, index=False)
            return candles, f"downloaded + cached: {os.path.basename(path)}"
        except Exception:
            pass
    return candles, "downloaded (fresh, tidak di-cache)"


def save_candles_csv(candles: list[dict], symbol: str, tf: str) -> str:
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
    start_d = datetime.fromtimestamp(candles[0]["open_time"], tz=timezone.utc).strftime("%Y-%m-%d")
    end_d   = datetime.fromtimestamp(candles[-1]["open_time"], tz=timezone.utc).strftime("%Y-%m-%d")
    # Prefix EXCHANGE supaya cache tidak tabrakan kalau user ganti exchange tapi
    # SYMBOL kebetulan sama (mis. "XRPUSDT" exist di bybit & binance).
    fname = f"{EXCHANGE}_{symbol}_{tf}_{start_d}_{end_d}.csv"
    fpath = os.path.join(DATA_DIR, fname)
    pd.DataFrame(candles).to_csv(fpath, index=False)
    return fpath


def _try_load_cache(symbol: str, tf: str, days_back: int,
                    max_age_minutes: float) -> tuple[list[dict] | None, str]:
    """
    Coba load candle dari CSV cache di folder data/ kalau:
      1. Ada file matching <exchange>_<symbol>_<tf>_*.csv
      2. File age < max_age_minutes
      3. Jumlah candle cukup masuk akal untuk days_back (minimal 95% dari expected)

    Return (candles | None, reason).
    """
    if max_age_minutes <= 0:
        return None, "cache disabled (MAX_CACHE_AGE_MINUTES=0)"
    if not os.path.exists(DATA_DIR):
        return None, "data/ folder belum ada"

    prefix = f"{EXCHANGE}_{symbol}_{tf}_"
    candidates: list[tuple[float, str]] = []  # (mtime, full_path)
    try:
        for fname in os.listdir(DATA_DIR):
            if fname.startswith(prefix) and fname.endswith(".csv"):
                fpath = os.path.join(DATA_DIR, fname)
                if os.path.isfile(fpath):
                    candidates.append((os.path.getmtime(fpath), fpath))
    except OSError as e:
        return None, f"read data/ gagal: {e}"

    if not candidates:
        return None, f"tidak ada CSV match {prefix}*.csv"

    candidates.sort(reverse=True)
    latest_mtime, latest_path = candidates[0]
    age_min: float = (time.time() - latest_mtime) / 60.0

    if age_min > max_age_minutes:
        return None, (
            f"cache stale: {os.path.basename(latest_path)} berusia "
            f"{age_min:.1f} menit (limit {max_age_minutes})"
        )

    try:
        df = pd.read_csv(latest_path)
    except Exception as e:
        return None, f"read CSV gagal: {e}"

    expected_total: float = (days_back * 86400.0) / TF_SECONDS_MAP[tf]
    min_acceptable: float = expected_total * 0.95   # toleransi 5%

    if len(df) < min_acceptable:
        return None, (
            f"cache partial: {len(df)} candle, butuh minimal "
            f"{int(min_acceptable)} (expected ~{int(expected_total)})"
        )

    candles: list[dict] = df.to_dict("records")
    return candles, (
        f"hit: {os.path.basename(latest_path)} ({len(candles)} candles, "
        f"age {age_min:.1f}m)"
    )
