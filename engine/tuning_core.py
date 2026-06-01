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
    if not isinstance(tc.DAYS_BACK, int) or tc.DAYS_BACK < 1:
        print(f"[ERROR] DAYS_BACK harus integer >= 1.")
        sys.exit(1)
    if not isinstance(tc.STRATEGY_FILE, str) or not tc.STRATEGY_FILE:
        print(f"[ERROR] STRATEGY_FILE harus string non-empty.")
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


def run_grid_search(strategy_module, candles_train: list[dict], candles_test: list[dict],
                    names: list[str], combos: list[tuple]) -> list[dict]:
    sim_fn = simulate_single if tc.POSITION_MODEL == "single" else simulate_multi
    total = len(combos)
    print(f"\n[INFO] Grid Search: {total} kombinasi parameter")
    print(f"[INFO] In-sample: {len(candles_train)} candles | Out-of-sample: {len(candles_test)} candles")

    results: list[dict] = []
    start = time.time()
    last_print = 0.0
    first_err_shown = False

    for idx, combo in enumerate(combos, start=1):
        patch_params(strategy_module, names, combo)
        try:
            res_in = sim_fn(strategy_module, candles_train, tc.TIMEFRAME)
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
            res_out = sim_fn(strategy_module, candles_test, tc.TIMEFRAME)
            r["out"] = compute_metrics(res_out, INITIAL_BALANCE)
            r["score_out"] = score_metrics(r["out"], tc.OPTIM_MODE)
        except Exception:
            r["out"] = compute_metrics({"trades": [], "equity_curve": [INITIAL_BALANCE],
                                        "final_balance": INITIAL_BALANCE}, INITIAL_BALANCE)
            r["score_out"] = float("-inf")

    top_candidates.sort(key=lambda r: r["score_out"], reverse=True)
    return top_candidates[:TOP_N_DISPLAY]


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


def print_top_results(top: list[dict]) -> None:
    print()
    print("=" * 70)
    print(f"  TOP {len(top)} PARAMETERS — strategy: {tc.STRATEGY_FILE}.py | optim: {tc.OPTIM_MODE.upper()}")
    print("  (Sorted by out-of-sample score)")
    print("=" * 70)

    if not top:
        print("\n[WARN] Tidak ada kombinasi yang valid (min 3 trade per backtest).")
        print("       Coba perluas range parameter di tuning_config.py atau tambah jumlah candle.")
        return

    score_label = {"profit": "PnL (USDT)", "drawdown": "-MaxDD (%)",
                   "balanced": "Sharpe Ratio"}.get(tc.OPTIM_MODE, "Score")

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


# =============================================================================
# ENTRY (dipanggil root hypertune.py)
# =============================================================================
def run_tuning() -> None:
    validate_config()

    print("=" * 70)
    print("  HYPERTUNING — Config-Driven")
    print("=" * 70)
    print(f"  Strategy file : {tc.STRATEGY_FILE}.py")
    print(f"  Symbol        : {SYMBOL}  (dari config.py)")
    print(f"  Timeframe     : {tc.TIMEFRAME}")
    print(f"  Days back     : {tc.DAYS_BACK}")
    print(f"  Optim mode    : {tc.OPTIM_MODE}")
    print(f"  Position      : {tc.POSITION_MODEL}")
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
    print(f"[INFO] Total kombinasi: {len(combos)}")
    if len(combos) == 0:
        print(f"[ERROR] Grid kosong.")
        sys.exit(1)

    for n in names:
        vals = expand_param(n, tc.PARAMS[n])
        preview = vals if len(vals) <= 6 else (vals[:3] + ["..."] + vals[-2:])
        print(f"       {n}: {preview}  (n={len(vals)})")

    # --- Load strategy ---
    strategy_module = load_strategy(tc.STRATEGY_FILE, tc.TIMEFRAME)
    print(f"\n[OK]   Strategy '{tc.STRATEGY_FILE}.py' loaded.")

    # --- Cek cache dulu ---
    max_cache_age = float(getattr(tc, "MAX_CACHE_AGE_MINUTES", 60))
    cached_candles, cache_reason = _try_load_cache(SYMBOL, tc.TIMEFRAME, tc.DAYS_BACK, max_cache_age)
    if cached_candles is not None:
        candles: list[dict] = cached_candles
        print(f"\n[CACHE] {cache_reason}")
        print(f"        Skip download — pakai data cache untuk hemat waktu.")
        print(f"        Range: {_ts_str(candles[0]['open_time'])} → {_ts_str(candles[-1]['open_time'])}")
    else:
        print(f"\n[CACHE] miss: {cache_reason}")
        print(f"[INFO] Downloading {SYMBOL} {tc.TIMEFRAME} candles untuk {tc.DAYS_BACK} hari...")
        t0 = time.time()
        candles = download_candles(SYMBOL, tc.TIMEFRAME, tc.DAYS_BACK)
        if not candles:
            print("[ERROR] Gagal download data atau data kosong.")
            sys.exit(1)
        print(f"[OK]   {len(candles)} candles ({time.time()-t0:.1f}s)")
        print(f"       Range: {_ts_str(candles[0]['open_time'])} → {_ts_str(candles[-1]['open_time'])}")

        csv_path = save_candles_csv(candles, SYMBOL, tc.TIMEFRAME)
        print(f"[OK]   CSV: {csv_path}")

    # --- Split 80/20 ---
    split_idx = int(len(candles) * 0.8)
    candles_train = candles[:split_idx]
    candles_test  = candles[split_idx:]
    if len(candles_train) < 50 or len(candles_test) < 20:
        print(f"[ERROR] Data terlalu sedikit. Train={len(candles_train)}, Test={len(candles_test)}.")
        print(f"        Pilih timeframe lebih kecil atau perpanjang DAYS_BACK di tuning_config.py.")
        sys.exit(1)

    # --- Grid search ---
    t0 = time.time()
    top = run_grid_search(strategy_module, candles_train, candles_test, names, combos)
    print(f"\n[INFO] Total waktu hypertuning: {time.time()-t0:.1f}s")

    print_top_results(top)
