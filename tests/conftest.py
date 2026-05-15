# =============================================================================
# tests/conftest.py — pytest fixtures & path setup
# =============================================================================
# Auto-loaded oleh pytest. Berlaku untuk semua test di folder tests/.
# =============================================================================
from __future__ import annotations

import os
import random
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

# Pastikan project root ada di sys.path supaya `import strategy` dst jalan
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# =============================================================================
# FIXTURE: Mock external dependencies (CCXT, sklearn, dll)
# =============================================================================
@pytest.fixture(autouse=True)
def _mock_external_io(monkeypatch):
    """
    Auto-applied fixture: mock semua I/O eksternal supaya test deterministik.
    - ccxt_client.exchange       → MagicMock (no real API call)
    - trade_logger.log_trade     → no-op
    - trade_logger.log_equity    → no-op
    - bot_health.json file write → no-op
    """
    # Mock ccxt exchange agar tidak panggil Bybit
    if "ccxt_client" in sys.modules:
        sys.modules["ccxt_client"].exchange = MagicMock()
        sys.modules["ccxt_client"].CCXT_SYMBOL = "XRP/USDT:USDT"

    # Block actual file writes dari trade_logger di test
    if "trade_logger" in sys.modules:
        monkeypatch.setattr("trade_logger.log_trade", lambda *a, **kw: None)
        monkeypatch.setattr("trade_logger.log_equity", lambda *a, **kw: None)


# =============================================================================
# HELPER: bikin dummy candle data deterministik
# =============================================================================
def make_candles(n: int = 100, base_price: float = 1.5, seed: int = 42, tf: str = "2h") -> list[dict]:
    """Bikin list candle dummy yang reproducible (seed fix)."""
    random.seed(seed)
    candles = []
    interval_sec = {"1m": 60, "15m": 900, "1h": 3600, "2h": 7200}.get(tf, 7200)
    base_ts = 1700000000.0
    for i in range(n):
        o = base_price + random.uniform(-0.05, 0.05)
        c = o + random.uniform(-0.03, 0.03)
        h = max(o, c) + random.uniform(0, 0.02)
        l = min(o, c) - random.uniform(0, 0.02)
        v = random.uniform(1000, 5000)
        candles.append({
            "timeframe": tf,
            "open_time": base_ts + i * interval_sec,
            "open": round(o, 5),
            "high": round(h, 5),
            "low": round(l, 5),
            "close": round(c, 5),
            "volume": round(v, 2),
            "buy_volume": round(v * 0.55, 2),
            "sell_volume": round(v * 0.45, 2),
            "tick_count": 100,
        })
    return candles


@pytest.fixture
def dummy_market_data():
    """Snapshot data lengkap (seperti yang dikirim engine ke generate_signal)."""
    candles = make_candles(n=100)
    current = candles[-1]
    return {
        "candles":  {"2h": candles[:-1]},
        "current":  {"2h": current},
        "best_bid": {"price": current["close"] * 0.9999, "qty": 1.0},
        "best_ask": {"price": current["close"] * 1.0001, "qty": 1.0},
        "bid_ask_spread":      current["close"] * 0.0002,
        "orderbook_imbalance": 0.0,
        "volume_delta":        0.0,
        "funding_rate":        0.0001,
        "latest_tick": None,
        "is_warmup": False,
    }


@pytest.fixture
def warmup_market_data(dummy_market_data):
    """Sama dengan dummy_market_data tapi is_warmup=True (strategi harus return hold)."""
    data = dict(dummy_market_data)
    data["is_warmup"] = True
    return data
