# =============================================================================
# tests/test_trade_logger.py — Non-blocking CSV writer untuk trade & equity
# =============================================================================
# Test fokus:
#   - _ensure_csv() create file dengan header kalau belum ada
#   - _write_row() append baris ke CSV
#   - log_trade() / log_equity() normalisasi field & masuk ke queue
#   - shutdown() set flag tanpa crash
#
# Catatan: conftest._mock_external_io monkeypatch log_trade & log_equity ke
# no-op untuk test lain. Di sini kita override ulang pakai fungsi aslinya
# via reference langsung ke module attribute (sebelum monkeypatch dipasang
# di tiap test, kita re-bind).
# =============================================================================
from __future__ import annotations

import csv
import queue

import pytest


@pytest.fixture
def tl(monkeypatch, tmp_path):
    """
    Fresh trade_logger module dengan TRADE_HISTORY_FILE/EQUITY_CURVE_FILE
    redirect ke tmp_path. Tidak start writer thread baru — kita test fungsi
    individual saja (lebih deterministik dari menunggu thread polling).
    """
    import trade_logger as _tl

    trade_file = tmp_path / "trade_history.csv"
    equity_file = tmp_path / "equity_curve.csv"
    monkeypatch.setattr(_tl, "TRADE_HISTORY_FILE", str(trade_file))
    monkeypatch.setattr(_tl, "EQUITY_CURVE_FILE", str(equity_file))

    # Conftest autouse mock log_trade/log_equity ke no-op untuk test lain.
    # Untuk test ini, undo dengan re-bind ke fungsi aslinya yang tersedia
    # via __wrapped__ atau langsung ambil dari modul.
    # Praktek paling sederhana: bersihkan queue sebelum & sesudah test
    # supaya tidak ada bocor dari test lain.
    while not _tl._trade_queue.empty():
        try:
            _tl._trade_queue.get_nowait()
        except queue.Empty:
            break
    while not _tl._equity_queue.empty():
        try:
            _tl._equity_queue.get_nowait()
        except queue.Empty:
            break

    yield _tl

    # Teardown: bersihkan queue lagi
    while not _tl._trade_queue.empty():
        try:
            _tl._trade_queue.get_nowait()
        except queue.Empty:
            break
    while not _tl._equity_queue.empty():
        try:
            _tl._equity_queue.get_nowait()
        except queue.Empty:
            break


# =============================================================================
# _ensure_csv — create file dengan header kalau belum ada
# =============================================================================
class TestEnsureCsv:
    def test_creates_file_with_header_when_missing(self, tl, tmp_path):
        fp = tmp_path / "new.csv"
        tl._ensure_csv(str(fp), ["a", "b", "c"])
        assert fp.exists()
        content = fp.read_text(encoding="utf-8")
        # Header harus muncul di baris pertama
        assert content.startswith("a,b,c")

    def test_does_not_overwrite_existing_file(self, tl, tmp_path):
        fp = tmp_path / "existing.csv"
        fp.write_text("old content\n", encoding="utf-8")
        tl._ensure_csv(str(fp), ["x", "y"])
        # Konten lama harus tetap (file sudah ada → tidak ditimpa)
        assert fp.read_text(encoding="utf-8") == "old content\n"


# =============================================================================
# _write_row — append baris ke CSV
# =============================================================================
class TestWriteRow:
    def test_append_row_to_csv(self, tl, tmp_path):
        fp = tmp_path / "out.csv"
        tl._ensure_csv(str(fp), ["a", "b"])
        tl._write_row(str(fp), ["a", "b"], {"a": 1, "b": 2})
        content = fp.read_text(encoding="utf-8")
        assert "1,2" in content

    def test_extra_fields_ignored(self, tl, tmp_path):
        """csv.DictWriter dipanggil dengan extrasaction='ignore' → field ekstra di-skip."""
        fp = tmp_path / "out.csv"
        tl._ensure_csv(str(fp), ["a", "b"])
        tl._write_row(str(fp), ["a", "b"], {"a": 1, "b": 2, "extra": 999})
        content = fp.read_text(encoding="utf-8")
        # Baris kedua adalah data — split & cek
        lines = content.strip().split("\n")
        assert lines[1] == "1,2"

    def test_multiple_rows_appended(self, tl, tmp_path):
        fp = tmp_path / "out.csv"
        tl._ensure_csv(str(fp), ["a", "b"])
        tl._write_row(str(fp), ["a", "b"], {"a": 1, "b": 2})
        tl._write_row(str(fp), ["a", "b"], {"a": 3, "b": 4})
        rows = list(csv.DictReader(fp.open(encoding="utf-8")))
        assert len(rows) == 2
        assert rows[0]["a"] == "1"
        assert rows[1]["a"] == "3"


# =============================================================================
# log_trade — queue baris yang dinormalisasi
# =============================================================================
class TestLogTrade:
    def _get_real_log_trade(self, tl):
        """Bypass conftest monkeypatch dengan akses langsung ke source code."""
        # Setelah re-import, log_trade di module tetap fungsi asli (monkeypatch
        # scope per-test via fixture, tapi conftest autouse berlaku juga).
        # Solusi: panggil _trade_queue.put manual via fungsi yang me-replicate
        # log_trade. Lebih mudah: monkeypatch reference langsung ke implementasi.
        # Tapi conftest sudah override. Kita panggil putfn manual sebagai approximation
        # — atau lebih bersih: undef monkeypatch dengan reload.
        # Pakai pendekatan paling jujur: panggil fungsi via dict access yang
        # bypass module-level rebind oleh monkeypatch (karena monkeypatch.setattr
        # ke _string path mengubah attribute module).
        import importlib
        importlib.reload(tl)
        return tl.log_trade

    def test_log_trade_puts_normalized_row_in_queue(self, tl):
        """log_trade dinormalisasi ke 9 field standar lalu masuk _trade_queue."""
        # Re-bind log_trade ke implementasi asli — definisi fungsinya
        # ada di source, conftest cuma override binding.
        import importlib
        importlib.reload(tl)
        # Setelah reload, log_trade kembali ke fungsi asli. TRADE_HISTORY_FILE
        # juga balik ke default, jadi kita re-apply patch.
        # Karena writer thread baru di-start lagi setelah reload, kita
        # FOKUS ke queue putting (tidak validasi file output).
        tl.log_trade({
            "timestamp": 12345.0, "symbol": "XRPUSDT", "side": "buy",
            "entry_price": 1.5, "exit_price": None, "qty": 100, "fee": 0.03,
            "pnl": None, "reason": "test",
        })
        # Ambil row dari queue
        row = tl._trade_queue.get(timeout=1)
        assert row["symbol"] == "XRPUSDT"
        assert row["side"] == "buy"
        assert row["entry_price"] == 1.5
        assert row["qty"] == 100
        assert row["reason"] == "test"

    def test_log_trade_defaults_missing_fields(self, tl):
        """Field yang tidak ada → default empty string atau current time."""
        import importlib
        importlib.reload(tl)
        # Minimal dict
        tl.log_trade({"side": "buy"})
        row = tl._trade_queue.get(timeout=1)
        # Field wajib 9 — semua key harus muncul dengan default sensible
        for k in ("timestamp", "symbol", "side", "entry_price", "exit_price",
                  "qty", "fee", "pnl", "reason"):
            assert k in row


# =============================================================================
# log_equity — queue baris equity yang dinormalisasi
# =============================================================================
class TestLogEquity:
    def test_log_equity_puts_normalized_row(self, tl):
        import importlib
        importlib.reload(tl)
        tl.log_equity({
            "timestamp": 999.0, "balance": 1500.0,
            "equity": 1520.0, "unrealized_pnl": 20.0,
        })
        row = tl._equity_queue.get(timeout=1)
        assert row["balance"] == 1500.0
        assert row["equity"] == 1520.0
        assert row["unrealized_pnl"] == 20.0

    def test_log_equity_defaults_missing_fields_to_zero(self, tl):
        import importlib
        importlib.reload(tl)
        tl.log_equity({})  # semua field missing
        row = tl._equity_queue.get(timeout=1)
        assert row["balance"] == 0.0
        assert row["equity"] == 0.0
        assert row["unrealized_pnl"] == 0.0


# =============================================================================
# shutdown — set flag, join thread
# =============================================================================
class TestShutdown:
    def test_shutdown_sets_flag(self, tl):
        """shutdown() men-set _shutdown event supaya writer loop berhenti."""
        import importlib
        importlib.reload(tl)
        # Sebelum: flag belum set (writer thread aktif)
        assert not tl._shutdown.is_set()
        tl.shutdown()
        assert tl._shutdown.is_set()


# =============================================================================
# Writer loop drain — cover queue.get_nowait + _write_row di dalam loop
# =============================================================================
class TestWriterLoopDrain:
    def test_drains_trade_queue_to_csv(self, tl, tmp_path, monkeypatch):
        """Run satu iterasi _writer_thread → queue ke-drain ke file."""
        import threading
        # Pakai path tmp; reload supaya thread baru pakai path baru
        import importlib
        importlib.reload(tl)
        # TestShutdown sebelumnya bisa men-set _shutdown — clear dulu supaya
        # loop kita di bawah benar-benar iterate sebelum di-stop.
        tl._shutdown.clear()
        trade_file = tmp_path / "trades.csv"
        equity_file = tmp_path / "equity.csv"
        monkeypatch.setattr(tl, "TRADE_HISTORY_FILE", str(trade_file))
        monkeypatch.setattr(tl, "EQUITY_CURVE_FILE", str(equity_file))

        # Pasang item di queue
        tl._trade_queue.put({
            "timestamp": 1.0, "symbol": "X", "side": "buy",
            "entry_price": 1.0, "exit_price": "", "qty": 10,
            "fee": 0.01, "pnl": "", "reason": "test",
        })
        tl._equity_queue.put({
            "timestamp": 1.0, "balance": 100.0, "equity": 100.0, "unrealized_pnl": 0.0,
        })

        # Run satu iterasi loop manual (event signaled setelah 1 cycle)
        def _stop_after_one_cycle():
            import time as _t
            _t.sleep(0.05)
            tl._shutdown.set()

        threading.Thread(target=_stop_after_one_cycle, daemon=True).start()
        # _writer_thread akan run 1x lalu exit setelah _shutdown.set()
        tl._writer_thread()

        # Reset shutdown supaya thread kembali bisa dipakai (kebersihan)
        tl._shutdown.clear()

        # File harus ditulis dengan 1 baris data
        assert trade_file.exists()
        rows = list(csv.DictReader(trade_file.open(encoding="utf-8")))
        assert len(rows) == 1
        assert rows[0]["symbol"] == "X"

        assert equity_file.exists()
        equity_rows = list(csv.DictReader(equity_file.open(encoding="utf-8")))
        assert len(equity_rows) == 1
        assert equity_rows[0]["balance"] == "100.0"