"""
Test suite untuk verifikasi semua bug fix:
- BUG-01 s/d BUG-07, BUG-11 (round 1)
- NEW-01, NEW-03, NEW-05, OLD-08, OLD-09 (round 2)

Jalankan: python test_bugfix.py  (atau $env:PYTHONUTF8="1"; python test_bugfix.py di Windows)
"""
import sys
import hmac
import hashlib
import numpy as np
import types
import re
import asyncio
from collections import deque
from sortedcontainers import SortedDict

sys.path.insert(0, '.')

PASS = 0
FAIL = 0

def check(name, condition, msg=""):
    global PASS, FAIL
    if condition:
        print(f"  PASS  {name}")
        PASS += 1
    else:
        print(f"  FAIL  {name}: {msg}")
        FAIL += 1


# =========================================================
# BUG-01: hmac.HMAC()
# =========================================================
print("\n[BUG-01] hmac.HMAC() compatibility")
sign = hmac.HMAC(b'secret_key', b'test_data', hashlib.sha256).hexdigest()
check("hmac.HMAC() works", len(sign) == 64)
with open('position_manager.py', encoding='utf-8') as f:
    src_pm = f.read()
check("position_manager uses hmac.HMAC()", "hmac.HMAC(" in src_pm)
check("position_manager NOT uses hmac.new()", "hmac.new(" not in src_pm)


# =========================================================
# BUG-02: candle_stream tidak boleh subscribe orderbook
# =========================================================
print("\n[BUG-02] Orderbook subscribe deduplification")
with open('candle_stream.py', encoding='utf-8') as f:
    src_cs = f.read()
check("candle_stream does NOT subscribe orderbook",
      'kline_args.append(f"orderbook.' not in src_cs)
check("candle_stream does NOT route orderbook messages",
      '_process_orderbook_message(data)' not in src_cs)


# =========================================================
# BUG-03: Tidak ada duplikat value di _BYBIT_INTERVAL_MAP
# =========================================================
print("\n[BUG-03] Interval map collision fix")
from candle_stream import _BYBIT_INTERVAL_MAP, _bybit_to_tf, _tf_to_bybit
vals = list(_BYBIT_INTERVAL_MAP.values())
dups = [v for v in set(vals) if vals.count(v) > 1]
check("No duplicate Bybit interval values", len(dups) == 0, f"Duplicates: {dups}")
check("1s NOT in map (removed)", 1 not in _BYBIT_INTERVAL_MAP)
check("60->1 (1m) mapping correct", _BYBIT_INTERVAL_MAP.get(60) == "1")
print(f"         _bybit_to_tf: {_bybit_to_tf}")


# =========================================================
# BUG-04: Sell signal + side tracking + SHORT triple barrier
# =========================================================
print("\n[BUG-04] Sell/short signal & side tracking")
import strategy
mock_model = types.SimpleNamespace()
mock_scaler = types.SimpleNamespace()
mock_scaler.transform = lambda X: X
strategy.model   = mock_model
strategy.scaler  = mock_scaler
strategy.ML_ENABLED = True

dummy_candles = [
    {'open_time': i*900, 'open': 1.0, 'high': 1.1, 'low': 0.9, 'close': 1.0+i*0.001, 'volume': 100+i}
    for i in range(60)
]
dummy_data = {
    'is_warmup': False,
    'candles': {'15m': dummy_candles},
    'current': {'15m': {'open_time': 60*900, 'close': 1.06}},
    'best_bid': {'price': 1.06, 'qty': 100},
    'best_ask': {'price': 1.061, 'qty': 100},
}

# SELL signal
strategy.bot_state = {'in_position': False, 'side': 'none', 'entry_price': 0.0, 'entry_time': 0.0}
mock_model.predict_proba = lambda X: np.array([[0.70, 0.30]])
sig = strategy.generate_signal(dummy_data)
check("SELL signal for prob_down>0.60", sig['action'] == 'sell', f"got: {sig}")

# BUY signal
strategy.bot_state = {'in_position': False, 'side': 'none', 'entry_price': 0.0, 'entry_time': 0.0}
mock_model.predict_proba = lambda X: np.array([[0.30, 0.70]])
sig2 = strategy.generate_signal(dummy_data)
check("BUY signal for prob_up>0.60", sig2['action'] == 'buy', f"got: {sig2}")

# SHORT SL
strategy.bot_state = {'in_position': True, 'side': 'short', 'entry_price': 1.000, 'entry_time': 0.0}
dummy_data['best_ask'] = {'price': 1.005, 'qty': 100}
sig3 = strategy.generate_signal(dummy_data)
check("SHORT SL triggers", sig3['action'] == 'close' and '[SHORT]' in sig3['reason'], f"got: {sig3}")

# LONG SL
strategy.bot_state = {'in_position': True, 'side': 'long', 'entry_price': 1.000, 'entry_time': 0.0}
dummy_data['best_bid'] = {'price': 0.994, 'qty': 100}
sig4 = strategy.generate_signal(dummy_data)
check("LONG SL triggers", sig4['action'] == 'close' and '[LONG]' in sig4['reason'], f"got: {sig4}")


# =========================================================
# BUG-05: _background_tasks set
# =========================================================
print("\n[BUG-05] Fire-and-forget task GC fix")
import execution
check("_background_tasks exists", hasattr(execution, '_background_tasks'))
check("_background_tasks is set", isinstance(execution._background_tasks, set))
with open('execution.py', encoding='utf-8') as f:
    src_ex = f.read()
check("add_done_callback used", 'add_done_callback' in src_ex)
check("_background_tasks.add(task)", '_background_tasks.add(task)' in src_ex)


# =========================================================
# BUG-06: Strategy loop live candle detection
# =========================================================
print("\n[BUG-06] Strategy loop live candle detection")
with open('main.py', encoding='utf-8') as f:
    src_main = f.read()
check("main.py tracks current_hash", 'current_hash' in src_main)
check("main.py triggers on hash change", 'current_hash != last_current_hash' in src_main)


# =========================================================
# BUG-07: Timestamp-based tick tracking
# =========================================================
print("\n[BUG-07] Timestamp-based resampler race condition fix")
fake_buf = deque(maxlen=5)
for i in range(8):
    fake_buf.append({'timestamp': float(i), 'price': 1.0, 'qty': 1.0, 'side': 'Buy'})
snapshot = list(fake_buf)
last_ts = 4.0
new_ticks = [t for t in snapshot if t['timestamp'] > last_ts]
check("Correct ticks after overflow [5,6,7]",
      [t['timestamp'] for t in new_ticks] == [5.0, 6.0, 7.0])
with open('data_resampler.py', encoding='utf-8') as f:
    src_dr = f.read()
check("_last_processed_ts in resampler", '_last_processed_ts' in src_dr)
code_only = '\n'.join(l for l in src_dr.split('\n') if not l.strip().startswith('#'))
check("No _processed_count in resampler code", '_processed_count' not in code_only)


# =========================================================
# BUG-11: orderbook_imbalance di kline mode
# =========================================================
print("\n[BUG-11] orderbook_imbalance in kline mode")
import candle_stream, data_stream
data_stream.orderbook_snapshot['bids'] = SortedDict({1.0: 100.0, 1.01: 50.0})
data_stream.orderbook_snapshot['asks'] = SortedDict({1.02: 30.0})
imb = candle_stream._get_orderbook_imbalance()
expected = round((150.0 - 30.0) / 180.0, 6)
check("orderbook_imbalance is non-zero", imb != 0.0, f"got {imb}")
check(f"orderbook_imbalance correct ~{expected}", abs(imb - expected) < 0.001, f"{imb} != {expected}")
data_stream.orderbook_snapshot['bids'] = SortedDict()
data_stream.orderbook_snapshot['asks'] = SortedDict()
check("Empty orderbook returns 0.0", candle_stream._get_orderbook_imbalance() == 0.0)


# =========================================================
# NEW-01: Session di luar retry loop (no connection leak)
# =========================================================
print("\n[NEW-01] ClientSession outside retry loop fix")
# Cari fungsi _live_place_order_async saja (bukan _paper_execute_loop)
live_fn_start = src_ex.find('async def _live_place_order_async(')
live_fn_src   = src_ex[live_fn_start:]  # dari fungsi ini sampai akhir file

# Di dalam _live_place_order_async: session harus di LUAR retry loop
session_in_live = live_fn_src.find('async with aiohttp.ClientSession(connector=connector) as session:')
retry_in_live   = live_fn_src.find('for attempt in range(1, MAX_RETRY + 1):')
check("ClientSession created BEFORE retry loop in live fn",
      0 < session_in_live < retry_in_live,
      f"session_pos={session_in_live}, retry_pos={retry_in_live}")
# Tidak boleh ada ClientSession() tanpa connector di dalam _live_place_order_async
check("No bare ClientSession() inside _live_place_order_async",
      'async with aiohttp.ClientSession() as session:' not in live_fn_src,
      "bare ClientSession() still inside live fn")


# =========================================================
# NEW-03: ML_ENABLED guard sebelum scaler/model access
# =========================================================
print("\n[NEW-03] ML_ENABLED guard before scaler/model")
with open('strategy.py', encoding='utf-8') as f:
    src_st = f.read()
check("ML_ENABLED check exists in strategy", 'if not ML_ENABLED:' in src_st)
# Posisi check harus SEBELUM scaler.transform()
ml_guard_pos  = src_st.find('if not ML_ENABLED:')
scaler_pos    = src_st.find('scaler.transform(')
check("ML guard is BEFORE scaler.transform()",
      0 < ml_guard_pos < scaler_pos,
      f"guard={ml_guard_pos}, scaler={scaler_pos}")

# Test fungsional: ML disabled → return hold, bukan exception
strategy.ML_ENABLED = False
strategy.bot_state  = {'in_position': False, 'side': 'none', 'entry_price': 0.0, 'entry_time': 0.0}
sig_no_ml = strategy.generate_signal(dummy_data)
check("ML disabled returns hold (not error)",
      sig_no_ml['action'] == 'hold',
      f"got: {sig_no_ml}")
check("ML disabled reason mentions inactive",
      'tidak aktif' in sig_no_ml.get('reason', '').lower() or 'ML' in sig_no_ml.get('reason', ''),
      f"reason: {sig_no_ml.get('reason')}")
strategy.ML_ENABLED = True  # restore


# =========================================================
# NEW-05: TF_SECONDS dinamis dari config
# =========================================================
print("\n[NEW-05] Dynamic TF_SECONDS from config")
with open('bot_monitor.py', encoding='utf-8') as f:
    src_bm = f.read()
check("bot_monitor builds TF_SECONDS from config",
      'from config import TIMEFRAMES as _tf_cfg' in src_bm)
check("bot_monitor has fallback for \"2h\"",
      '"2h": 7200' in src_bm or '"2h"' in src_bm)
# Test fungsional: "2h" sekarang harus ketemu di TF_SECONDS
from config import TIMEFRAMES
TF_SECONDS_test = {tf: v[0] for tf, v in TIMEFRAMES.items()}
check('"2h" present in dynamic TF_SECONDS',
      '2h' in TF_SECONDS_test,
      f"TIMEFRAMES keys: {list(TIMEFRAMES.keys())}")
check('"2h" maps to 7200s',
      TF_SECONDS_test.get('2h') == 7200,
      f"got {TF_SECONDS_test.get('2h')}")


# =========================================================
# OLD-08: REST_MARKET_URL di config dan main.py
# =========================================================
print("\n[OLD-08] REST_MARKET_URL for market data")
with open('config.py', encoding='utf-8') as f:
    src_cfg = f.read()
check("config.py defines REST_MARKET_URL", 'REST_MARKET_URL' in src_cfg)
check("REST_MARKET_URL = REST_BASE_URL", 'REST_MARKET_URL = REST_BASE_URL' in src_cfg)
check("main.py imports REST_MARKET_URL", 'REST_MARKET_URL' in src_main)
check("main.py uses REST_MARKET_URL for preload", 'base_url = REST_MARKET_URL' in src_main)
check("main.py NOT hardcodes REST_BASE_URL for preload",
      'base_url = REST_BASE_URL' not in src_main)
# Verify the constant value
from config import REST_MARKET_URL, REST_BASE_URL
check("REST_MARKET_URL == REST_BASE_URL (always live)",
      REST_MARKET_URL == REST_BASE_URL)


# =========================================================
# OLD-09: initial_sync() di position_manager
# =========================================================
print("\n[OLD-09] initial_sync() before strategy starts")
check("position_manager has initial_sync()", 'async def initial_sync()' in src_pm)
check("initial_sync skips paper mode",
      "if MODE == \"paper\":" in src_pm and 'return  # paper mode' in src_pm)
check("main.py calls initial_sync() for demo/live",
      'await position_manager.initial_sync()' in src_main)
check("main.py guards initial_sync with MODE check",
      'if MODE in ("demo", "live"):' in src_main and
      'await position_manager.initial_sync()' in src_main)
# Test fungsional: paper mode initial_sync() returns immediately (no await needed)
import position_manager
async def _test_initial_sync_paper():
    from config import MODE
    if MODE == "paper":
        # Should return immediately without network call
        await position_manager.initial_sync()
        return True
    return True
result = asyncio.run(_test_initial_sync_paper())
check("initial_sync() runs without error in paper mode", result is True)


# =========================================================
# SUMMARY
# =========================================================
print("\n" + "=" * 56)
print(f"  TOTAL: {PASS} PASSED | {FAIL} FAILED")
print("=" * 56)
sys.exit(0 if FAIL == 0 else 1)
