# =============================================================================
# backtest.py — Backtest CLI mandiri (PnL + metrik lengkap)
# =============================================================================
# Cara pakai:
#   1. Edit backtest_config.py (TIMEFRAME, START, END, STRATEGY_FILE).
#      SYMBOL / MODAL / LEVERAGE / FEE diambil dari config.py.
#   2. python backtest.py
#
# Mesin simulasi & metrik dibagi dengan hypertune.py (engine/backtest_*.py) →
# hasil backtest konsisten dengan tuning, nol drift.
# =============================================================================
from __future__ import annotations

import os
import sys
import time

# File ini di user/. Tambahkan root (config.py) + root/engine (modul infra) +
# user/ (strategy, backtest_config) ke sys.path supaya import flat tetap jalan.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, os.path.join(_ROOT, "engine"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# backtest_core dipasang DULU (memasang mock position_manager/execution ke
# sys.modules sebelum strategy di-load).
from backtest_core import load_strategy, simulate_single, simulate_multi
from backtest_metrics import compute_metrics, print_backtest_report
from backtest_data import (
    download_candles_range_cached, date_to_ms, _ts_str, TF_SECONDS_MAP,
)

from config import SYMBOL, INITIAL_BALANCE, LEVERAGE, ORDER_SIZE_USDT, FEE_RATE
import backtest_config as bc


def main() -> None:
    # --- Validasi config ---
    if bc.TIMEFRAME not in TF_SECONDS_MAP:
        print(f"[ERROR] TIMEFRAME '{bc.TIMEFRAME}' invalid. Valid: {list(TF_SECONDS_MAP.keys())}")
        sys.exit(1)
    if bc.POSITION_MODEL not in ("single", "multi"):
        print(f"[ERROR] POSITION_MODEL harus single|multi, dapat: '{bc.POSITION_MODEL}'")
        sys.exit(1)

    try:
        start_ms = date_to_ms(bc.START)
        end_ms   = date_to_ms(bc.END)
    except ValueError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)
    if start_ms >= end_ms:
        print(f"[ERROR] START ({bc.START}) harus sebelum END ({bc.END}).")
        sys.exit(1)

    is_live_end = str(bc.END).strip().lower() in ("end", "now", "")
    end_label = "now" if is_live_end else bc.END
    # END tanggal tetap → data immutable → boleh di-cache. END='end' → tumbuh → fetch fresh.
    allow_cache = not is_live_end

    print("=" * 64)
    print("  BACKTEST — config-driven")
    print("=" * 64)
    print(f"  Symbol     : {SYMBOL}  (dari config.py)")
    print(f"  Timeframe  : {bc.TIMEFRAME}")
    print(f"  Range      : {bc.START} → {end_label}")
    print(f"  Strategy   : {bc.STRATEGY_FILE}.py  | model: {bc.POSITION_MODEL}")
    print(f"  Modal      : {INITIAL_BALANCE:.2f} USDT")
    print(f"  Order Size : {ORDER_SIZE_USDT} USDT × leverage {LEVERAGE} = {ORDER_SIZE_USDT*LEVERAGE} notional")
    print(f"  Fee Rate   : {FEE_RATE*100:.3f}% per side")
    print("=" * 64)

    # --- Ambil data (cache kalau END tanggal tetap) ---
    cache_note = "cache aktif (END tetap)" if allow_cache else "fetch fresh (END='end')"
    print(f"\n[INFO] Menyiapkan {SYMBOL} {bc.TIMEFRAME} candles — {cache_note}...")
    t0 = time.time()
    candles, src = download_candles_range_cached(SYMBOL, bc.TIMEFRAME, start_ms, end_ms, allow_cache)
    if not candles:
        print("[ERROR] Gagal ambil data atau data kosong untuk rentang ini.")
        sys.exit(1)
    print(f"[OK]   {len(candles)} candles ({time.time()-t0:.1f}s) — {src}")
    print(f"       Range aktual: {_ts_str(candles[0]['open_time'])} → {_ts_str(candles[-1]['open_time'])}")

    # --- Load strategy + simulate ---
    strat = load_strategy(bc.STRATEGY_FILE, bc.TIMEFRAME)
    print(f"[OK]   Strategy '{bc.STRATEGY_FILE}.py' loaded.\n")

    sim_fn = simulate_single if bc.POSITION_MODEL == "single" else simulate_multi
    result = sim_fn(strat, candles, bc.TIMEFRAME)
    metrics = compute_metrics(result, INITIAL_BALANCE)

    header = (f"BACKTEST {SYMBOL} {bc.TIMEFRAME} | {bc.START} → {end_label} "
              f"(LEV {LEVERAGE}x, modal {INITIAL_BALANCE:.0f})")
    print_backtest_report(header, metrics)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INFO] Dibatalkan user.")
        sys.exit(0)
