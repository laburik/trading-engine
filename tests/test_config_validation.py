# =============================================================================
# tests/test_config_validation.py — Unit test untuk validasi user/config.py
# =============================================================================
# validate_config() menerima objek cfg (default = modul config). Kita oper
# SimpleNamespace valid lalu rusak SATU field per test → pastikan error muncul.
# =============================================================================
from __future__ import annotations

import types

import pytest

import config


def _valid_cfg() -> types.SimpleNamespace:
    """Namespace yang mencerminkan default config.py (semua nilai valid)."""
    return types.SimpleNamespace(
        EXCHANGE="bybit",
        SYMBOL="XRPUSDT",
        CATEGORY="linear",
        MODE="demo",
        STRATEGY_FILE="strategy",
        INITIAL_BALANCE=1000.0,
        LEVERAGE=3,
        ORDER_SIZE_USDT=2,
        SLIPPAGE_TOLERANCE=0.0005,
        MAX_RETRY=3,
        RETRY_DELAY_MS=20,
        CANCEL_ON_PARTIAL=False,
        FEE_RATE=0.0002,
        BACKTEST_SLIPPAGE=0.0002,
        BACKTEST_HALF_SPREAD=0.0001,
        BACKTEST_FUNDING_RATE=0.0001,
        ORDERBOOK_DEPTH=50,
        FUNDING_RATE_INTERVAL_SEC=1200,
        DATA_MODE="kline",
        TICK_BUFFER_SIZE=10000,
        ORDERBOOK_BUFFER_SIZE=100,
        TIMEFRAMES={"1m": (60, 200), "15m": (900, 50), "2h": (7200, 100)},
        LOGS_DIR="logs",
        HEARTBEAT_INTERVAL_SEC=1,
        HEARTBEAT_TIMEOUT_SEC=5,
        PNL_SYNC_INTERVAL=2,
    )


# =============================================================================
# Happy path
# =============================================================================
def test_valid_namespace_passes():
    assert config.validate_config(_valid_cfg()) == []


def test_real_module_defaults_valid():
    """config.py default (yang ter-import) HARUS lolos — kalau tidak, import
    config bakal SystemExit dan merusak seluruh suite."""
    assert config.validate_config() == []


# =============================================================================
# Enum / pilihan
# =============================================================================
@pytest.mark.parametrize("field,bad", [
    ("CATEGORY", "spot"),
    ("MODE", "production"),
    ("DATA_MODE", "candle"),
])
def test_bad_enum(field, bad):
    cfg = _valid_cfg()
    setattr(cfg, field, bad)
    errs = config.validate_config(cfg)
    assert any(field in e for e in errs)


# =============================================================================
# String non-empty
# =============================================================================
@pytest.mark.parametrize("field", ["EXCHANGE", "SYMBOL", "STRATEGY_FILE", "LOGS_DIR"])
def test_empty_string_rejected(field):
    cfg = _valid_cfg()
    setattr(cfg, field, "")
    assert any(field in e for e in config.validate_config(cfg))


def test_strategy_file_with_py_suffix():
    cfg = _valid_cfg()
    cfg.STRATEGY_FILE = "strategy.py"
    assert any("STRATEGY_FILE" in e for e in config.validate_config(cfg))


# =============================================================================
# Numeric range
# =============================================================================
@pytest.mark.parametrize("field,bad", [
    ("INITIAL_BALANCE", 0),       # harus > 0
    ("INITIAL_BALANCE", -10),
    ("LEVERAGE", 0),              # harus >= 1
    ("ORDER_SIZE_USDT", 0),       # harus > 0
    ("FEE_RATE", 1),             # harus < 1
    ("FEE_RATE", -0.1),          # harus >= 0
    ("SLIPPAGE_TOLERANCE", 1.0),  # harus < 1
    ("RETRY_DELAY_MS", -1),
    ("FUNDING_RATE_INTERVAL_SEC", 0),
    ("PNL_SYNC_INTERVAL", 0),
])
def test_bad_numeric(field, bad):
    cfg = _valid_cfg()
    setattr(cfg, field, bad)
    assert any(field in e for e in config.validate_config(cfg))


@pytest.mark.parametrize("field", ["INITIAL_BALANCE", "FEE_RATE", "LEVERAGE"])
def test_non_numeric_type(field):
    cfg = _valid_cfg()
    setattr(cfg, field, "banyak")
    assert any(field in e for e in config.validate_config(cfg))


# =============================================================================
# Integer fields (tolak float/bool)
# =============================================================================
@pytest.mark.parametrize("field", ["MAX_RETRY", "TICK_BUFFER_SIZE", "ORDERBOOK_BUFFER_SIZE"])
def test_int_field_rejects_float(field):
    cfg = _valid_cfg()
    setattr(cfg, field, 3.5)
    assert any(field in e for e in config.validate_config(cfg))


def test_bool_field_rejects_non_bool():
    cfg = _valid_cfg()
    cfg.CANCEL_ON_PARTIAL = "yes"
    assert any("CANCEL_ON_PARTIAL" in e for e in config.validate_config(cfg))


# =============================================================================
# ORDERBOOK_DEPTH khusus bybit linear
# =============================================================================
def test_bad_bybit_depth():
    cfg = _valid_cfg()
    cfg.ORDERBOOK_DEPTH = 42           # bukan 1/50/200/500
    assert any("ORDERBOOK_DEPTH" in e for e in config.validate_config(cfg))


def test_depth_42_ok_for_non_bybit():
    cfg = _valid_cfg()
    cfg.EXCHANGE = "binance"           # batasan depth bybit tidak berlaku
    cfg.ORDERBOOK_DEPTH = 42
    assert config.validate_config(cfg) == []


# =============================================================================
# TIMEFRAMES
# =============================================================================
def test_timeframes_empty():
    cfg = _valid_cfg()
    cfg.TIMEFRAMES = {}
    assert any("TIMEFRAMES" in e for e in config.validate_config(cfg))


def test_timeframes_not_dict():
    cfg = _valid_cfg()
    cfg.TIMEFRAMES = [("1m", 60, 200)]
    assert any("TIMEFRAMES" in e for e in config.validate_config(cfg))


def test_timeframes_bad_spec_shape():
    cfg = _valid_cfg()
    cfg.TIMEFRAMES = {"1m": (60,)}     # harus 2 elemen
    assert any("1m" in e for e in config.validate_config(cfg))


def test_timeframes_bad_interval():
    cfg = _valid_cfg()
    cfg.TIMEFRAMES = {"1m": (0, 200)}  # interval harus > 0
    assert any("1m" in e for e in config.validate_config(cfg))


def test_timeframes_bad_maxcandle():
    cfg = _valid_cfg()
    cfg.TIMEFRAMES = {"1m": (60, 0)}   # max_candle harus > 0
    assert any("1m" in e for e in config.validate_config(cfg))


# =============================================================================
# Heartbeat cross-field
# =============================================================================
def test_heartbeat_timeout_not_greater_than_interval():
    cfg = _valid_cfg()
    cfg.HEARTBEAT_INTERVAL_SEC = 5
    cfg.HEARTBEAT_TIMEOUT_SEC = 5      # harus > interval
    assert any("HEARTBEAT_TIMEOUT_SEC" in e for e in config.validate_config(cfg))


# =============================================================================
# Field hilang
# =============================================================================
def test_missing_field_reported():
    cfg = _valid_cfg()
    del cfg.SYMBOL
    assert any("SYMBOL" in e for e in config.validate_config(cfg))


# =============================================================================
# Banyak error sekaligus terkumpul (bukan stop di error pertama)
# =============================================================================
def test_collects_multiple_errors():
    cfg = _valid_cfg()
    cfg.MODE = "x"
    cfg.LEVERAGE = 0
    cfg.FEE_RATE = 2
    errs = config.validate_config(cfg)
    assert len(errs) >= 3
