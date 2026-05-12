# =============================================================================
# bot_monitor.py — Health Tracker + Watchdog
# =============================================================================
#
# HEALTH TRACKER (Ide #1):
#   Merekam metrik kesehatan strategy setiap on_tick() dipanggil.
#   Data disimpan ke 'bot_health.json' agar bisa dibaca dashboard.
#
# WATCHDOG TIMER (Ide #2):
#   Memantau apakah on_tick() masih dipanggil secara rutin.
#   Jika tidak ada aktivitas > WATCHDOG_TIMEOUT_SEC, status = FROZEN.
#   Dashboard akan tampilkan peringatan merah.
#
# Cara pakai di strategy.py on_tick():
#   import bot_monitor
#   bot_monitor.record_tick(signal)
# =============================================================================

import json
import os
import time
import threading
from collections import deque

# ─────────────────────────────────────────────────────────────────────────────
# KONFIGURASI
# ─────────────────────────────────────────────────────────────────────────────
HEALTH_FILE               = "bot_health.json"  # file output yang dibaca dashboard
WATCHDOG_TIMEOUT_SEC      = 120               # FROZEN jika > 2 menit tanpa tick
NO_TRADE_WARN_SEC         = 3 * 3600         # WARN jika > 3 jam tidak ada trade
MAX_CONSECUTIVE_ERR       = 5               # ERROR jika error berturut-turut >= N
FLUSH_MIN_INTERVAL        = 3.0            # maks 1x file write per 3 detik

# ── Deteksi #1: Model Drift ────────────────────────────────────────────────
ML_DRIFT_MIN_SAMPLES      = 30    # mulai cek setelah N tick ML terkumpul
ML_DRIFT_STD_THRESHOLD    = 0.005 # std dev < ini → model dianggap stuck

# ── Deteksi #2: Balance Anomaly ────────────────────────────────────────────
BALANCE_DROP_THRESHOLD    = 0.05  # turun > 5% dari equity tertinggi = anomali
BALANCE_HISTORY_MAX       = 120   # simpan 120 snapshot terakhir (~10 menit @5s)

# ── Deteksi #3: Signal Tidak Tereksekusi ────────────────────────────────────
EXECUTION_GRACE_TICKS     = 2     # tunggu N tick sebelum dianggap gagal eksekusi

# ── Deteksi #4: Buffer Overflow ─────────────────────────────────────────────
BUFFER_OVERFLOW_FACTOR    = 1.5   # count > LIMIT * faktor ini = overflow
BUFFER_HARD_LIMIT         = 1500  # hard limit jika LIMIT tidak tersedia

# Hitung sekali dari config — tidak perlu rebuild setiap tick
try:
    from config import TIMEFRAMES as _TIMEFRAMES_CFG
    _TF_SECONDS: dict[str, int] = {tf: v[0] for tf, v in _TIMEFRAMES_CFG.items()}
    _TF_LIMITS:  dict[str, int] = {tf: v[1] for tf, v in _TIMEFRAMES_CFG.items()}
except Exception:
    _TF_SECONDS = {
        "1m": 60,   "3m": 180,  "5m": 300,
        "15m": 900, "30m": 1800,
        "1h": 3600, "2h": 7200, "4h": 14400,
        "6h": 21600, "8h": 28800, "12h": 43200,
        "1d": 86400,
    }
    _TF_LIMITS = {}

# ─────────────────────────────────────────────────────────────────────────────
# STATE IN-MEMORY (direset setiap bot restart)
# ─────────────────────────────────────────────────────────────────────────────
_state = {
    # Waktu
    "start_time":           time.time(),
    "last_tick_time":       0.0,
    "last_trade_time":      0.0,
    "last_error_time":      0.0,

    # Counter sinyal
    "total_ticks":          0,
    "signal_counts": {
        "buy":   0,
        "sell":  0,
        "close": 0,
        "hold":  0,
        "error": 0,
    },

    # Error tracking
    "consecutive_errors":   0,
    "last_error_reason":    "",
    "total_errors":         0,

    # ML tracking
    "last_ml_prob":         None,
    "ml_prob_history":      deque(maxlen=20),   # rolling 20 nilai terakhir

    # Status
    "status":               "STARTING",
    "status_reason":        "",
    "extra_warnings":       [],  # list peringatan tambahan (tidak ubah status utama)

    # Data quality tracking
    "data_quality": {
        "candles_per_tf":       {},
        "last_candle_age_sec":  {},
        "bid_price":            0.0,
        "ask_price":            0.0,
        "bid_valid":            False,
        "ask_valid":            False,
        "spread_pct":           0.0,
        "data_issues":          [],
    },

    # ── Deteksi #1: Model Drift ──────────────────────────────────────────
    "ml_drift_detected":    False,
    "ml_drift_std":         None,

    # ── Deteksi #2: Balance Anomaly ──────────────────────────────────────
    "equity_history":       deque(maxlen=BALANCE_HISTORY_MAX),   # list of (timestamp, equity)
    "equity_peak":          0.0,
    "equity_anomaly":       "",   # string deskripsi anomali jika ada

    # ── Deteksi #3: Signal Tidak Tereksekusi ─────────────────────────────
    "pending_action":       None,  # action yang dikirim, menunggu konfirmasi
    "pending_since_ticks":  0,
    "exec_miss_count":      0,     # berapa kali sinyal tidak tereksekusi
    "last_exec_miss":       "",

    # ── Deteksi #4: Buffer Overflow ──────────────────────────────────────
    "buffer_overflow":      {},   # {"15m": True/False}
}

_lock           = threading.Lock()
_last_flush_time = 0.0   # timestamp terakhir kali file ditulis (untuk throttle)


# ─────────────────────────────────────────────────────────────────────────────
# FUNGSI UTAMA: dipanggil dari strategy.on_tick()
# ─────────────────────────────────────────────────────────────────────────────
def record_tick(signal: dict, ml_prob: float = None, data: dict = None,
                equity: float = None, position_side: str = None):
    """
    Catat satu siklus on_tick().

    Args:
        signal:        dict dari generate_signal() → {"action": ..., "reason": ...}
        ml_prob:       probabilitas ML 0.0–1.0 (opsional)
        data:          dict data mentah untuk cek kualitas (opsional)
        equity:        nilai equity saat ini dari position_manager (opsional)
        position_side: "long" | "short" | "none" dari position_manager (opsional)
    """
    with _lock:
        now    = time.time()
        action = signal.get("action", "hold")
        reason = signal.get("reason", "")

        _state["last_tick_time"] = now
        _state["total_ticks"]   += 1

        # Distribusi sinyal
        is_error = ("Error" in reason or "error" in reason)
        if is_error:
            _state["signal_counts"]["error"] += 1
            _state["consecutive_errors"]     += 1
            _state["last_error_reason"]       = reason
            _state["last_error_time"]         = now
            _state["total_errors"]           += 1
        else:
            key = action if action in _state["signal_counts"] else "hold"
            _state["signal_counts"][key] += 1
            _state["consecutive_errors"]  = 0

        if action in ("buy", "sell", "close"):
            _state["last_trade_time"] = now

        # ML probability
        if ml_prob is not None:
            _state["last_ml_prob"] = round(ml_prob, 4)
            _state["ml_prob_history"].append(round(ml_prob, 4))

        # Data quality
        if data is not None:
            _analyze_data_quality(data, now)

        # ── Deteksi #1: Model Drift ────────────────────────────────────────
        if ml_prob is not None:
            _check_model_drift()

        # ── Deteksi #2: Balance Anomaly ────────────────────────────────────
        if equity is not None:
            _check_balance_anomaly(equity, now)

        # ── Deteksi #3: Signal Tidak Tereksekusi ───────────────────────────
        if position_side is not None:
            _check_signal_execution(action, position_side, now)

        _evaluate_status(now)

    _flush_to_file()


def record_error(reason: str):
    """
    Catat error yang terjadi di luar generate_signal() normal.
    Dipanggil dari except block di on_tick().
    """
    with _lock:
        now = time.time()
        _state["signal_counts"]["error"] += 1
        _state["consecutive_errors"]     += 1
        _state["last_error_reason"]       = reason
        _state["last_error_time"]         = now
        _state["total_errors"]           += 1
        _state["last_tick_time"]          = now
        _evaluate_status(now)
    _flush_to_file()


def _analyze_data_quality(data: dict, now: float):
    """
    Analisa kualitas data yang diterima strategy.
    Dipanggil di dalam _lock dari record_tick().
    """
    dq     = _state["data_quality"]
    issues = []

    # ── 1. Cek candle buffer per timeframe ──────────────────────────────
    candles_dict = data.get("candles", {})
    new_counts   = {}
    new_ages     = {}

    for tf, candle_list in candles_dict.items():
        count = len(candle_list) if candle_list else 0
        new_counts[tf] = count

        if count == 0:
            issues.append(f"Candle buffer '{tf}' KOSONG")
            new_ages[tf] = None
        else:
            # Usia candle terakhir vs waktu sekarang
            last_open_time = candle_list[-1].get("open_time", 0)
            if last_open_time > 0:
                age_sec = round(now - last_open_time)
                new_ages[tf] = age_sec

                expected_interval = _TF_SECONDS.get(tf, 900)
                # Candle dianggap stale jika usianya > 3x interval
                if age_sec > expected_interval * 3:
                    mins = age_sec // 60
                    issues.append(
                        f"Candle '{tf}' STALE — terakhir {mins} menit lalu "
                        f"(seharusnya maks {expected_interval * 3 // 60} menit)"
                    )
            else:
                new_ages[tf] = None
                issues.append(f"Candle '{tf}' tidak punya open_time")

    dq["candles_per_tf"]      = new_counts
    dq["last_candle_age_sec"] = new_ages

    # ── 2. Cek bid/ask ──────────────────────────────────────────────────
    bid = data.get("best_bid", {}).get("price", 0.0)
    ask = data.get("best_ask", {}).get("price", 0.0)

    dq["bid_price"] = round(bid, 6)
    dq["ask_price"] = round(ask, 6)
    dq["bid_valid"] = bid > 0
    dq["ask_valid"] = ask > 0

    if not dq["bid_valid"]:
        issues.append("best_bid = 0 — orderbook belum terima data")
    if not dq["ask_valid"]:
        issues.append("best_ask = 0 — orderbook belum terima data")

    # ── 3. Hitung spread ────────────────────────────────────────────────
    if bid > 0 and ask > 0:
        dq["spread_pct"] = round((ask - bid) / bid * 100, 4)
        if dq["spread_pct"] > 1.0:
            issues.append(f"Spread tidak wajar: {dq['spread_pct']:.4f}%")
    else:
        dq["spread_pct"] = 0.0

    # ── 4. Deteksi Buffer Overflow (Ide #4) ─────────────────────────────
    overflow = {}
    for tf, count in new_counts.items():
        limit = _TF_LIMITS.get(tf, BUFFER_HARD_LIMIT)
        threshold = int(limit * BUFFER_OVERFLOW_FACTOR)
        overflow[tf] = count > threshold
        if overflow[tf]:
            issues.append(
                f"Buffer '{tf}' OVERFLOW: {count} candle "
                f"(limit {limit}, threshold {threshold}) — kemungkinan memory leak"
            )
    _state["buffer_overflow"] = overflow

    dq["data_issues"] = issues


# ─────────────────────────────────────────────────────────────────────────────
# DETEKSI #1 — Model Drift
# ─────────────────────────────────────────────────────────────────────────────
def _check_model_drift():
    """Cek apakah ML model stuck (std dev probabilitas terlalu rendah)."""
    hist = _state["ml_prob_history"]
    if len(hist) < ML_DRIFT_MIN_SAMPLES:
        _state["ml_drift_detected"] = False
        _state["ml_drift_std"]      = None
        return

    mean = sum(hist) / len(hist)
    variance = sum((x - mean) ** 2 for x in hist) / len(hist)
    std = variance ** 0.5
    _state["ml_drift_std"] = round(std, 6)

    if std < ML_DRIFT_STD_THRESHOLD:
        _state["ml_drift_detected"] = True
    else:
        _state["ml_drift_detected"] = False


# ─────────────────────────────────────────────────────────────────────────────
# DETEKSI #2 — Balance Anomaly
# ─────────────────────────────────────────────────────────────────────────────
def _check_balance_anomaly(equity: float, now: float):
    """Cek apakah ada penurunan equity yang tidak wajar."""
    _state["equity_history"].append((now, equity))

    # Update equity peak
    if equity > _state["equity_peak"]:
        _state["equity_peak"] = equity

    peak = _state["equity_peak"]
    if peak > 0 and equity < peak * (1 - BALANCE_DROP_THRESHOLD):
        drop_pct = (peak - equity) / peak * 100
        _state["equity_anomaly"] = (
            f"Equity turun {drop_pct:.1f}% dari peak "
            f"({peak:.2f} → {equity:.2f}) tanpa close trade tercatat"
        )
    else:
        _state["equity_anomaly"] = ""


# ─────────────────────────────────────────────────────────────────────────────
# DETEKSI #3 — Signal Tidak Tereksekusi
# ─────────────────────────────────────────────────────────────────────────────
def _check_signal_execution(action: str, position_side: str, now: float):
    """
    Cek apakah sinyal buy/sell berhasil membuka posisi.
    Logika:
    - Saat action=buy/sell: tandai sebagai pending
    - Tick berikutnya: cek apakah position_side sudah berubah
    - Jika setelah GRACE_TICKS masih belum berubah → MISS
    """
    pending = _state["pending_action"]

    if pending is not None:
        _state["pending_since_ticks"] += 1
        expected = "none"
        if pending == "buy":
            expected = "long"
        elif pending == "sell":
            expected = "short"
        elif pending == "close":
            expected = "none"

        # Jika sudah sesuai — eksekusi berhasil
        if position_side == expected:
            # Perhatian: Tidak melacak atribut `qty`. 
            # Jika user melakukan averaging/pyramiding di state yang sama, bot mengira sukses instan.
            _state["pending_action"]      = None
            _state["pending_since_ticks"] = 0
        # Jika melebihi grace ticks dan belum sesuai
        elif _state["pending_since_ticks"] >= EXECUTION_GRACE_TICKS:
            _state["exec_miss_count"] += 1
            _state["last_exec_miss"]   = (
                f"Sinyal '{pending}' tidak tereksekusi "
                f"(position tetap '{position_side}' setelah {EXECUTION_GRACE_TICKS} tick)"
            )
            _state["pending_action"]      = None
            _state["pending_since_ticks"] = 0

    # Tandai action baru sebagai pending (hanya buy/sell/close)
    if action in ("buy", "sell", "close"):
        _state["pending_action"]      = action
        _state["pending_since_ticks"] = 0


# ─────────────────────────────────────────────────────────────────────────────
# EVALUASI STATUS
# ─────────────────────────────────────────────────────────────────────────────
def _evaluate_status(now: float):
    """Tentukan status bot. Dipanggil dalam _lock."""
    warnings_list = []

    # ── Prioritas 1: Error berturut-turut ───────────────────────────────
    if _state["consecutive_errors"] >= MAX_CONSECUTIVE_ERR:
        _state["status"] = "ERROR"
        _state["status_reason"] = (
            f"{_state['consecutive_errors']} error berturut-turut: "
            f"{_state['last_error_reason']}"
        )
        _state["extra_warnings"] = warnings_list
        return

    # ── Prioritas 2 (WARN): kumpulkan semua peringatan ──────────────────

    # Data issues (candle kosong, bid=0, dll)
    data_issues = _state["data_quality"].get("data_issues", [])
    if data_issues:
        warnings_list.append(f"[Data] {data_issues[0]}")

    # Deteksi #1: Model Drift
    if _state.get("ml_drift_detected"):
        std = _state.get("ml_drift_std", 0)
        warnings_list.append(
            f"[Model Drift] ML prob tidak bergerak "
            f"(std={std:.4f} < threshold {ML_DRIFT_STD_THRESHOLD})"
        )

    # Deteksi #2: Balance Anomaly
    anomaly = _state.get("equity_anomaly", "")
    if anomaly:
        warnings_list.append(f"[Balance] {anomaly}")

    # Deteksi #3: Signal Tidak Tereksekusi
    miss = _state.get("last_exec_miss", "")
    if miss and _state.get("exec_miss_count", 0) > 0:
        warnings_list.append(f"[Eksekusi] {miss}")

    # Deteksi #4: Buffer Overflow (sudah masuk data_issues via _analyze_data_quality)

    # Tidak ada trade lama
    no_trade_secs = now - _state["last_trade_time"] if _state["last_trade_time"] > 0 else 0
    if _state["last_trade_time"] > 0 and no_trade_secs >= NO_TRADE_WARN_SEC:
        hrs = int(no_trade_secs // 3600)
        warnings_list.append(f"[Trade] Tidak ada trade selama {hrs} jam")

    # Set status akhir
    if warnings_list:
        _state["status"]        = "WARN"
        _state["status_reason"] = warnings_list[0]  # tampilkan peringatan pertama
    else:
        _state["status"]        = "OK"
        _state["status_reason"] = ""

    _state["extra_warnings"] = warnings_list


# ─────────────────────────────────────────────────────────────────────────────
# WATCHDOG — dijalankan di thread terpisah oleh main.py
# ─────────────────────────────────────────────────────────────────────────────
_watchdog_thread = None
_watchdog_stop   = threading.Event()


def _watchdog_loop():
    """Thread yang terus memantau apakah on_tick() masih dipanggil."""
    while not _watchdog_stop.is_set():
        time.sleep(10)   # cek setiap 10 detik
        with _lock:
            now           = time.time()
            last_tick     = _state["last_tick_time"]
            ticks_elapsed = now - last_tick if last_tick > 0 else 0

            # Baru dianggap frozen jika pernah menerima tick sebelumnya
            if last_tick > 0 and ticks_elapsed > WATCHDOG_TIMEOUT_SEC:
                _state["status"] = "FROZEN"
                mins = int(ticks_elapsed // 60)
                _state["status_reason"] = (
                    f"Tidak ada activity selama {mins} menit "
                    f"(watchdog timeout: {WATCHDOG_TIMEOUT_SEC}s)"
                )
        _flush_to_file(force=True)   # watchdog harus selalu update, bypass throttle


def start_watchdog():
    """
    Mulai watchdog thread.
    Dipanggil sekali dari main.py setelah bot start.
    """
    global _watchdog_thread
    if _watchdog_thread is not None and _watchdog_thread.is_alive():
        return  # sudah jalan

    _watchdog_stop.clear()
    _watchdog_thread = threading.Thread(
        target=_watchdog_loop,
        name="bot_monitor_watchdog",
        daemon=True,   # ikut mati saat main process selesai
    )
    _watchdog_thread.start()


def stop_watchdog():
    """Hentikan watchdog thread (dipanggil saat graceful shutdown)."""
    _watchdog_stop.set()


# ─────────────────────────────────────────────────────────────────────────────
# TULIS KE FILE (dibaca dashboard) — throttled, maks 1x per FLUSH_MIN_INTERVAL
# ─────────────────────────────────────────────────────────────────────────────
def _flush_to_file(force: bool = False):
    """
    Tulis state ke bot_health.json.
    Throttled: file hanya ditulis jika sudah lewat FLUSH_MIN_INTERVAL detik
    sejak flush terakhir, KECUALI force=True (dipakai watchdog).
    Ini mencegah ratusan disk write per menit saat mode tick aktif.
    """
    global _last_flush_time
    now = time.time()

    # Ambil snapshot data dengan lock penuh untuk cegah race-condition file dan timer
    with _lock:
        if not force and (now - _last_flush_time) < FLUSH_MIN_INTERVAL:
            return   # belum waktunya, skip

        _last_flush_time = now

        avg_prob = None
        if _state["ml_prob_history"]:
            avg_prob = round(sum(_state["ml_prob_history"]) / len(_state["ml_prob_history"]), 4)

        last_trade_secs = (
            round(now - _state["last_trade_time"])
            if _state["last_trade_time"] > 0 else None
        )
        last_tick_secs = (
            round(now - _state["last_tick_time"])
            if _state["last_tick_time"] > 0 else None
        )
        uptime_secs = round(now - _state["start_time"])

        payload = {
            "status":               _state["status"],
            "status_reason":        _state["status_reason"],
            "extra_warnings":       list(_state["extra_warnings"]),
            "last_update":          now,
            "uptime_sec":           uptime_secs,
            "last_tick_sec_ago":    last_tick_secs,
            "last_trade_sec_ago":   last_trade_secs,
            "total_ticks":          _state["total_ticks"],
            "signal_counts":        dict(_state["signal_counts"]),
            "consecutive_errors":   _state["consecutive_errors"],
            "total_errors":         _state["total_errors"],
            "last_error_reason":    _state["last_error_reason"],
            "last_ml_prob":         _state["last_ml_prob"],
            "avg_ml_prob_20":       avg_prob,
            "watchdog_timeout_sec": WATCHDOG_TIMEOUT_SEC,
            "data_quality":         dict(_state["data_quality"]),
            # ── 4 deteksi baru ──────────────────────────────────────
            "ml_drift_detected":    _state["ml_drift_detected"],
            "ml_drift_std":         _state["ml_drift_std"],
            "equity_anomaly":       _state["equity_anomaly"],
            "exec_miss_count":      _state["exec_miss_count"],
            "last_exec_miss":       _state["last_exec_miss"],
            "buffer_overflow":      dict(_state["buffer_overflow"]),
        }

    try:
        tmp = HEALTH_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, HEALTH_FILE)   # atomic write
    except Exception:
        pass   # jangan sampai monitor crash-kan bot


# ─────────────────────────────────────────────────────────────────────────────
# ACCESSOR — dibaca oleh dashboard.py
# ─────────────────────────────────────────────────────────────────────────────
def read_health(path: str = ".") -> dict:
    """
    Baca bot_health.json dari folder bot.
    Dipakai oleh dashboard.py.

    Returns:
        dict health data, atau dict kosong jika file tidak ada.
    """
    fpath = os.path.join(path, HEALTH_FILE)
    try:
        if os.path.exists(fpath):
            with open(fpath, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}
