#██████╗ ██╗███████╗██╗  ██╗██╗   ██╗    ██╗  ██╗███████╗███╗   ██╗ ██████╗ ██╗  ██╗███████╗██████╗ ██████╗ ██████╗  ██████╗ 
#██╔══██╗██║╚══███╔╝██║ ██╔╝╚██╗ ██╔╝    ██║  ██║██╔════╝████╗  ██║██╔════╝ ██║ ██╔╝██╔════╝██╔══██╗██╔══██╗██╔══██╗██╔═══██╗
#██████╔╝██║  ███╔╝ █████╔╝  ╚████╔╝     ███████║█████╗  ██╔██╗ ██║██║  ███╗█████╔╝ █████╗  ██████╔╝██████╔╝██████╔╝██║   ██║
#██╔══██╗██║ ███╔╝  ██╔═██╗   ╚██╔╝      ██╔══██║██╔══╝  ██║╚██╗██║██║   ██║██╔═██╗ ██╔══╝  ██╔══██╗██╔═══╝ ██╔══██╗██║   ██║
#██║  ██║██║███████╗██║  ██╗   ██║       ██║  ██║███████╗██║ ╚████║╚██████╔╝██║  ██╗███████╗██║  ██║██║     ██║  ██║╚██████╔╝
#╚═╝  ╚═╝╚═╝╚══════╝╚═╝  ╚═╝   ╚═╝       ╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝  ╚═╝ ╚═════╝                                                                                                                             
# if you copy, haram, unless you ask permission from the author
# for personal use only, if you use it for commercial purposes, you will be responsible for your own actions
# =============================================================================
# trade_logger.py — Non-blocking CSV Logger for Trades & Equity Curve
# =============================================================================
from __future__ import annotations

import csv
import os
import time
import queue
import threading
import logging
from typing import Any
from config import TRADE_HISTORY_FILE, EQUITY_CURVE_FILE
from ft_types import EquityLogRow, TradeLogRow

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [LOGGER] %(levelname)s: %(message)s"
)
logger = logging.getLogger("trade_logger")

# =============================================================================
# CSV HEADERS
# =============================================================================
TRADE_FIELDS: list[str]  = ["timestamp", "symbol", "side", "entry_price", "exit_price", "qty", "fee", "pnl", "reason"]
EQUITY_FIELDS: list[str] = ["timestamp", "balance", "equity", "unrealized_pnl"]


# =============================================================================
# BACKGROUND WRITER (daemon thread for non-blocking I/O)
# =============================================================================
_trade_queue: queue.Queue[dict[str, Any]]  = queue.Queue()
_equity_queue: queue.Queue[dict[str, Any]] = queue.Queue()
_shutdown: threading.Event = threading.Event()


def _ensure_csv(filepath: str, fieldnames: list[str]) -> None:
    """Create CSV file with headers if it doesn't exist."""
    if not os.path.exists(filepath):
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
        logger.info(f"Created log file: {filepath}")


def _writer_thread() -> None:
    """Background thread: flush queues to disk continuously."""
    _ensure_csv(TRADE_HISTORY_FILE, TRADE_FIELDS)
    _ensure_csv(EQUITY_CURVE_FILE, EQUITY_FIELDS)

    while not _shutdown.is_set():
        # Drain trade queue
        while not _trade_queue.empty():
            try:
                row: dict[str, Any] = _trade_queue.get_nowait()
                _write_row(TRADE_HISTORY_FILE, TRADE_FIELDS, row)
            except queue.Empty:
                break
            except Exception as e:
                logger.error(f"Trade write error: {e}")

        # Drain equity queue
        while not _equity_queue.empty():
            try:
                row = _equity_queue.get_nowait()
                _write_row(EQUITY_CURVE_FILE, EQUITY_FIELDS, row)
            except queue.Empty:
                break
            except Exception as e:
                logger.error(f"Equity write error: {e}")

        time.sleep(0.1)  # Write cycle: 100ms


def _write_row(filepath: str, fieldnames: list[str], row: dict[str, Any]) -> None:
    """Append a single row to a CSV file safely."""
    with open(filepath, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writerow(row)


# Start background writer thread
_writer: threading.Thread = threading.Thread(target=_writer_thread, daemon=True, name="csv-writer")
_writer.start()


# =============================================================================
# PUBLIC API
# =============================================================================
def log_trade(trade: TradeLogRow) -> None:
    """
    Queue a trade for CSV logging (non-blocking).

    Expected fields:
        timestamp, symbol, side, entry_price, exit_price, qty, pnl, reason
    """
    # Normalize
    row: dict[str, Any] = {
        "timestamp":   trade.get("timestamp", time.time()),
        "symbol":      trade.get("symbol", ""),
        "side":        trade.get("side", ""),
        "entry_price": trade.get("entry_price", ""),
        "exit_price":  trade.get("exit_price", ""),
        "qty":         trade.get("qty", ""),
        "fee":         trade.get("fee", ""),
        "pnl":         trade.get("pnl", ""),
        "reason":      trade.get("reason", ""),
    }
    _trade_queue.put(row)


def log_equity(equity: EquityLogRow) -> None:
    """
    Queue an equity snapshot for CSV logging (non-blocking).

    Expected fields:
        timestamp, balance, equity, unrealized_pnl
    """
    row: dict[str, Any] = {
        "timestamp":       equity.get("timestamp", time.time()),
        "balance":         equity.get("balance", 0.0),
        "equity":          equity.get("equity", 0.0),
        "unrealized_pnl":  equity.get("unrealized_pnl", 0.0),
    }
    _equity_queue.put(row)


def shutdown() -> None:
    """Gracefully shut down the writer thread (flush all queued rows)."""
    logger.info("Shutting down trade logger...")
    _shutdown.set()
    _writer.join(timeout=5)
    logger.info("Trade logger shut down.")
