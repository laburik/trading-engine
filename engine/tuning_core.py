# =============================================================================
# tuning_core.py — Mesin hyperparameter tuning (grid expand + search + ranking)
# =============================================================================
# Diekstrak dari hypertune.py. Bagian yang SPESIFIK tuning saja — simulator &
# metrik diwarisi dari backtest_core/backtest_metrics (satu sumber, nol drift).
#
# Penting: import backtest_core lebih dulu supaya mock position_manager/execution
# terpasang ke sys.modules SEBELUM strategy di-load.
# =============================================================================
from __future__ import annotations

import itertools
import sys
import time
from typing import Any

import numpy as np

# Core bersama (backtest_core memasang mock saat di-import — biarkan paling atas).
from backtest_core import (
    load_strategy, patch_params, simulate_single, simulate_multi,
)
from backtest_metrics import compute_metrics, print_metric_block
from backtest_data import (
    download_candles, save_candles_csv, _try_load_cache, _ts_str, TF_SECONDS_MAP,
)

from config import SYMBOL, FEE_RATE, INITIAL_BALANCE, ORDER_SIZE_USDT, LEVERAGE
import tuning_config as tc

# =============================================================================
# CONSTANTS
# =============================================================================
TOP_N_VALIDATE = 10
TOP_N_DISPLAY  = 3


# =============================================================================
# CONFIG VALIDATION
# =============================================================================
def validate_config() -> None:
    required = ["STRATEGY_FILE", "TIMEFRAME", "DAYS_BACK", "OPTIM_MODE", "POSITION_MODEL", "PARAMS"]
    missing = [a for a in required if not hasattr(tc, a)]
    if missing:
        print(f"[ERROR] tuning_config.py kurang field: {missing}")
        sys.exit(1)
    if tc.TIMEFRAME not in TF_SECONDS_MAP:
        print(f"[ERROR] TIMEFRAME '{tc.TIMEFRAME}' invalid. Valid: {list(TF_SECONDS_MAP.keys())}")
        sys.exit(1)
    if tc.OPTIM_MODE not in ("profit", "drawdown", "balanced"):
        print(f"[ERROR] OPTIM_MODE harus profit|drawdown|balanced, dapat: '{tc.OPTIM_MODE}'")
        sys.exit(1)
    if tc.POSITION_MODEL not in ("single", "multi"):
        print(f"[ERROR] POSITION_MODEL harus single|multi, dapat: '{tc.POSITION_MODEL}'")
        sys.exit(1)
    if not isinstance(tc.PARAMS, dict) or not tc.PARAMS:
        print(f"[ERROR] PARAMS harus dict non-empty.")
        sys.exit(1)
    # Tiap key PARAMS harus string non-empty, tiap value list non-empty (spec
    # range [start,end,n] atau discrete list). Cegah typo struktur sebelum expand.
    for key, spec in tc.PARAMS.items():
        if not isinstance(key, str) or not key:
            print(f"[ERROR] PARAMS punya key tidak valid: {key!r} (harus string non-empty).")
            sys.exit(1)
        if not isinstance(spec, list) or len(spec) == 0:
            print(f"[ERROR] PARAMS['{key}'] harus list non-empty "
                  f"(range [start,end,n] atau discrete list), dapat: {spec!r}.")
            sys.exit(1)
    if not isinstance(tc.DAYS_BACK, int) or isinstance(tc.DAYS_BACK, bool) or tc.DAYS_BACK < 1:
        print(f"[ERROR] DAYS_BACK harus integer >= 1.")
        sys.exit(1)
    if not isinstance(tc.STRATEGY_FILE, str) or not tc.STRATEGY_FILE:
        print(f"[ERROR] STRATEGY_FILE harus string non-empty.")
        sys.exit(1)
    if tc.STRATEGY_FILE.endswith(".py"):
        print(f"[ERROR] STRATEGY_FILE cukup nama modul tanpa '.py' (mis. 'strategy').")
        sys.exit(1)
    mca = getattr(tc, "MAX_CACHE_AGE_MINUTES", None)
    if mca is not None and (not isinstance(mca, (int, float)) or isinstance(mca, bool) or mca < 0):
        print(f"[ERROR] MAX_CACHE_AGE_MINUTES harus angka >= 0, dapat: {mca!r}.")
        sys.exit(1)

    # --- Opsional: grid multi-timeframe ---
    tfg = getattr(tc, "TUNE_TIMEFRAMES", None)
    if tfg is not None:
        if not isinstance(tfg, list):
            print(f"[ERROR] TUNE_TIMEFRAMES harus list TF (mis. ['15m','1h','2h']) atau [] untuk pakai TIMEFRAME tunggal.")
            sys.exit(1)
        bad = [t for t in tfg if t not in TF_SECONDS_MAP]
        if bad:
            print(f"[ERROR] TUNE_TIMEFRAMES berisi TF invalid: {bad}. Valid: {list(TF_SECONDS_MAP.keys())}")
            sys.exit(1)

    # --- Opsional: percepatan tuning (windowing) ---
    ft = getattr(tc, "FAST_TUNING", False)
    if not isinstance(ft, bool):
        print(f"[ERROR] FAST_TUNING harus True/False (bool), dapat: {type(ft).__name__}.")
        sys.exit(1)
    fw = getattr(tc, "FAST_WINDOW", 0)
    if not isinstance(fw, int) or isinstance(fw, bool) or fw < 0:
        print(f"[ERROR] FAST_WINDOW harus integer >= 0 (0 = auto), dapat: {fw!r}.")
        sys.exit(1)

    # --- Opsional: rasio split in-sample/out-of-sample ---
    osp = getattr(tc, "OOS_SPLIT", 5)
    if not isinstance(osp, int) or isinstance(osp, bool) or osp < 2:
        print(f"[ERROR] OOS_SPLIT harus integer >= 2 (test = 1/N), dapat: {osp!r}.")
        sys.exit(1)

    # --- Opsional: walk-forward (rolling window) ---
    wf = getattr(tc, "WALK_FORWARD", False)
    if not isinstance(wf, bool):
        print(f"[ERROR] WALK_FORWARD harus True/False (bool), dapat: {type(wf).__name__}.")
        sys.exit(1)
    if wf:
        for k in ("WF_WINDOW_DAYS", "WF_STEP_DAYS"):
            v = getattr(tc, k, None)
            if not isinstance(v, int) or isinstance(v, bool) or v < 1:
                print(f"[ERROR] {k} harus integer >= 1 (WALK_FORWARD aktif), dapat: {v!r}.")
                sys.exit(1)


def validate_params_in_strategy(strategy_module, params: dict | None = None) -> None:
    """Pastikan tiap key di PARAMS BENAR-BENAR ada sebagai variabel di strategy file.

    Tuning param yang tidak dibaca strategy = sia-sia: nilai di-set lewat
    setattr (patch_params) tapi kode strategi tak pernah membacanya → grid
    search jalan tapi hasilnya identik semua (silent no-op). Lebih baik blokir
    dengan pesan jelas daripada user bingung kenapa tuning "tidak ngefek".
    Dipanggil SETELAH load_strategy (butuh modul yang sudah ter-load).
    """
    if params is None:
        params = tc.PARAMS
    missing = [k for k in params if not hasattr(strategy_module, k)]
    if missing:
        available = sorted(
            n for n in dir(strategy_module)
            if n.isupper() and not n.startswith("_")
        )
        print(f"[ERROR] Parameter PARAMS ini tidak ada di {tc.STRATEGY_FILE}.py: {missing}")
        print(f"        Param yang tersedia di strategy : {available}")
        print(f"        Cek ejaan/huruf besar-kecil (case-sensitive), atau deklarasikan")
        print(f"        variabel itu di atas {tc.STRATEGY_FILE}.py (gaya input MQL5).")
        sys.exit(1)


# =============================================================================
# PARAM EXPANSION
# =============================================================================
def expand_param(name: str, spec: Any) -> list[Any]:
    """
    [start, end, N] dengan semua numeric (bukan bool) + N int >=1 → linspace.
    Selain itu (panjang lain, atau ada bool/string) → discrete list apa adanya.
    """
    if not isinstance(spec, list) or len(spec) == 0:
        raise ValueError(f"PARAMS['{name}']: harus list non-empty")

    is_range = (
        len(spec) == 3
        and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in spec[:2])
        and isinstance(spec[2], int)
        and not isinstance(spec[2], bool)
        and spec[2] >= 1
    )

    if is_range:
        start, end, n = spec
        if n == 1:
            return [start]
        is_int_pair = isinstance(start, int) and isinstance(end, int)
        values = np.linspace(start, end, n).tolist()
        if is_int_pair:
            values = [int(round(v)) for v in values]
            seen: set = set()
            uniq: list = []
            for v in values:
                if v not in seen:
                    seen.add(v)
                    uniq.append(v)
            return uniq
        return [round(float(v), 10) for v in values]

    return list(spec)


def expand_grid(params: dict) -> tuple[list[str], list[tuple]]:
    """Return (names, list of combo tuples)."""
    names = list(params.keys())
    expanded = [expand_param(k, params[k]) for k in names]
    combos = list(itertools.product(*expanded))
    return names, combos


# =============================================================================
# SCORING
# =============================================================================
def score_metrics(metrics: dict, mode: str) -> float:
    if metrics["num_trades"] < 3:
        return float("-inf")
    if mode == "profit":
        return metrics["pnl"]
    if mode == "drawdown":
        return -metrics["max_dd_pct"]
    if mode == "balanced":
        return metrics["sharpe"]
    return 0.0


# =============================================================================
# GRID SEARCH ORCHESTRATOR
# =============================================================================
def _progress_bar(current: int, total: int, width: int = 30) -> str:
    pct = current / total if total > 0 else 1.0
    filled = int(width * pct)
    bar = "=" * filled + "-" * (width - filled)
    return f"[{bar}] {current}/{total} ({pct*100:.1f}%)"


def _resolve_window(strategy_module) -> int:
    """Jumlah candle terakhir yang di-feed ke strategi saat FAST_TUNING aktif.
    FAST_WINDOW>0 → pakai itu (di-clamp >= MIN_CANDLES supaya indikator cukup
    data). FAST_WINDOW=0 → auto = MIN_CANDLES × 5 (minimal 200) — cukup lebar
    agar EMA/indikator rolling sudah konvergen (hasil ≈ full history)."""
    min_c = int(getattr(strategy_module, "MIN_CANDLES", 50))
    fw = int(getattr(tc, "FAST_WINDOW", 0) or 0)
    if fw > 0:
        if fw < min_c:
            print(f"[WARN] FAST_WINDOW={fw} < MIN_CANDLES={min_c} → dinaikkan ke {min_c} "
                  f"(window lebih kecil = indikator strategi kekurangan data).")
            fw = min_c
        return fw
    return max(min_c * 5, 200)


def run_grid_search(strategy_module, candles_train: list[dict], candles_test: list[dict],
                    names: list[str], combos: list[tuple], tf: str,
                    window: int | None = None) -> list[dict]:
    sim_fn = simulate_single if tc.POSITION_MODEL == "single" else simulate_multi
    total = len(combos)
    win_note = f" | FAST window={window}" if window else ""
    print(f"\n[INFO] Grid Search ({tf}): {total} kombinasi parameter{win_note}")
    print(f"[INFO] In-sample: {len(candles_train)} candles | Out-of-sample: {len(candles_test)} candles")

    results: list[dict] = []
    start = time.time()
    last_print = 0.0
    first_err_shown = False

    for idx, combo in enumerate(combos, start=1):
        patch_params(strategy_module, names, combo)
        try:
            res_in = sim_fn(strategy_module, candles_train, tf, window=window)
            met_in = compute_metrics(res_in, INITIAL_BALANCE)
            score_in = score_metrics(met_in, tc.OPTIM_MODE)
        except Exception as e:
            if not first_err_shown:
                print(f"\n[WARN] Combo gagal: {dict(zip(names, combo))} → {type(e).__name__}: {e}")
                first_err_shown = True
            continue

        results.append({
            "params":   dict(zip(names, combo)),
            "in":       met_in,
            "score_in": score_in,
        })

        now = time.time()
        if now - last_print > 0.3 or idx == total:
            elapsed = now - start
            eta = (elapsed / idx) * (total - idx) if idx > 0 else 0.0
            sys.stdout.write(f"\r{_progress_bar(idx, total)}  ETA {eta:.0f}s ")
            sys.stdout.flush()
            last_print = now
    print()

    results.sort(key=lambda r: r["score_in"], reverse=True)
    top_candidates = [r for r in results if r["score_in"] != float("-inf")][:TOP_N_VALIDATE]

    print(f"\n[INFO] Validating top {len(top_candidates)} candidates on out-of-sample...")
    for r in top_candidates:
        patch_params(strategy_module, names, tuple(r["params"][n] for n in names))
        try:
            res_out = sim_fn(strategy_module, candles_test, tf, window=window)
            r["out"] = compute_metrics(res_out, INITIAL_BALANCE)
            r["score_out"] = score_metrics(r["out"], tc.OPTIM_MODE)
        except Exception:
            r["out"] = compute_metrics({"trades": [], "equity_curve": [INITIAL_BALANCE],
                                        "final_balance": INITIAL_BALANCE}, INITIAL_BALANCE)
            r["score_out"] = float("-inf")

    top_candidates.sort(key=lambda r: r["score_out"], reverse=True)
    return top_candidates[:TOP_N_DISPLAY]


# =============================================================================
# WALK-FORWARD (rolling window) — tune di train → uji di test berikutnya → geser
# =============================================================================
# Beda dengan run_grid_search: pemilihan param HANYA pakai skor in-sample (train),
# lalu param itu diuji di test (TIDAK ada peek ke test saat memilih) → ini cermin
# nyata "tune di masa lalu, trade ke depan". Hasil tiap jendela dikumpulkan jadi
# DISTRIBUSI (lebih jujur dari 1 split: lihat % jendela profit, median, worst DD).
def _best_on_train(strategy_module, train: list[dict], names: list[str],
                   combos: list[tuple], tf: str, window: int | None):
    """Cari kombinasi terbaik di train (skor in-sample saja). Return
    (combo, metrics_in) atau None kalau tak ada kandidat valid."""
    sim_fn = simulate_single if tc.POSITION_MODEL == "single" else simulate_multi
    best_score = float("-inf")
    best = None
    for combo in combos:
        patch_params(strategy_module, names, combo)
        try:
            m = compute_metrics(sim_fn(strategy_module, train, tf, window=window), INITIAL_BALANCE)
        except Exception:
            continue
        s = score_metrics(m, tc.OPTIM_MODE)
        if s != float("-inf") and s > best_score:
            best_score, best = s, (combo, m)
    return best


def run_walk_forward(strategy_module, candles: list[dict], names: list[str],
                     combos: list[tuple], tf: str, window: int | None) -> list[dict]:
    """Roll jendela [train|test] sepanjang `candles`. Tiap jendela: tune di train,
    uji param terbaik di test. Return list per-jendela (params, in, out, rentang)."""
    cpd = max(1, round(86400.0 / TF_SECONDS_MAP[tf]))     # candle per hari di TF ini
    n_split = int(getattr(tc, "OOS_SPLIT", 5))
    win_len = int(getattr(tc, "WF_WINDOW_DAYS", 20) * cpd)
    step    = max(1, int(getattr(tc, "WF_STEP_DAYS", 5) * cpd))
    test_len = max(1, win_len // n_split)
    train_len = win_len - test_len

    print(f"\n[INFO] Walk-Forward ({tf}): window {getattr(tc,'WF_WINDOW_DAYS',20)}d "
          f"(train {train_len} / test {test_len} candle, split 1/{n_split}), "
          f"step {getattr(tc,'WF_STEP_DAYS',5)}d, {len(combos)} kombinasi/jendela.")
    if win_len > len(candles):
        print(f"[WARN] {tf}: data ({len(candles)} candle) < 1 window ({win_len}). "
              f"Perpanjang DAYS_BACK atau perkecil WF_WINDOW_DAYS.")
        return []

    results: list[dict] = []
    start = 0
    wf_idx = 0
    while start + win_len <= len(candles):
        train = candles[start:start + train_len]
        test  = candles[start + train_len:start + win_len]
        wf_idx += 1
        if len(train) < 50 or len(test) < 20:
            start += step
            continue
        best = _best_on_train(strategy_module, train, names, combos, tf, window)
        if best is None:
            start += step
            continue
        combo, m_in = best
        sim_fn = simulate_single if tc.POSITION_MODEL == "single" else simulate_multi
        patch_params(strategy_module, names, combo)
        try:
            m_out = compute_metrics(sim_fn(strategy_module, test, tf, window=window), INITIAL_BALANCE)
        except Exception:
            start += step
            continue
        results.append({
            "params": dict(zip(names, combo)), "in": m_in, "out": m_out,
            "t0": test[0]["open_time"], "t1": test[-1]["open_time"], "wf": len(results) + 1,
        })
        sys.stdout.write(f"\r  rolling… jendela selesai: {len(results)} ")
        sys.stdout.flush()
        start += step
    print()
    return results


# =============================================================================
# OUTPUT
# =============================================================================
def _fmt_value(v: Any) -> str:
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        return f"{v:.6f}".rstrip("0").rstrip(".")
    return str(v)


def _fmt_params(params: dict) -> str:
    return ", ".join(f"{k}={_fmt_value(v)}" for k, v in params.items())


def _score_label() -> str:
    return {"profit": "PnL (USDT)", "drawdown": "-MaxDD (%)",
            "balanced": "Sharpe Ratio"}.get(tc.OPTIM_MODE, "Score")


def print_top_results(top: list[dict], tf: str | None = None) -> None:
    print()
    print("=" * 70)
    tf_note = f" | TF: {tf}" if tf else ""
    print(f"  TOP {len(top)} PARAMETERS — strategy: {tc.STRATEGY_FILE}.py | optim: {tc.OPTIM_MODE.upper()}{tf_note}")
    print("  (Sorted by out-of-sample score)")
    print("=" * 70)

    if not top:
        print("\n[WARN] Tidak ada kombinasi yang valid (min 3 trade per backtest).")
        print("       Coba perluas range parameter di tuning_config.py atau tambah jumlah candle.")
        return

    score_label = _score_label()

    for rank, r in enumerate(top, start=1):
        print()
        print(f"#{rank}  Out-of-Sample Score: {r['score_out']:.4f}   "
              f"In-Sample Score: {r['score_in']:.4f}   ({score_label})")
        print(f"  Params: {_fmt_params(r['params'])}")
        print_metric_block("In-Sample (80%)", r["in"])
        print_metric_block("Out-of-Sample (20%)", r["out"])

    print()
    print("=" * 70)
    print("  Selesai. Copy parameter di atas ke strategy file untuk dipakai live.")
    print("=" * 70)


def print_tf_leaderboard(all_results: list[dict]) -> None:
    """Ranking lintas-timeframe (urut out-of-sample score) → jawab 'TF mana yang
    paling cocok'. Dipanggil hanya saat grid multi-TF."""
    ranked = sorted(all_results, key=lambda r: r.get("score_out", float("-inf")), reverse=True)
    print()
    print("=" * 70)
    print(f"  LEADERBOARD ANTAR-TIMEFRAME — optim: {tc.OPTIM_MODE.upper()} ({_score_label()})")
    print("  (gabungan kandidat terbaik tiap TF, urut out-of-sample score)")
    print("=" * 70)
    if not ranked:
        print("\n[WARN] Tidak ada kandidat valid di TF mana pun.")
        return
    for rank, r in enumerate(ranked, start=1):
        mi, mo = r["in"], r["out"]
        star = "  <-- TF TERBAIK" if rank == 1 else ""
        print(f"\n#{rank}  TF={r.get('tf','?'):>4}  OOS={r['score_out']:.4f}  IS={r['score_in']:.4f}{star}")
        print(f"     PnL  in {mi['pnl']:+.2f} / out {mo['pnl']:+.2f} USDT   "
              f"MaxDD in {mi['max_dd_pct']:.1f}% / out {mo['max_dd_pct']:.1f}%   "
              f"PF in {mi['profit_factor'] if mi['profit_factor']!=float('inf') else 9.99:.2f}")
        print(f"     Params: {_fmt_params(r['params'])}")
    print()
    print("=" * 70)


def _wf_stats(results: list[dict]) -> dict:
    """Ringkas distribusi hasil OUT-OF-SAMPLE walk-forward."""
    pnls = np.array([r["out"]["pnl"] for r in results], float)
    dds  = np.array([r["out"]["max_dd_pct"] for r in results], float)
    n = len(results)
    return {
        "n": n,
        "pct_profit": float((pnls > 0).mean() * 100),
        "median_pnl": float(np.median(pnls)),
        "mean_pnl":   float(pnls.mean()),
        "sum_pnl":    float(pnls.sum()),
        "worst_pnl":  float(pnls.min()),
        "best_pnl":   float(pnls.max()),
        "worst_dd":   float(dds.max()),
        "med_dd":     float(np.median(dds)),
    }


def print_walk_forward(results: list[dict], tf: str | None = None) -> None:
    """Tabel per-jendela + DISTRIBUSI hasil out-of-sample (inti walk-forward)."""
    print()
    print("=" * 70)
    tf_note = f" | TF: {tf}" if tf else ""
    print(f"  WALK-FORWARD — strategy: {tc.STRATEGY_FILE}.py | optim: {tc.OPTIM_MODE.upper()}{tf_note}")
    print("  (tiap jendela: tune di train → uji di test berikutnya yang UNSEEN)")
    print("=" * 70)
    if not results:
        print("\n[WARN] Tidak ada jendela walk-forward yang valid. "
              "Perpanjang DAYS_BACK / perkecil WF_WINDOW_DAYS.")
        return
    print(f"\n  {'#':>3} {'test mulai':>16} {'PnL':>8} {'DD%':>6} {'trades':>6} {'win%':>5}  best params")
    for r in results:
        mo = r["out"]
        print(f"  {r['wf']:>3} {_ts_str(r['t0']):>16} {mo['pnl']:>+8.2f} {mo['max_dd_pct']:>6.1f} "
              f"{mo['num_trades']:>6} {mo['win_rate']:>5.1f}  {_fmt_params(r['params'])}")
    s = _wf_stats(results)
    print("\n  " + "-" * 66)
    print(f"  DISTRIBUSI OUT-OF-SAMPLE ({s['n']} jendela):")
    print(f"    Jendela profit : {s['pct_profit']:.0f}%   "
          f"(median PnL {s['median_pnl']:+.2f} | mean {s['mean_pnl']:+.2f} | total {s['sum_pnl']:+.2f} USDT)")
    print(f"    PnL terburuk   : {s['worst_pnl']:+.2f}   terbaik {s['best_pnl']:+.2f} USDT")
    print(f"    Max DD         : terburuk {s['worst_dd']:.1f}%   median {s['med_dd']:.1f}%")
    verdict = ("ROBUST (mayoritas jendela profit)" if s["pct_profit"] >= 60 and s["median_pnl"] > 0
               else "RAPUH (banyak jendela rugi → hati-hati overfit/regime)" if s["pct_profit"] < 50
               else "CAMPUR (edge tipis)")
    print(f"    Verdict        : {verdict}")
    print("=" * 70)


def print_wf_tf_summary(all_wf: list[tuple]) -> None:
    """Ringkasan lintas-TF untuk mode walk-forward → 'TF mana paling tahan'."""
    rows = [(tf, _wf_stats(res)) for tf, res in all_wf if res]
    rows.sort(key=lambda x: (x[1]["median_pnl"], x[1]["pct_profit"]), reverse=True)
    print()
    print("=" * 70)
    print("  RINGKASAN WALK-FORWARD ANTAR-TIMEFRAME")
    print("  (urut median PnL out-of-sample → TF paling tahan lintas jendela)")
    print("=" * 70)
    if not rows:
        print("\n[WARN] Tidak ada jendela valid di TF mana pun.")
        return
    print(f"\n  {'TF':>4} {'jendela':>7} {'%profit':>8} {'medianPnL':>10} {'meanPnL':>9} {'worstDD%':>9}")
    for i, (tf, s) in enumerate(rows):
        star = "  <-- TF TERBAIK" if i == 0 else ""
        print(f"  {tf:>4} {s['n']:>7} {s['pct_profit']:>7.0f}% {s['median_pnl']:>+10.2f} "
              f"{s['mean_pnl']:>+9.2f} {s['worst_dd']:>9.1f}{star}")
    print()
    print("=" * 70)


# =============================================================================
# DATA LOADING (per timeframe)
# =============================================================================
def _load_candles(tf: str) -> list[dict]:
    """Ambil candle untuk satu TF (cache → kalau miss download + simpan CSV).
    Return [] kalau gagal (caller skip TF itu)."""
    max_cache_age = float(getattr(tc, "MAX_CACHE_AGE_MINUTES", 60))
    cached, reason = _try_load_cache(SYMBOL, tf, tc.DAYS_BACK, max_cache_age)
    if cached is not None:
        print(f"[CACHE] {tf}: {reason} "
              f"({_ts_str(cached[0]['open_time'])} → {_ts_str(cached[-1]['open_time'])})")
        return cached
    print(f"[CACHE] {tf} miss: {reason}")
    print(f"[INFO] Downloading {SYMBOL} {tf} candles untuk {tc.DAYS_BACK} hari...")
    t0 = time.time()
    candles = download_candles(SYMBOL, tf, tc.DAYS_BACK)
    if not candles:
        print(f"[ERROR] {tf}: gagal download / data kosong — TF ini di-skip.")
        return []
    print(f"[OK]   {tf}: {len(candles)} candles ({time.time()-t0:.1f}s) — "
          f"{_ts_str(candles[0]['open_time'])} → {_ts_str(candles[-1]['open_time'])}")
    print(f"[OK]   CSV: {save_candles_csv(candles, SYMBOL, tf)}")
    return candles


# =============================================================================
# ENTRY (dipanggil root hypertune.py)
# =============================================================================
def run_tuning() -> None:
    validate_config()

    # TF yang diadu: TUNE_TIMEFRAMES kalau diisi, kalau tidak → TIMEFRAME tunggal.
    tf_list = list(getattr(tc, "TUNE_TIMEFRAMES", []) or [])
    if not tf_list:
        tf_list = [tc.TIMEFRAME]
    fast = bool(getattr(tc, "FAST_TUNING", False))
    walk = bool(getattr(tc, "WALK_FORWARD", False))
    n_split = int(getattr(tc, "OOS_SPLIT", 5))

    print("=" * 70)
    print("  HYPERTUNING — Config-Driven")
    print("=" * 70)
    print(f"  Strategy file : {tc.STRATEGY_FILE}.py")
    print(f"  Symbol        : {SYMBOL}  (dari config.py)")
    print(f"  Timeframe(s)  : {', '.join(tf_list)}" + ("  (grid multi-TF)" if len(tf_list) > 1 else ""))
    print(f"  Days back     : {tc.DAYS_BACK}")
    print(f"  Optim mode    : {tc.OPTIM_MODE}")
    print(f"  Position      : {tc.POSITION_MODEL}")
    print(f"  Fast tuning   : {fast}")
    print(f"  Split (OOS)   : 1/{n_split} test : {n_split-1}/{n_split} train")
    if walk:
        print(f"  Walk-forward  : ON (window {getattr(tc,'WF_WINDOW_DAYS',20)}d, "
              f"step {getattr(tc,'WF_STEP_DAYS',5)}d)")
    print(f"  Initial Bal   : {INITIAL_BALANCE:.2f} USDT")
    print(f"  Order Size    : {ORDER_SIZE_USDT} USDT × leverage {LEVERAGE} = {ORDER_SIZE_USDT*LEVERAGE} notional")
    print(f"  Fee Rate      : {FEE_RATE*100:.3f}% per side")
    print("=" * 70)

    # --- Expand grid ---
    try:
        names, combos = expand_grid(tc.PARAMS)
    except Exception as e:
        print(f"[ERROR] Gagal parse PARAMS: {e}")
        sys.exit(1)

    print(f"\n[INFO] Param dituning: {names}")
    print(f"[INFO] Kombinasi per TF: {len(combos)}  ×  {len(tf_list)} TF  =  {len(combos)*len(tf_list)} total")
    if len(combos) == 0:
        print(f"[ERROR] Grid kosong.")
        sys.exit(1)

    for n in names:
        vals = expand_param(n, tc.PARAMS[n])
        preview = vals if len(vals) <= 6 else (vals[:3] + ["..."] + vals[-2:])
        print(f"       {n}: {preview}  (n={len(vals)})")

    # --- Load strategy (sekali; TF di-set per iterasi oleh simulator) ---
    strategy_module = load_strategy(tc.STRATEGY_FILE, tf_list[0])
    print(f"\n[OK]   Strategy '{tc.STRATEGY_FILE}.py' loaded.")

    # Param yang dituning HARUS ada di strategy file (kalau tidak, tuning sia-sia).
    validate_params_in_strategy(strategy_module)

    # FAST_TUNING: tentukan lebar window sekali (MIN_CANDLES strategi TF-independen).
    window = _resolve_window(strategy_module) if fast else None
    if fast:
        print(f"[INFO] FAST_TUNING aktif → strategi tiap bar hanya 'lihat' {window} candle "
              f"terakhir (indikator rolling: hasil ≈ full history; angka final tetap "
              f"verifikasi via backtest.py).")

    # --- Loop tiap TF: load data → (walk-forward ATAU single-split) ---
    t0 = time.time()
    all_top: list[dict] = []        # mode single-split (untuk leaderboard antar-TF)
    all_wf: list[tuple] = []        # mode walk-forward (untuk ringkasan antar-TF)
    ran_any = False
    for tf in tf_list:
        print("\n" + "-" * 70)
        print(f"  TIMEFRAME: {tf}")
        print("-" * 70)
        candles = _load_candles(tf)
        if not candles:
            continue

        if walk:
            wf = run_walk_forward(strategy_module, candles, names, combos, tf, window)
            if not wf:
                continue
            ran_any = True
            all_wf.append((tf, wf))
            print_walk_forward(wf, tf if len(tf_list) > 1 else None)
            continue

        split_idx = int(len(candles) * (n_split - 1) / n_split)
        candles_train = candles[:split_idx]
        candles_test  = candles[split_idx:]
        if len(candles_train) < 50 or len(candles_test) < 20:
            print(f"[WARN] {tf}: data terlalu sedikit (train={len(candles_train)}, "
                  f"test={len(candles_test)}) — TF ini di-skip. Perpanjang DAYS_BACK.")
            continue
        ran_any = True
        top = run_grid_search(strategy_module, candles_train, candles_test,
                              names, combos, tf, window)
        for r in top:
            r["tf"] = tf
        all_top.extend(top)
        print_top_results(top, tf if len(tf_list) > 1 else None)

    # Tidak ada satu pun TF yang punya data cukup → gagal keras (kontrak lama).
    if not ran_any:
        print(f"[ERROR] Tidak ada TF dengan data cukup untuk tuning "
              f"(butuh train>=50 & test>=20 candle). Perpanjang DAYS_BACK atau "
              f"pilih timeframe lebih kecil.")
        sys.exit(1)

    print(f"\n[INFO] Total waktu hypertuning: {time.time()-t0:.1f}s")

    # --- Ringkasan lintas-TF (kalau lebih dari satu TF) ---
    if len(tf_list) > 1:
        if walk:
            print_wf_tf_summary(all_wf)
        else:
            print_tf_leaderboard(all_top)
