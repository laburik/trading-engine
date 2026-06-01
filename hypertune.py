# =============================================================================
# hypertune.py — Config-Driven Hyperparameter Tuning (launcher tipis)
# =============================================================================
# Cara pakai:
#   1. Edit tuning_config.py (STRATEGY_FILE, TIMEFRAME, DAYS_BACK, OPTIM_MODE,
#      POSITION_MODEL, PARAMS). SYMBOL/MODAL/LEVERAGE/FEE dari config.py.
#   2. Pastikan parameter di strategy file (mis. RSI_PERIOD = 14) ada di top.
#   3. python hypertune.py
#
# Logika tuning hidup di engine/tuning_core.py (mesin simulasi & metrik dibagi
# dengan backtest.py via engine/backtest_*.py → nol drift).
# =============================================================================
from __future__ import annotations

import os
import sys

# Tambahkan folder engine/ ke sys.path agar modul infrastruktur bisa di-import flat.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tuning_core import run_tuning


def main() -> None:
    run_tuning()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INFO] Dibatalkan user.")
        sys.exit(0)
