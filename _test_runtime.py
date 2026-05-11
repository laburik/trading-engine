"""
Quick runtime sanity test — confirms all refactored modules work at runtime.
Run: python _test_runtime.py
"""
import sys

print("=" * 55)
print("  RUNTIME SANITY TEST (refactored modules)")
print("=" * 55)

# 1. Import semua modul
import ft_types;         print("[OK] ft_types")
import trade_logger;     print("[OK] trade_logger")
import data_stream;      print("[OK] data_stream")
import data_resampler;   print("[OK] data_resampler")
import position_manager; print("[OK] position_manager")
import strategy;         print(f"[OK] strategy  (ML_ENABLED={strategy.ML_ENABLED})")
import execution;        print("[OK] execution")

print()

# 2. Test position_manager logic
position_manager.open_position("long", 2.5, 100.0, 0.05)
pos = position_manager.get_position()
assert pos["side"] == "long", "side should be long"
assert pos["entry_price"] == 2.5, "entry_price mismatch"
net_pnl, fee = position_manager.close_position(2.6, 0.05)
assert round(net_pnl, 4) == round((2.6 - 2.5) * 100.0 - 0.05, 4)
pnl = position_manager.get_pnl_summary()
print(f"[OK] position_manager: balance={pnl['balance']:.4f}, realized={pnl['realized_pnl_total']:.4f}")

# 3. Test generate_signal dengan data dummy
from ft_types import MarketDataSnapshot, Candle
from typing import Optional

dummy_candles = [
    {
        "timeframe": "15m", "open_time": float(i),
        "open": 2.5, "high": 2.55, "low": 2.45,
        "close": 2.5 + i * 0.001,
        "volume": 1000.0, "buy_volume": 500.0, "sell_volume": 500.0,
        "tick_count": 100,
    }
    for i in range(60)
]

dummy_data: MarketDataSnapshot = {
    "candles": {"15m": dummy_candles},  # type: ignore[list-item]
    "current": {"15m": None},
    "best_bid": {"price": 2.50, "qty": 100.0},
    "best_ask": {"price": 2.51, "qty": 100.0},
    "bid_ask_spread": 0.01,
    "orderbook_imbalance": 0.0,
    "volume_delta": 0.0,
    "funding_rate": 0.0001,
    "latest_tick": None,
    "is_warmup": False,
}

sig = strategy.generate_signal(dummy_data)
assert sig["action"] in ("buy", "sell", "hold", "close")
print(f"[OK] strategy.generate_signal: action={sig['action']!r}")
print(f"     reason: {sig.get('reason', '')[:60]}")

# 4. Test ft_types TypedDict instantiation
snap: ft_types.BidAskLevel = {"price": 1.0, "qty": 10.0}
assert snap["price"] == 1.0
print("[OK] ft_types TypedDict instantiation")

print()
print("=" * 55)
print("  SEMUA TEST BERHASIL — Program siap dijalankan!")
print("  Jalankan dengan: python main.py")
print("=" * 55)
