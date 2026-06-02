# =============================================================================
# tests/test_preflight_check.py — Validator strategy.py sebelum bot jalan
# =============================================================================
# preflight_check.validate_strategy() me-load strategy.py dari folder yang
# sama dengan __file__ preflight_check. Untuk test, kita patch __file__ ke
# tmp_path dan tulis strategy.py dummy yang macam-macam (valid, syntax error,
# missing function, dst) supaya semua cabang validator ke-cover.
# =============================================================================
from __future__ import annotations

import sys
import textwrap

import pytest


# -----------------------------------------------------------------------------
# Helper: tulis strategy.py dummy ke tmp_path, lalu patch preflight_check
# supaya menganggap tmp_path sebagai folder asalnya. Setelah patch, validator
# akan baca strategy.py dari tmp_path (bukan dari engine/ asli).
# -----------------------------------------------------------------------------
@pytest.fixture
def preflight_in_tmpdir(tmp_path, monkeypatch):
    """Return tuple (preflight_check_module, write_strategy_fn)."""
    import preflight_check as pf

    # Pindah folder kerja preflight_check ke tmp_path/engine/ (via __file__).
    # validate_strategy() resolves project_root = dirname(dirname(__file__)) lalu
    # root = project_root/user (strategy.py user ada di user/ sejak reorg). Jadi
    # __file__ harus 2 level di bawah project root (mirror struktur asli).
    engine_dir = tmp_path / "engine"
    engine_dir.mkdir(exist_ok=True)
    fake_file = str(engine_dir / "preflight_check.py")
    monkeypatch.setattr(pf, "__file__", fake_file)

    # Bersihkan import cache sebelum & sesudah tiap test
    for k in list(sys.modules.keys()):
        if k in ("strategy", "_strategy_ml_cache"):
            del sys.modules[k]

    def write_strategy(source: str) -> None:
        user_dir = tmp_path / "user"
        user_dir.mkdir(exist_ok=True)
        fpath = user_dir / "strategy.py"
        fpath.write_text(textwrap.dedent(source), encoding="utf-8")

    yield pf, write_strategy

    # Teardown: hapus strategy module supaya test berikutnya fresh
    for k in list(sys.modules.keys()):
        if k in ("strategy", "_strategy_ml_cache"):
            del sys.modules[k]
    # Hapus tmp_path/user dari sys.path supaya tidak bocor antar test
    user_str = str(tmp_path / "user")
    if user_str in sys.path:
        sys.path.remove(user_str)


# Strategy "valid" minimum — di-pakai banyak test sebagai baseline
_VALID_STRATEGY = """\
def generate_signal(data):
    if data.get('is_warmup', True):
        return {"action": "hold", "reason": "warmup"}
    # Pakai sedikit logic supaya tidak selalu non-hold di 5 seed
    candles = data.get("candles", {})
    if not candles:
        return {"action": "hold", "reason": "no candles"}
    first_tf = next(iter(candles.values()), [])
    if len(first_tf) < 60:
        return {"action": "hold", "reason": "wait"}
    return {"action": "hold", "reason": "neutral"}
"""


# =============================================================================
# LANGKAH 1 — FILE TIDAK ADA
# =============================================================================
class TestFileMissing:
    def test_missing_strategy_py_returns_error(self, preflight_in_tmpdir):
        pf, _ = preflight_in_tmpdir
        # Tidak tulis strategy.py
        errors, warnings = pf.validate_strategy()
        assert errors, "Should error when strategy.py missing"
        assert any("tidak ditemukan" in e for e in errors)


# =============================================================================
# LANGKAH 2 — SYNTAX ERROR
# =============================================================================
class TestSyntaxError:
    def test_syntax_error_is_caught(self, preflight_in_tmpdir):
        pf, write = preflight_in_tmpdir
        write("def generate_signal(data:\n    return {'action': 'hold'}\n")  # missing )
        errors, _ = pf.validate_strategy()
        assert errors
        assert any("Syntax Error" in e for e in errors)


# =============================================================================
# LANGKAH 3 — IMPORT ERROR (NameError, ImportError, generic)
# =============================================================================
class TestImportFailures:
    def test_import_error_library_missing(self, preflight_in_tmpdir):
        pf, write = preflight_in_tmpdir
        write("import library_yang_tidak_ada_xyz\n\ndef generate_signal(data):\n    return {'action': 'hold'}\n")
        errors, _ = pf.validate_strategy()
        assert errors
        # ImportError handler memanggilnya "Import Error"
        assert any("Import Error" in e or "import" in e.lower() for e in errors)

    def test_name_error_at_module_level(self, preflight_in_tmpdir):
        pf, write = preflight_in_tmpdir
        write("undefined_name + 1\n\ndef generate_signal(data):\n    return {'action': 'hold'}\n")
        errors, _ = pf.validate_strategy()
        assert errors

    def test_generic_exception_at_import(self, preflight_in_tmpdir):
        pf, write = preflight_in_tmpdir
        # ZeroDivisionError pas module-level evaluation
        write("x = 1 / 0\n\ndef generate_signal(data):\n    return {'action': 'hold'}\n")
        errors, _ = pf.validate_strategy()
        assert errors
        assert any("ZeroDivisionError" in e or "import" in e.lower() for e in errors)


# =============================================================================
# LANGKAH 4 — generate_signal MISSING / BUKAN FUNCTION
# =============================================================================
class TestMissingFunction:
    def test_no_generate_signal_returns_error(self, preflight_in_tmpdir):
        pf, write = preflight_in_tmpdir
        write("x = 42  # tidak ada generate_signal\n")
        errors, _ = pf.validate_strategy()
        assert errors
        assert any("generate_signal" in e for e in errors)

    def test_generate_signal_not_callable(self, preflight_in_tmpdir):
        pf, write = preflight_in_tmpdir
        write("generate_signal = 'i am not a function'\n")
        errors, _ = pf.validate_strategy()
        assert errors
        assert any("bukan sebuah fungsi" in e or "generate_signal" in e for e in errors)


# =============================================================================
# LANGKAH 5 — RUNTIME ERROR DI generate_signal()
# =============================================================================
class TestRuntimeErrors:
    def test_index_error_at_runtime(self, preflight_in_tmpdir):
        pf, write = preflight_in_tmpdir
        write("def generate_signal(data):\n    return data['candles']['nonexistent_tf'][999]\n")
        errors, _ = pf.validate_strategy()
        assert errors
        # Boleh KeyError atau IndexError, tergantung dummy data
        assert any(
            "Error saat menjalankan" in e or "KeyError" in e or "IndexError" in e
            for e in errors
        )

    def test_zero_division_at_runtime(self, preflight_in_tmpdir):
        pf, write = preflight_in_tmpdir
        write("def generate_signal(data):\n    return {'action': 'hold', 'x': 1/0}\n")
        errors, _ = pf.validate_strategy()
        assert errors
        assert any("ZeroDivisionError" in e for e in errors)


# =============================================================================
# LANGKAH 6 — VALIDASI RETURN VALUE FORMAT
# =============================================================================
class TestReturnFormat:
    def test_non_dict_return_is_error(self, preflight_in_tmpdir):
        pf, write = preflight_in_tmpdir
        write("def generate_signal(data):\n    return 'not a dict'\n")
        errors, _ = pf.validate_strategy()
        assert errors
        assert any("harus return sebuah dict" in e for e in errors)

    def test_dict_without_action_key(self, preflight_in_tmpdir):
        pf, write = preflight_in_tmpdir
        write("def generate_signal(data):\n    return {'reason': 'oops'}\n")
        errors, _ = pf.validate_strategy()
        assert errors
        assert any("key 'action'" in e for e in errors)

    def test_invalid_action_value(self, preflight_in_tmpdir):
        pf, write = preflight_in_tmpdir
        write("def generate_signal(data):\n    return {'action': 'banana'}\n")
        errors, _ = pf.validate_strategy()
        assert errors
        assert any("tidak valid" in e for e in errors)


# =============================================================================
# LANGKAH 8 — WARMUP HARUS RETURN HOLD
# =============================================================================
class TestWarmupGuard:
    def test_strategy_that_trades_during_warmup_errors(self, preflight_in_tmpdir):
        pf, write = preflight_in_tmpdir
        write("""\
            def generate_signal(data):
                return {"action": "buy", "reason": "selalu beli"}
        """)
        errors, _ = pf.validate_strategy()
        assert errors
        # Salah satunya pasti tentang warmup
        joined = "\n".join(errors)
        assert "warmup" in joined.lower() or "is_warmup" in joined or "always" in joined.lower()


# =============================================================================
# LANGKAH 9 — STRATEGY YANG SELALU NON-HOLD → WARNING
# =============================================================================
class TestAlwaysActiveWarning:
    def test_always_buy_after_warmup_triggers_warning(self, preflight_in_tmpdir):
        pf, write = preflight_in_tmpdir
        # Strategy yang lulus warmup check tapi selalu return buy setelahnya
        write("""\
            def generate_signal(data):
                if data.get("is_warmup", True):
                    return {"action": "hold", "reason": "warmup"}
                return {"action": "buy", "reason": "selalu beli"}
        """)
        errors, warnings = pf.validate_strategy()
        # Errors tidak boleh blocker (lulus warmup check), tapi warning soal
        # selalu-non-hold harus muncul.
        assert any("semua" in w.lower() and "percobaan" in w.lower() for w in warnings)


# =============================================================================
# LANGKAH 12 — sleep() DI DALAM generate_signal (BLOCKER)
# =============================================================================
class TestSleepDetection:
    def test_time_sleep_in_generate_signal_is_blocker(self, preflight_in_tmpdir):
        pf, write = preflight_in_tmpdir
        write("""\
            import time
            def generate_signal(data):
                time.sleep(0.001)
                return {"action": "hold", "reason": "x"}
        """)
        errors, _ = pf.validate_strategy()
        assert any("sleep" in e.lower() for e in errors)


# =============================================================================
# LANGKAH 13 — HTTP REQUEST DI generate_signal → WARNING
# =============================================================================
class TestHttpWarning:
    def test_requests_get_in_generate_signal_triggers_warning(self, preflight_in_tmpdir):
        pf, write = preflight_in_tmpdir
        # Pakai requests.get tapi tidak benar2 dipanggil (cuma teks AST).
        # generate_signal sendiri tetap return hold supaya tidak crash.
        write("""\
            import requests  # noqa
            def generate_signal(data):
                if data.get("is_warmup", True):
                    return {"action": "hold", "reason": "warmup"}
                if False:
                    requests.get("https://example.com")  # dead code, hanya untuk AST scan
                return {"action": "hold", "reason": "ok"}
        """)
        errors, warnings = pf.validate_strategy()
        assert any("HTTP" in w or "http" in w.lower() for w in warnings)


# =============================================================================
# LANGKAH 14 — sys.exit() / exit() / os._exit() (BLOCKER)
# =============================================================================
class TestExitDetection:
    def test_sys_exit_at_module_level_is_blocker(self, preflight_in_tmpdir):
        pf, write = preflight_in_tmpdir
        write("""\
            import sys
            def generate_signal(data):
                if False:
                    sys.exit(0)  # dead branch, hanya untuk AST
                return {"action": "hold", "reason": "x"}
        """)
        errors, _ = pf.validate_strategy()
        assert any("exit" in e.lower() for e in errors)

    def test_bare_exit_call_is_blocker(self, preflight_in_tmpdir):
        pf, write = preflight_in_tmpdir
        write("""\
            def generate_signal(data):
                if False:
                    exit()
                return {"action": "hold", "reason": "x"}
        """)
        errors, _ = pf.validate_strategy()
        assert any("exit" in e.lower() for e in errors)


# =============================================================================
# LANGKAH 15 — FILE I/O DI generate_signal → WARNING
# =============================================================================
class TestFileIoWarning:
    def test_open_in_generate_signal_triggers_warning(self, preflight_in_tmpdir):
        pf, write = preflight_in_tmpdir
        write("""\
            def generate_signal(data):
                if data.get("is_warmup", True):
                    return {"action": "hold", "reason": "warmup"}
                if False:
                    open("dummy.txt")  # dead code untuk AST
                return {"action": "hold", "reason": "ok"}
        """)
        errors, warnings = pf.validate_strategy()
        assert any("file" in w.lower() or "I/O" in w for w in warnings)


# =============================================================================
# LANGKAH 11 — on_tick() ADA TAPI TIDAK PANGGIL place_order (BLOCKER)
# =============================================================================
class TestOnTickValidation:
    def test_on_tick_without_place_order_errors(self, preflight_in_tmpdir):
        pf, write = preflight_in_tmpdir
        write("""\
            def generate_signal(data):
                return {"action": "hold", "reason": "x"}

            def on_tick(data):
                pass
        """)
        errors, _ = pf.validate_strategy()
        assert any("on_tick" in e and "place_order" in e for e in errors)

    def test_on_tick_with_place_order_ok(self, preflight_in_tmpdir):
        pf, write = preflight_in_tmpdir
        write("""\
            def generate_signal(data):
                if data.get("is_warmup", True):
                    return {"action": "hold", "reason": "warmup"}
                return {"action": "hold", "reason": "neutral"}

            def on_tick(data):
                import execution
                signal = generate_signal(data)
                if signal["action"] != "hold":
                    execution.place_order(signal)
        """)
        errors, _ = pf.validate_strategy()
        # on_tick valid (mention place_order) → tidak boleh error tentang on_tick
        assert not any("on_tick" in e for e in errors)


# =============================================================================
# HAPPY PATH — strategy valid harus return (errors=[], warnings=[])
# =============================================================================
class TestHappyPath:
    def test_valid_strategy_no_errors(self, preflight_in_tmpdir):
        pf, write = preflight_in_tmpdir
        write(_VALID_STRATEGY)
        errors, _ = pf.validate_strategy()
        assert errors == [], f"Expected no errors, got: {errors}"


# =============================================================================
# RUN() WRAPPER — backward-compatible API
# =============================================================================
class TestRunWrapper:
    def test_run_returns_errors_only(self, preflight_in_tmpdir):
        pf, write = preflight_in_tmpdir
        write(_VALID_STRATEGY)
        result = pf.run()
        # run() return list[str] — kosong kalau OK
        assert isinstance(result, list)
        assert result == []

    def test_run_returns_errors_when_broken(self, preflight_in_tmpdir):
        pf, write = preflight_in_tmpdir
        write("def generate_signal(data):\n    return 'not a dict'\n")
        result = pf.run()
        assert isinstance(result, list)
        assert len(result) > 0
