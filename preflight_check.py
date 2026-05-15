# =============================================================================
# preflight_check.py — Strategy Validator
# =============================================================================
#
# Dipanggil SEBELUM bot dijalankan oleh main.py.
#
# Cara kerja:
#   1. Cek sintaks strategy.py (ast.parse) → tangkap SyntaxError
#   2. Coba import strategy.py             → tangkap ImportError, NameError, dll
#   3. Pastikan generate_signal() ada      → tangkap jika tidak ada fungsinya
#   4. Jalankan generate_signal(dummy)     → tangkap error logika runtime
#   5. Validasi format return value        → harus dict dengan key "action"
#      dan "action" harus salah satu dari: buy / sell / close / hold
#
# Jika ada masalah → return list[str] berisi pesan error
# Jika semua OK    → return list kosong []
#
# Strategy BEBAS pakai ML atau pure indicator — tidak ada persyaratan khusus.
# =============================================================================

import ast
import os
import sys
import traceback


# ─────────────────────────────────────────────────────────────────────────────
# Data dummy yang akan dikirim ke generate_signal() untuk pengujian
# Format ini sama persis dengan yang dikirim main.py saat live
# ─────────────────────────────────────────────────────────────────────────────
def _make_dummy_data(n_candles: int = 100) -> dict:
    """Buat data candle dummy dengan format persis seperti live data."""
    import random
    random.seed(42)

    base_price = 2.0
    candles = []
    for i in range(n_candles):
        o = base_price + random.uniform(-0.05, 0.05)
        c = o + random.uniform(-0.03, 0.03)
        h = max(o, c) + random.uniform(0, 0.02)
        l = min(o, c) - random.uniform(0, 0.02)
        v = random.uniform(1000, 5000)
        candles.append({
            "timeframe":  "15m",
            "open_time":  1700000000.0 + i * 900,
            "open":       round(o, 5),
            "high":       round(h, 5),
            "low":        round(l, 5),
            "close":      round(c, 5),
            "volume":     round(v, 2),
            "buy_volume": round(v * 0.55, 2),
            "sell_volume":round(v * 0.45, 2),
            "tick_count": 100,
        })

    current = candles[-1]
    return {
        "candles":  {"15m": candles[:-1]},
        "current":  {"15m": current},
        "best_bid": {"price": current["close"] * 0.9999, "qty": 1.0},
        "best_ask": {"price": current["close"] * 1.0001, "qty": 1.0},
        "bid_ask_spread":      current["close"] * 0.0002,
        "orderbook_imbalance": 0.0,
        "volume_delta":        0.0,
        "funding_rate":        0.0001,
        "latest_tick": {
            "price":     current["close"],
            "qty":       1.0,
            "side":      "Buy",
            "timestamp": current["open_time"],
        },
        "is_warmup": False,
    }


# ─────────────────────────────────────────────────────────────────────────────
# VALIDATOR UTAMA
# ─────────────────────────────────────────────────────────────────────────────
def validate_strategy() -> tuple[list[str], list[str]]:
    """
    Jalankan semua pengecekan terhadap strategy.py.

    Returns:
        tuple:
          - errors   (list[str]): Masalah FATAL — bot TIDAK bisa jalan.
          - warnings (list[str]): Saran perbaikan — bot TETAP bisa jalan.
    """
    errors   = []
    warnings = []
    root   = os.path.dirname(os.path.abspath(__file__))
    fpath  = os.path.join(root, "strategy.py")

    # ------------------------------------------------------------------
    # LANGKAH 1: File strategy.py harus ada
    # ------------------------------------------------------------------
    if not os.path.isfile(fpath):
        errors.append(
            "[STRATEGY] ❌ File 'strategy.py' tidak ditemukan di:\n"
            f"   {root}\n"
            "   Buat file strategy.py terlebih dahulu."
        )
        return errors, warnings  # tidak ada gunanya lanjut kalau filenya tidak ada

    # ------------------------------------------------------------------
    # LANGKAH 2: Cek SINTAKS dengan ast.parse
    # Ini tangkap: indentasi salah, kurung tidak menutup, penulisan typo, dll
    # ------------------------------------------------------------------
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            source = f.read()
        ast.parse(source, filename="strategy.py")
    except SyntaxError as e:
        errors.append(
            f"[STRATEGY] ❌ Syntax Error di baris {e.lineno}:\n"
            f"   {(e.text or '').strip()}\n"
            f"   Pesan: {e.msg}"
        )
        return errors, warnings  # syntax error → tidak bisa diimport, stop di sini

    # ------------------------------------------------------------------
    # LANGKAH 3: Coba IMPORT strategy.py
    # Ini tangkap: NameError, ImportError, FileNotFoundError, dll
    # Pasang mock untuk execution & position_manager agar tidak crash
    # ------------------------------------------------------------------
    from unittest.mock import MagicMock
    _injected = []

    for mod in ("execution", "position_manager"):
        if mod not in sys.modules:
            sys.modules[mod] = MagicMock()
            _injected.append(mod)

    # Hapus cache lama agar strategy di-reload fresh
    for key in list(sys.modules.keys()):
        if key in ("strategy", "_strategy_ml_cache"):
            del sys.modules[key]

    if root not in sys.path:
        sys.path.insert(0, root)

    strat = None
    try:
        import strategy as strat
    except SyntaxError as e:
        errors.append(
            f"[STRATEGY] ❌ Syntax Error saat import (baris {e.lineno}):\n"
            f"   {(e.text or '').strip()}\n"
            f"   Pesan: {e.msg}"
        )
    except ImportError as e:
        errors.append(
            f"[STRATEGY] ❌ Import Error — library tidak ditemukan:\n"
            f"   {e}\n"
            f"   Pastikan semua library yang dipakai sudah di-install (pip install ...)."
        )
    except NameError as e:
        errors.append(
            f"[STRATEGY] ❌ Name Error — variabel/fungsi belum didefinisikan:\n"
            f"   {e}"
        )
    except Exception as e:
        tb = traceback.format_exc(limit=5)
        errors.append(
            f"[STRATEGY] ❌ Error saat import strategy.py:\n"
            f"   {type(e).__name__}: {e}\n"
            f"   Detail:\n{tb}"
        )
    finally:
        for mod in _injected:
            sys.modules.pop(mod, None)

    if errors:
        return errors, warnings  # import gagal → tidak bisa lanjut

    # ------------------------------------------------------------------
    # LANGKAH 4: Pastikan fungsi generate_signal() ADA
    # ------------------------------------------------------------------
    if not hasattr(strat, "generate_signal"):
        errors.append(
            "[STRATEGY] ❌ Fungsi 'generate_signal(data)' tidak ditemukan di strategy.py.\n"
            "   Pastikan strategy.py punya fungsi ini:\n"
            "   def generate_signal(data: dict) -> dict: ..."
        )
        return errors, warnings

    if not callable(getattr(strat, "generate_signal")):
        errors.append(
            "[STRATEGY] ❌ 'generate_signal' di strategy.py bukan sebuah fungsi."
        )
        return errors, warnings

    # ------------------------------------------------------------------
    # LANGKAH 5: JALANKAN generate_signal() dengan data dummy
    # Ini tangkap: error logika runtime (IndexError, KeyError,
    #              AttributeError, ZeroDivisionError, dll)
    # ------------------------------------------------------------------
    dummy_data = _make_dummy_data(n_candles=100)

    try:
        result = strat.generate_signal(dummy_data)
    except Exception as e:
        tb = traceback.format_exc(limit=8)
        errors.append(
            f"[STRATEGY] ❌ Error saat menjalankan generate_signal() dengan data dummy:\n"
            f"   {type(e).__name__}: {e}\n"
            f"   Periksa logika di strategy.py.\n"
            f"   Traceback:\n{tb}"
        )
        return errors

    # ------------------------------------------------------------------
    # LANGKAH 6: Validasi FORMAT return value
    # generate_signal() HARUS return dict dengan key "action"
    # "action" HARUS salah satu dari: "buy", "sell", "close", "hold"
    # ------------------------------------------------------------------
    VALID_ACTIONS = {"buy", "sell", "close", "hold"}

    if not isinstance(result, dict):
        errors.append(
            f"[STRATEGY] ❌ generate_signal() harus return sebuah dict.\n"
            f"   Tapi menerima: {type(result).__name__} = {result!r}\n"
            f"   Contoh yang benar: return {{\"action\": \"hold\", \"reason\": \"...\"}}"
        )
        return errors

    if "action" not in result:
        errors.append(
            f"[STRATEGY] ❌ Return value dari generate_signal() harus punya key 'action'.\n"
            f"   Tapi yang diterima: {result!r}\n"
            f"   Contoh yang benar: return {{\"action\": \"hold\", \"reason\": \"...\"}}"
        )
        return errors

    action_val = result.get("action")
    if action_val not in VALID_ACTIONS:
        errors.append(
            f"[STRATEGY] ❌ Nilai 'action' tidak valid: {action_val!r}\n"
            f"   Nilai yang diizinkan: {sorted(VALID_ACTIONS)}\n"
            f"   Pastikan generate_signal() return salah satu dari nilai tersebut."
        )
        return errors

    # ------------------------------------------------------------------
    # LANGKAH 7: on_tick() OPSIONAL (mode advanced)
    # Sejak refactor strategy_runtime, engine otomatis orkestrasi:
    # generate_signal → record_tick → place_order. User tidak wajib
    # nulis on_tick(). Jika ada, dianggap mode advanced (override engine).
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # LANGKAH 8: generate_signal() HARUS return "hold" saat is_warmup=True
    # Selama preload historis, strategy tidak boleh membuka posisi apapun.
    # ------------------------------------------------------------------
    warmup_data = _make_dummy_data(n_candles=100)
    warmup_data["is_warmup"] = True
    try:
        warmup_result = strat.generate_signal(warmup_data)
        warmup_action = warmup_result.get("action", "") if isinstance(warmup_result, dict) else ""
        if warmup_action != "hold":
            errors.append(
                f"[STRATEGY] ❌ generate_signal() return '{warmup_action}' saat is_warmup=True.\n"
                "   Saat bot pertama kali jalan, ia memuat data historis (warmup).\n"
                "   Selama periode ini strategy HARUS return 'hold' agar tidak\n"
                "   membuka posisi sebelum data candle cukup terkumpul.\n"
                "   Tambahkan di awal generate_signal():\n"
                "   if data.get('is_warmup', True):\n"
                "       return {'action': 'hold', 'reason': 'Warmup'}"
            )
    except Exception:
        pass  # error runtime sudah ditangkap di langkah 5

    if errors:
        return errors, warnings

    # ------------------------------------------------------------------
    # LANGKAH 9: generate_signal() tidak boleh SELALU return non-hold
    # Jika dari 5 seed berbeda semuanya return buy/sell, strategi tidak
    # punya filter kondisi → akan membuka posisi di setiap candle.
    # ------------------------------------------------------------------
    import random
    non_hold_count = 0
    SEEDS = [0, 7, 13, 42, 99]
    for seed in SEEDS:
        try:
            random.seed(seed)
            test_data = _make_dummy_data(n_candles=100)
            test_result = strat.generate_signal(test_data)
            if isinstance(test_result, dict) and test_result.get("action") != "hold":
                non_hold_count += 1
        except Exception:
            break

    # ── Langkah 9 → WARNING bukan blocker ─────────────────────────────────
    # Developer scalping atau agresif mungkin memang ingin entry tiap candle.
    # Kita cukup ingatkan, bukan paksa.
    if non_hold_count == len(SEEDS):
        warnings.append(
            f"[STRATEGY] ⚠️  generate_signal() return sinyal aktif pada\n"
            f"   semua {len(SEEDS)} percobaan dengan data dummy berbeda.\n"
            "   Pastikan ini memang disengaja (scalping/agresif)\n"
            "   dan bukan karena lupa menambahkan filter kondisi entry.\n"
            "   Bot TETAP bisa dijalankan."
        )

    if errors:
        return errors, warnings

    # ------------------------------------------------------------------
    # LANGKAH 10: generate_signal() tidak boleh BLOCKING (> 5 detik)
    # Dipanggil dari dalam asyncio event loop — jika blocking, seluruh
    # engine bot (WebSocket, heartbeat, PnL sync) akan membeku.
    # ------------------------------------------------------------------
    import time as _time
    MAX_ALLOWED_SEC = 5.0
    try:
        _t0 = _time.perf_counter()
        strat.generate_signal(_make_dummy_data(n_candles=100))
        _elapsed = _time.perf_counter() - _t0
        if _elapsed > MAX_ALLOWED_SEC:
            errors.append(
                f"[STRATEGY] ❌ generate_signal() membutuhkan {_elapsed:.1f} detik untuk selesai.\n"
                f"   Batas maksimum yang aman: {MAX_ALLOWED_SEC:.0f} detik.\n"
                "   Fungsi ini dipanggil dari dalam asyncio event loop — jika terlalu\n"
                "   lambat, seluruh sistem (WebSocket, heartbeat, PnL) akan membeku.\n"
                "   Periksa apakah ada operasi berat seperti loop besar, I/O file,\n"
                "   atau pemanggilan API di dalam generate_signal()."
            )
    except Exception:
        pass  # error runtime sudah ditangkap di langkah 5

    if errors:
        return errors, warnings

    # ------------------------------------------------------------------
    # LANGKAH 11: Jika user define on_tick() (mode advanced), pastikan
    # tetap merujuk ke execution.place_order(). Mode default tidak butuh
    # cek ini karena strategy_runtime yang panggil place_order otomatis.
    # ------------------------------------------------------------------
    import inspect
    if hasattr(strat, "on_tick") and callable(getattr(strat, "on_tick")):
        try:
            on_tick_src = inspect.getsource(strat.on_tick)
            has_execution = "execution" in on_tick_src or "place_order" in on_tick_src
            if not has_execution:
                errors.append(
                    "[STRATEGY] ❌ on_tick() didefinisikan tapi tidak memanggil "
                    "'execution.place_order()'.\n"
                    "   Karena Anda override on_tick (mode advanced), engine TIDAK\n"
                    "   akan auto-execute — Anda harus panggil place_order() sendiri.\n"
                    "   Solusi: hapus on_tick() (engine handle otomatis), ATAU\n"
                    "   tambahkan di on_tick:\n"
                    "       if signal['action'] != 'hold':\n"
                    "           execution.place_order(signal)"
                )
        except (OSError, TypeError):
            pass  # source tidak bisa dibaca → lewati cek ini

    # ------------------------------------------------------------------
    # LANGKAH 12-15: Analisis AST — deteksi pola berbahaya di strategy.py
    # Tidak perlu menjalankan kode — cukup membaca struktur sintaksnya.
    # ------------------------------------------------------------------
    try:
        _tree = ast.parse(source, filename="strategy.py")

        # Temukan node FunctionDef untuk generate_signal
        _gen_node = next(
            (n for n in ast.walk(_tree)
             if isinstance(n, ast.FunctionDef) and n.name == "generate_signal"),
            None
        )

        # ── Check 12 (BLOCKER): time.sleep() di dalam generate_signal() ────
        # sleep() adalah blocking call — membekukan event loop seluruh bot.
        if _gen_node:
            for _n in ast.walk(_gen_node):
                if isinstance(_n, ast.Call):
                    _f = _n.func
                    _is_sleep = (
                        (isinstance(_f, ast.Attribute) and _f.attr == "sleep") or
                        (isinstance(_f, ast.Name) and _f.id == "sleep")
                    )
                    if _is_sleep:
                        errors.append(
                            f"[STRATEGY] ❌ generate_signal() mengandung 'sleep()' "
                            f"(baris {getattr(_n, 'lineno', '?')}).\n"
                            "   Pemanggilan sleep() di dalam generate_signal() akan\n"
                            "   MEMBEKUKAN seluruh bot (WebSocket, heartbeat, PnL sync)\n"
                            "   karena fungsi ini dipanggil dari asyncio event loop.\n"
                            "   Hapus semua time.sleep() dari generate_signal()."
                        )
                        break

        # ── Check 13 → WARNING bukan blocker ──────────────────────────────
        # Beberapa strategi lanjutan butuh data eksternal (sentimen, news API).
        # Kita ingatkan risikonya tapi tidak memblokir.
        _HTTP_MODS    = {"requests", "urllib", "aiohttp", "httpx"}
        _HTTP_METHODS = {"get", "post", "put", "delete", "request", "fetch", "send"}
        if _gen_node:
            _http_found = False
            for _hn in ast.walk(_gen_node):
                if _http_found:
                    break
                if isinstance(_hn, ast.Call):
                    _hf = _hn.func
                    _flagged = (
                        # requests.get(...)
                        (isinstance(_hf, ast.Attribute) and
                         _hf.attr in _HTTP_METHODS and
                         isinstance(_hf.value, ast.Name) and
                         _hf.value.id in _HTTP_MODS) or
                        # requests.Session().get(...)
                        (isinstance(_hf, ast.Attribute) and
                         _hf.attr in _HTTP_METHODS and
                         isinstance(_hf.value, ast.Call) and
                         isinstance(_hf.value.func, ast.Attribute) and
                         isinstance(_hf.value.func.value, ast.Name) and
                         _hf.value.func.value.id in _HTTP_MODS)
                    )
                    if _flagged:
                        warnings.append(
                            f"[STRATEGY] ⚠️  generate_signal() melakukan HTTP request "
                            f"(baris {getattr(_hn, 'lineno', '?')}).\n"
                            "   Pemanggilan jaringan di setiap tick berisiko membekukan bot\n"
                            "   jika server lambat atau tidak merespons.\n"
                            "   Pertimbangkan memindahkan fetch data ke background task.\n"
                            "   Bot TETAP bisa dijalankan."
                        )
                        _http_found = True


        # ── Check 14 (BLOCKER): sys.exit() / exit() / quit() di strategy ─
        # Jika dipanggil saat trading, seluruh proses bot langsung mati
        # tanpa sempat menutup posisi yang sedang terbuka.
        _EXIT_NAMES = {"exit", "quit"}
        for _n in ast.walk(_tree):
            if isinstance(_n, ast.Call):
                _f = _n.func
                _is_exit = False
                # exit() / quit()
                if isinstance(_f, ast.Name) and _f.id in _EXIT_NAMES:
                    _is_exit = True
                # sys.exit()
                if (isinstance(_f, ast.Attribute) and _f.attr == "exit" and
                        isinstance(_f.value, ast.Name) and _f.value.id == "sys"):
                    _is_exit = True
                # os._exit()
                if (isinstance(_f, ast.Attribute) and _f.attr == "_exit" and
                        isinstance(_f.value, ast.Name) and _f.value.id == "os"):
                    _is_exit = True
                if _is_exit:
                    errors.append(
                        f"[STRATEGY] ❌ strategy.py memanggil exit/sys.exit/os._exit "
                        f"(baris {getattr(_n, 'lineno', '?')}).\n"
                        "   Jika exit() dipanggil saat ada posisi terbuka, bot mati\n"
                        "   seketika tanpa sempat menutup posisi — menyebabkan posisi\n"
                        "   'terparkir' tanpa pengawasan di Bybit.\n"
                        "   Hapus semua pemanggilan exit() dari strategy.py."
                    )
                    break

        # ── Check 15 (WARNING): open() / file I/O di dalam generate_signal() ─
        # Operasi file seperti open(), pd.read_csv() di setiap tick memperlambat
        # strategi dan berpotensi korupsi file jika dua proses menulis bersamaan.
        _FILE_FUNCS = {"open", "read_csv", "to_csv", "read_json", "to_json",
                       "read_excel", "to_excel", "read_parquet"}
        if _gen_node:
            _file_hits = []
            for _n in ast.walk(_gen_node):
                if isinstance(_n, ast.Call):
                    _f = _n.func
                    _fname = None
                    if isinstance(_f, ast.Name):
                        _fname = _f.id
                    elif isinstance(_f, ast.Attribute):
                        _fname = _f.attr
                    if _fname in _FILE_FUNCS:
                        _file_hits.append(getattr(_n, "lineno", "?"))
        # ── Check 15 → WARNING bukan blocker ──────────────────────────────
        # Developer mungkin ingin baca file konfigurasi atau data tambahan.
        # Kita ingatkan risikonya, tidak paksa menghapusnya.
            if _file_hits:
                warnings.append(
                    f"[STRATEGY] ⚠️  generate_signal() melakukan operasi file I/O "
                    f"di baris {_file_hits}.\n"
                    "   Operasi file di setiap tick bisa memperlambat strategi.\n"
                    "   Jika bisa, pindahkan pembacaan file ke level modul (di luar fungsi).\n"
                    "   Bot TETAP bisa dijalankan."
                )

    except SyntaxError:
        pass  # syntax error sudah ditangkap di langkah 2

    if errors:
        return errors, warnings

    # ------------------------------------------------------------------
    # LANGKAH 16 (WARNING): generate_signal() harus tahan data tidak lengkap
    # Simulasikan situasi di mana beberapa key data tidak tersedia
    # (misalnya saat WebSocket baru connect dan data sebagian belum masuk).
    # ------------------------------------------------------------------
    _PARTIAL_CASES = [
        ("tanpa best_bid/best_ask", {k: v for k, v in _make_dummy_data().items()
                                      if k not in ("best_bid", "best_ask")}),
        ("candles kosong",          {**_make_dummy_data(), "candles": {}}),
        ("current kosong",          {**_make_dummy_data(), "current": {}}),
    ]
    # ── Langkah 16 → WARNING bukan blocker ────────────────────────────────
    # Memaksa handle semua edge case terlalu ketat untuk developer pemula.
    # Cukup ingatkan jika ada crash dengan data tidak lengkap.
    for _case_name, _partial_data in _PARTIAL_CASES:
        try:
            strat.generate_signal(_partial_data)
        except Exception as _e:
            warnings.append(
                f"[STRATEGY] ⚠️  generate_signal() crash saat data tidak lengkap "
                f"({_case_name}):\n"
                f"   {type(_e).__name__}: {_e}\n"
                "   Ini bisa terjadi saat WebSocket baru terhubung dan data\n"
                "   sebagian belum tersedia. Pertimbangkan menambahkan guard:\n"
                "   if not data.get('best_bid'): return {'action': 'hold', ...}\n"
                "   Bot TETAP bisa dijalankan."
            )
            break

    return errors, warnings



# =============================================================================
# run() — entry point untuk main.py
# =============================================================================
def run() -> list[str]:
    """
    Entry point untuk main.py — backward compatible.
    Hanya return errors (blocker). Warnings diabaikan di sini
    karena main.py sudah cukup ketat dengan error fatal saja.
    """
    errors, _warnings = validate_strategy()
    return errors


def run_and_print() -> bool:
    """
    Jalankan validasi dan print hasilnya ke terminal.
    Menampilkan errors (blocker) DAN warnings (saran perbaikan).
    Returns True jika tidak ada error fatal.
    """
    print("🔍 Memvalidasi strategy.py...")
    errors, warnings = validate_strategy()

    if warnings:
        print(f"\n⚠️  {len(warnings)} saran perbaikan (bot tetap bisa jalan):")
        for i, w in enumerate(warnings, 1):
            print(f"{'─'*60}")
            print(f"  Saran #{i}:\n{w}")
        print(f"{'─'*60}")

    if not errors:
        print("\n✅ Strategy OK — Tidak ada error fatal. Bot siap dijalankan.\n")
        return True

    print(f"\n❌ Strategy GAGAL VALIDASI — {len(errors)} masalah ditemukan:\n")
    for i, err in enumerate(errors, 1):
        print(f"{'─'*60}")
        print(f"  Masalah #{i}:\n{err}")
    print(f"{'─'*60}")
    print("\n⛔ Bot TIDAK dijalankan. Perbaiki strategy.py terlebih dahulu.\n")
    return False


# =============================================================================
# Jalankan langsung: python preflight_check.py
# =============================================================================
if __name__ == "__main__":
    import sys as _sys
    ok = run_and_print()
    _sys.exit(0 if ok else 1)
