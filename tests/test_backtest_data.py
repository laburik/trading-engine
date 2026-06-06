# =============================================================================
# tests/test_backtest_data.py — Unit test untuk engine/backtest_data.py
# =============================================================================
# Tidak ada network: CCXT di-fake (FakeEx) & file I/O diarahkan ke tmp_path.
# =============================================================================
from __future__ import annotations

import os
import re
import time

import pandas as pd
import pytest

import backtest_data as bd


# =============================================================================
# FAKE EXCHANGE (pengganti CCXT, no network)
# =============================================================================
class FakeEx:
    def __init__(self, rows, markets_by_id=None, raise_on_fetch=False):
        self._rows = rows
        self._calls = 0
        self.raise_on_fetch = raise_on_fetch
        self.markets_by_id = (
            markets_by_id if markets_by_id is not None
            else {"XRPUSDT": {"symbol": "XRP/USDT:USDT"}}
        )

    def fetch_ohlcv(self, symbol, timeframe, since, limit):
        self._calls += 1
        if self.raise_on_fetch:
            raise RuntimeError("fetch boom")
        if self._calls == 1:
            return list(self._rows)
        return []


def _rows(n=3, base_ts_ms=1700000000000, interval_sec=3600):
    """OHLCV rows ala CCXT: [ts_ms, o, h, l, c, v]. ts jauh di masa lampau."""
    out = []
    for i in range(n):
        ts = base_ts_ms + i * interval_sec * 1000
        out.append([ts, 1.0 + i, 1.2 + i, 0.9 + i, 1.1 + i, 1000.0 + i])
    return out


# =============================================================================
# date_to_ms
# =============================================================================
class TestDateToMs:
    def test_valid_date(self):
        ms = bd.date_to_ms("2026-04-22")
        # Recompute manual untuk verifikasi
        from datetime import datetime, timezone
        expected = int(datetime(2026, 4, 22, tzinfo=timezone.utc).timestamp() * 1000)
        assert ms == expected

    @pytest.mark.parametrize("token", ["end", "now", "", "END", "Now"])
    def test_end_now_returns_approx_now(self, token):
        before = int(time.time() * 1000)
        ms = bd.date_to_ms(token)
        after = int(time.time() * 1000)
        assert before <= ms <= after + 5

    def test_invalid_raises_valueerror(self):
        with pytest.raises(ValueError):
            bd.date_to_ms("22-04-2026")


# =============================================================================
# _ts_str
# =============================================================================
def test_ts_str_format():
    # Epoch tetap → string UTC tetap. Assert format PERSIS, bukan sekadar "ada '20'".
    s = bd._ts_str(1700000000.0)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", s)   # format persis
    assert s == "2023-11-14 22:13 UTC"                            # nilai UTC tepat


# =============================================================================
# _resolve_symbol
# =============================================================================
class TestResolveSymbol:
    def test_dict_market(self):
        ex = FakeEx(_rows(), markets_by_id={"XRPUSDT": {"symbol": "XRP/USDT:USDT"}})
        assert bd._resolve_symbol(ex, "XRPUSDT") == "XRP/USDT:USDT"

    def test_list_market_takes_first(self):
        ex = FakeEx(_rows(), markets_by_id={"XRPUSDT": [{"symbol": "XRP/USDT:USDT"}]})
        assert bd._resolve_symbol(ex, "XRPUSDT") == "XRP/USDT:USDT"

    def test_not_found_returns_raw(self):
        ex = FakeEx(_rows(), markets_by_id={})
        assert bd._resolve_symbol(ex, "DOGEUSDT") == "DOGEUSDT"


# =============================================================================
# _paginate_ohlcv
# =============================================================================
class TestPaginate:
    def test_single_batch(self):
        ex = FakeEx(_rows(3))
        rows = bd._paginate_ohlcv(ex, "XRP/USDT:USDT", "1h", 1, 10**14)
        assert len(rows) == 3

    def test_empty_batch_breaks(self):
        ex = FakeEx([])
        rows = bd._paginate_ohlcv(ex, "XRP/USDT:USDT", "1h", 1, 10**14)
        assert rows == []

    def test_fetch_exception_breaks(self, capsys):
        ex = FakeEx(_rows(3), raise_on_fetch=True)
        rows = bd._paginate_ohlcv(ex, "XRP/USDT:USDT", "1h", 1, 10**14)
        assert rows == []
        assert "gagal" in capsys.readouterr().out.lower()


# =============================================================================
# _rows_to_candles
# =============================================================================
class TestRowsToCandles:
    def test_basic_conversion(self):
        candles = bd._rows_to_candles(_rows(3), "1h")
        assert len(candles) == 3
        c0 = candles[0]
        assert c0["timeframe"] == "1h"
        assert c0["open_time"] == 1700000000.0
        assert c0["open"] == 1.0
        assert c0["close"] == 1.1
        assert c0["buy_volume"] == 0.0

    def test_dedup_and_sort(self):
        rows = _rows(3)
        # tambah duplikat + acak urutan
        dup = list(rows[1]) ; shuffled = [rows[2], rows[0], rows[1], dup]
        candles = bd._rows_to_candles(shuffled, "1h")
        assert len(candles) == 3                       # duplikat dibuang
        ts = [c["open_time"] for c in candles]
        assert ts == sorted(ts)                        # tersortir

    def test_trim_end_ms(self):
        rows = _rows(4, interval_sec=3600)            # ts: base, +1h, +2h, +3h
        trim = rows[1][0]                              # buang yang > rows[1]
        candles = bd._rows_to_candles(rows, "1h", trim_end_ms=trim)
        assert len(candles) == 2

    def test_strip_running_candle(self):
        # candle terbaru open_time sangat dekat 'now' → di-strip (belum tutup)
        now_ms = int(time.time() * 1000)
        rows = [
            [now_ms - 7200 * 1000, 1, 1, 1, 1, 1],     # 2 jam lalu (closed)
            [now_ms - 60 * 1000,   1, 1, 1, 1, 1],     # 1 menit lalu (masih jalan utk tf 1h)
        ]
        candles = bd._rows_to_candles(rows, "1h")
        assert len(candles) == 1                       # yang berjalan di-strip


# =============================================================================
# download_candles / download_candles_range (orchestrator, ex di-inject)
# =============================================================================
class TestDownload:
    def test_download_candles(self, monkeypatch):
        ex = FakeEx(_rows(3))
        monkeypatch.setattr(bd, "_build_ccxt_client", lambda: ex)
        candles = bd.download_candles("XRPUSDT", "1h", days_back=1)
        assert len(candles) == 3
        assert candles[0]["timeframe"] == "1h"

    def test_download_candles_range(self, monkeypatch):
        ex = FakeEx(_rows(3))
        monkeypatch.setattr(bd, "_build_ccxt_client", lambda: ex)
        candles = bd.download_candles_range("XRPUSDT", "1h", 1, 10**14)
        assert len(candles) == 3


# =============================================================================
# _build_ccxt_client — error path (exchange invalid → SystemExit)
# =============================================================================
def test_build_ccxt_client_invalid_exchange(monkeypatch, capsys):
    monkeypatch.setattr(bd, "EXCHANGE", "no_such_exchange_zzz")
    with pytest.raises(SystemExit):
        bd._build_ccxt_client()
    assert "ERROR" in capsys.readouterr().out


# =============================================================================
# _bt_cache_path
# =============================================================================
def test_bt_cache_path_format():
    path = bd._bt_cache_path("XRPUSDT", "1h", 1700000000000, 1700086400000)
    base = os.path.basename(path)
    assert base.startswith(f"{bd.EXCHANGE}_bt_XRPUSDT_1h_")
    assert base.endswith(".csv")


# =============================================================================
# download_candles_range_cached
# =============================================================================
class TestRangeCached:
    def test_no_cache_when_disallowed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bd, "DATA_DIR", str(tmp_path))
        candles_fake = bd._rows_to_candles(_rows(3), "1h")
        monkeypatch.setattr(bd, "download_candles_range", lambda *a, **k: candles_fake)
        candles, src = bd.download_candles_range_cached("XRPUSDT", "1h", 1, 2, allow_cache=False)
        assert candles == candles_fake
        assert "fresh" in src

    def test_download_then_cache_hit(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bd, "DATA_DIR", str(tmp_path))
        candles_fake = bd._rows_to_candles(_rows(3), "1h")
        monkeypatch.setattr(bd, "download_candles_range", lambda *a, **k: candles_fake)

        # call 1: no file → download + cache
        c1, src1 = bd.download_candles_range_cached("XRPUSDT", "1h", 1700000000000,
                                                    1700086400000, allow_cache=True)
        assert "cached" in src1
        # call 2: file ada → cache hit (download_candles_range diganti agar pasti dari cache)
        monkeypatch.setattr(bd, "download_candles_range",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not download")))
        c2, src2 = bd.download_candles_range_cached("XRPUSDT", "1h", 1700000000000,
                                                    1700086400000, allow_cache=True)
        assert "cache hit" in src2
        assert len(c2) == 3


# =============================================================================
# save_candles_csv
# =============================================================================
def test_save_candles_csv(monkeypatch, tmp_path):
    monkeypatch.setattr(bd, "DATA_DIR", str(tmp_path))
    candles = bd._rows_to_candles(_rows(3), "1h")
    path = bd.save_candles_csv(candles, "XRPUSDT", "1h")
    assert os.path.isfile(path)
    df = pd.read_csv(path)
    assert len(df) == 3


# =============================================================================
# _try_load_cache
# =============================================================================
class TestTryLoadCache:
    def _write_cache(self, tmp_path, tf, n_rows):
        candles = bd._rows_to_candles(_rows(n_rows), tf)
        fname = f"{bd.EXCHANGE}_XRPUSDT_{tf}_2026-01-01_2026-01-02.csv"
        fpath = os.path.join(str(tmp_path), fname)
        pd.DataFrame(candles).to_csv(fpath, index=False)
        return fpath

    def test_disabled_when_age_zero(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bd, "DATA_DIR", str(tmp_path))
        out, reason = bd._try_load_cache("XRPUSDT", "1h", 1, max_age_minutes=0)
        assert out is None and "disabled" in reason

    def test_data_dir_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bd, "DATA_DIR", os.path.join(str(tmp_path), "ghost"))
        out, reason = bd._try_load_cache("XRPUSDT", "1h", 1, max_age_minutes=60)
        assert out is None and "belum ada" in reason

    def test_no_matching_csv(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bd, "DATA_DIR", str(tmp_path))
        out, reason = bd._try_load_cache("XRPUSDT", "1h", 1, max_age_minutes=60)
        assert out is None and "tidak ada CSV" in reason

    def test_cache_hit_fresh_enough(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bd, "DATA_DIR", str(tmp_path))
        # days_back=1, tf=1h → expected 24, min ~22.8. Tulis 24 baris.
        self._write_cache(tmp_path, "1h", 24)
        out, reason = bd._try_load_cache("XRPUSDT", "1h", 1, max_age_minutes=60)
        assert out is not None
        assert "hit" in reason
        assert len(out) == 24

    def test_cache_stale(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bd, "DATA_DIR", str(tmp_path))
        fpath = self._write_cache(tmp_path, "1h", 24)
        old = time.time() - 3600 * 5          # 5 jam lalu
        os.utime(fpath, (old, old))
        out, reason = bd._try_load_cache("XRPUSDT", "1h", 1, max_age_minutes=60)
        assert out is None and "stale" in reason

    def test_cache_partial(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bd, "DATA_DIR", str(tmp_path))
        self._write_cache(tmp_path, "1h", 3)   # jauh di bawah min ~22.8
        out, reason = bd._try_load_cache("XRPUSDT", "1h", 1, max_age_minutes=60)
        assert out is None and "partial" in reason
