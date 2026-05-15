# =============================================================================
# strategy_runtime.py — Engine-Managed Strategy Orchestrator
# =============================================================================
#
# Mode default: user hanya perlu nulis `generate_signal(data)` di strategy.py.
# Engine otomatis urus:
#   1. Panggil generate_signal() untuk dapat sinyal
#   2. Auto-extract ml_prob dari signal["reason"] jika ada pola "Skor: X.XX"
#   3. Auto-fetch equity & position_side dari position_manager
#   4. Catat ke bot_monitor.record_tick() — supaya dashboard tidak stuck STARTING
#   5. Forward ke execution.place_order() jika action != "hold"
#
# Mode advanced: kalau user define `on_tick(data)` sendiri di strategy.py,
# engine pakai itu (bypass orkestrasi default). Cocok untuk kasus seperti
# strategy_ml.py yang butuh sync state custom dengan position_manager.
# =============================================================================
from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger("strategy_runtime")

# Regex untuk ekstrak skor probabilitas dari reason string seperti
# "Sinyal RF ML Prob Naik > 60% (Skor: 0.72)"
_ML_PROB_PATTERN = re.compile(r"Skor:\s*([0-9.]+)")


def _extract_ml_prob(reason: str) -> Optional[float]:
    """Parse ML probability dari reason string. Return None jika tidak ditemukan."""
    if not reason:
        return None
    m = _ML_PROB_PATTERN.search(reason)
    if not m:
        return None
    try:
        return float(m.group(1))
    except (ValueError, IndexError):
        return None


def _get_position_info() -> tuple[Optional[float], Optional[str]]:
    """Ambil equity & position_side dari position_manager. Return (None, None) jika gagal."""
    try:
        import position_manager as _pm
        summary = _pm.get_pnl_summary()
        equity = float(summary.get("equity", 0.0))  # type: ignore[arg-type]
        side = str(summary.get("side", "none"))
        return equity, side
    except Exception:
        return None, None


def run_iteration(strategy_module: Any, data: dict) -> None:
    """
    Jalankan satu iterasi strategi. Dipanggil oleh main.strategy_loop tiap tick.

    - Jika strategy_module.on_tick() didefinisikan → user ambil kendali penuh.
    - Selain itu → engine orkestrasi default (signal → record → execute).
    """
    # ── Mode advanced: user override on_tick ────────────────────────────────
    if hasattr(strategy_module, "on_tick") and callable(strategy_module.on_tick):
        strategy_module.on_tick(data)
        return

    # ── Mode default: engine-managed ────────────────────────────────────────
    import bot_monitor

    try:
        signal = strategy_module.generate_signal(data)
    except Exception as e:
        bot_monitor.record_error(f"generate_signal() crash: {type(e).__name__}: {e}")
        logger.error(f"generate_signal() error: {e}", exc_info=True)
        return

    if not isinstance(signal, dict) or "action" not in signal:
        bot_monitor.record_error(f"generate_signal() return invalid: {signal!r}")
        return

    ml_prob = _extract_ml_prob(signal.get("reason", ""))
    equity, pos_side = _get_position_info()

    bot_monitor.record_tick(
        signal,
        ml_prob=ml_prob,
        data=data,
        equity=equity,
        position_side=pos_side,
    )

    action = signal.get("action", "hold")
    if action in ("buy", "sell", "close"):
        import execution
        logger.info(f"[EXECUTE] {action.upper()} | {signal.get('reason', '')}")
        execution.place_order(signal)
