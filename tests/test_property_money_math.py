# =============================================================================
# tests/test_property_money_math.py — Hypothesis property-based testing
# =============================================================================
# Property tests untuk math / parser di jalur uang.
#
# Berbeda dengan example-based test (pytest biasa), property test menyatakan
# INVARIAN yang harus selalu benar, lalu Hypothesis generate ratusan input
# random buat coba membreak invariant tersebut. Cocok buat:
#   - VWAP calculation (math harus konsisten secara aritmatika)
#   - Parser (output harus selalu well-formed apapun input)
#
# Strategi yang dipakai sengaja dibatasi range supaya generated input realistis
# (harga 0.0001..100_000, qty 0.001..1_000_000) — tidak boros generate input
# yang tidak akan pernah terjadi di production.
# =============================================================================
from __future__ import annotations

import sys
from unittest.mock import MagicMock

from hypothesis import HealthCheck, assume, given, settings, strategies as st
from sortedcontainers import SortedDict


# -----------------------------------------------------------------------------
# Setup: load execution module dengan mocked dependencies (sama dengan
# test_execution.py setup) supaya kita bisa call _calculate_vwap langsung.
# -----------------------------------------------------------------------------
_SAVED: dict[str, object] = {}
for _name in ("ccxt_client", "data_stream", "position_manager", "trade_logger"):
    _SAVED[_name] = sys.modules.get(_name)
    sys.modules[_name] = MagicMock()
_SAVED["execution"] = sys.modules.pop("execution", None)

try:
    import execution  # type: ignore[import-not-found]
finally:
    for _name, _orig in _SAVED.items():
        if _orig is not None:
            sys.modules[_name] = _orig  # type: ignore[assignment]
        else:
            sys.modules.pop(_name, None)

# Import position_manager fresh — parser-nya pure function, gak butuh I/O
import position_manager  # noqa: E402


# =============================================================================
# STRATEGI HYPOTHESIS — generator input realistis
# =============================================================================
# Range harga: pakai crypto-realistic spread (XRP/USDT ~ $0.50, BTC ~ $50k).
# allow_nan/infinity=False supaya gak hit edge case float yang gak pernah
# terjadi di production.
_price_strat = st.floats(
    min_value=0.0001, max_value=100_000.0,
    allow_nan=False, allow_infinity=False,
)
_qty_strat = st.floats(
    min_value=0.001, max_value=1_000_000.0,
    allow_nan=False, allow_infinity=False,
)


def _make_book(bids: dict[float, float], asks: dict[float, float]) -> dict:
    """Build orderbook dict (SortedDict) yang mimick production data_stream."""
    return {
        "bids": SortedDict(bids),
        "asks": SortedDict(asks),
    }


# =============================================================================
# PROPERTY TESTS — _calculate_vwap
# =============================================================================
class TestVwapProperties:
    """Invariants yang harus selalu benar untuk _calculate_vwap, apapun input-nya."""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        levels=st.lists(
            st.tuples(_price_strat, _qty_strat),
            min_size=1, max_size=20, unique_by=lambda t: t[0],
        ),
        qty=_qty_strat,
    )
    def test_buy_vwap_never_below_lowest_ask(self, levels, qty):
        """
        Property: VWAP buy harus >= ask level termurah.
        Reason: buy walks ascending — fill pertama di level termurah, VWAP
        bisa naik kalau pindah ke level lebih mahal, tapi tidak pernah lebih
        rendah dari ask termurah.
        """
        execution.data_stream.orderbook_snapshot = _make_book({}, dict(levels))
        vwap, fillable, err = execution._calculate_vwap("buy", qty)
        if vwap is None:
            return  # empty book / invalid case — property tidak applicable
        lowest_ask = min(p for p, _ in levels)
        assert vwap >= lowest_ask - 1e-9

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        levels=st.lists(
            st.tuples(_price_strat, _qty_strat),
            min_size=1, max_size=20, unique_by=lambda t: t[0],
        ),
        qty=_qty_strat,
    )
    def test_sell_vwap_never_above_highest_bid(self, levels, qty):
        """
        Property: VWAP sell harus <= bid level tertinggi.
        Reason: sell walks descending — fill pertama di bid termahal.
        """
        execution.data_stream.orderbook_snapshot = _make_book(dict(levels), {})
        vwap, fillable, err = execution._calculate_vwap("sell", qty)
        if vwap is None:
            return
        highest_bid = max(p for p, _ in levels)
        assert vwap <= highest_bid + 1e-9

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        levels=st.lists(
            st.tuples(_price_strat, _qty_strat),
            min_size=1, max_size=20, unique_by=lambda t: t[0],
        ),
        qty=_qty_strat,
    )
    def test_fillable_never_exceeds_qty(self, levels, qty):
        """Property: fillable ≤ requested qty (tidak bisa over-fill)."""
        execution.data_stream.orderbook_snapshot = _make_book({}, dict(levels))
        vwap, fillable, err = execution._calculate_vwap("buy", qty)
        if vwap is None:
            return
        assert fillable <= qty + 1e-9

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        levels=st.lists(
            st.tuples(_price_strat, _qty_strat),
            min_size=1, max_size=20, unique_by=lambda t: t[0],
        ),
        qty=_qty_strat,
    )
    def test_fillable_never_exceeds_book_depth(self, levels, qty):
        """Property: fillable ≤ total qty di book (tidak bisa fill dari liquidity hantu)."""
        execution.data_stream.orderbook_snapshot = _make_book({}, dict(levels))
        vwap, fillable, err = execution._calculate_vwap("buy", qty)
        if vwap is None:
            return
        total_depth = sum(q for _, q in levels)
        assert fillable <= total_depth + 1e-9

    @settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        price=_price_strat,
        level_qty=_qty_strat,
        order_qty=_qty_strat,
    )
    def test_single_level_vwap_equals_price(self, price, level_qty, order_qty):
        """
        Property: kalau book cuma punya 1 level dan order qty ≤ level qty,
        VWAP harus persis = price level itu.
        """
        assume(order_qty <= level_qty)
        execution.data_stream.orderbook_snapshot = _make_book({}, {price: level_qty})
        vwap, fillable, err = execution._calculate_vwap("buy", order_qty)
        assert vwap is not None
        assert abs(vwap - price) < 1e-6
        assert abs(fillable - order_qty) < 1e-6

    @settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(qty=_qty_strat)
    def test_empty_book_returns_none(self, qty):
        """Property: book kosong → vwap=None, fillable=0, error string."""
        execution.data_stream.orderbook_snapshot = _make_book({}, {})
        vwap, fillable, err = execution._calculate_vwap("buy", qty)
        assert vwap is None
        assert fillable == 0.0
        assert err  # non-empty string

    @settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(qty=st.floats(min_value=-1000, max_value=0))
    def test_non_positive_qty_returns_error(self, qty):
        """Property: qty ≤ 0 selalu return error, tidak pernah valid."""
        execution.data_stream.orderbook_snapshot = _make_book({}, {1.0: 100.0})
        vwap, fillable, err = execution._calculate_vwap("buy", qty)
        assert vwap is None
        assert "Invalid qty" in err

    @settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(side=st.text(min_size=1, max_size=10).filter(lambda s: s not in ("buy", "sell")))
    def test_invalid_side_returns_error(self, side):
        """Property: side selain 'buy'/'sell' selalu error."""
        execution.data_stream.orderbook_snapshot = _make_book({1.0: 10.0}, {1.01: 10.0})
        vwap, _, err = execution._calculate_vwap(side, 5.0)
        assert vwap is None
        assert "Invalid side" in err


# =============================================================================
# PROPERTY TESTS — _parse_position_response
# =============================================================================
# Output parser ada 3 jenis: ("clear", None), ("update", tuple), ("skip", reason).
# Properties: output selalu well-formed, tidak pernah corrupt state lewat
# data dengan bentuk tak terduga.
class TestParsePositionProperties:
    @settings(max_examples=100)
    @given(noise=st.lists(
        st.one_of(st.none(), st.integers(), st.text(), st.floats(allow_nan=False, allow_infinity=False)),
        max_size=10,
    ))
    def test_garbage_input_never_crashes(self, noise):
        """
        Property: parser robust — input apapun (non-dict mixed) tidak boleh
        raise exception. Boleh return ("clear", None) atau ("skip", reason)
        atau ("update", ...) tergantung isi, tapi GAK BOLEH crash.
        """
        try:
            status, payload = position_manager._parse_position_response(noise)
        except Exception as e:
            raise AssertionError(f"Parser crashed on garbage input: {e!r}")
        assert status in ("clear", "update", "skip")

    @settings(max_examples=100)
    @given(
        side=st.sampled_from(["long", "short", "LONG", "SHORT", "Long", "Short"]),
        contracts=st.floats(min_value=0.001, max_value=10000, allow_nan=False, allow_infinity=False),
        entry=st.floats(min_value=0.0001, max_value=100_000, allow_nan=False, allow_infinity=False),
        unreal=st.floats(min_value=-1000, max_value=1000, allow_nan=False, allow_infinity=False),
    )
    def test_valid_position_returns_update_with_normalized_fields(self, side, contracts, entry, unreal):
        """
        Property: input lengkap & valid → status='update', side dinormalisasi
        ke lowercase, semua field numerik > 0.
        """
        pos = {
            "side": side, "contracts": contracts,
            "entryPrice": entry, "unrealizedPnl": unreal,
        }
        status, payload = position_manager._parse_position_response([pos])
        assert status == "update"
        out_side, out_entry, out_qty, out_unreal = payload
        assert out_side in ("long", "short")
        assert out_entry > 0
        assert out_qty > 0
        # Side input case-insensitive → normalized lowercase
        assert out_side == side.lower()

    @settings(max_examples=100)
    @given(
        contracts=st.floats(min_value=0.001, max_value=10000, allow_nan=False, allow_infinity=False),
        bad_entry=st.one_of(
            st.none(),
            st.floats(max_value=0, allow_nan=False, allow_infinity=False),
            st.text(min_size=1, max_size=20).filter(lambda s: not s.replace(".", "").replace("-", "").isdigit()),
        ),
    )
    def test_invalid_entry_price_returns_skip_not_update(self, contracts, bad_entry):
        """
        Property: KASUS BUG yang di-prevent — entryPrice invalid/missing →
        parser TIDAK BOLEH return ("update", ...) yang akan corrupt state lokal
        ke entry_price=0 (PnL = (bid - 0) × qty = infinite).
        Harus return ("skip", reason).
        """
        pos = {"side": "long", "contracts": contracts, "entryPrice": bad_entry}
        status, payload = position_manager._parse_position_response([pos])
        assert status == "skip"
        assert isinstance(payload, str)

    @settings(max_examples=50)
    @given(n_inactive=st.integers(min_value=0, max_value=10))
    def test_all_inactive_positions_return_clear(self, n_inactive):
        """Property: semua position dengan contracts=0 → clear."""
        positions = [
            {"side": "long", "contracts": 0, "entryPrice": 100.0}
            for _ in range(n_inactive)
        ]
        status, payload = position_manager._parse_position_response(positions)
        assert status == "clear"
        assert payload is None

    @settings(max_examples=50)
    @given(
        side=st.sampled_from(["long", "short"]),
        contracts=st.floats(min_value=0.001, max_value=1000, allow_nan=False, allow_infinity=False),
        entry=st.floats(min_value=0.001, max_value=10_000, allow_nan=False, allow_infinity=False),
    )
    def test_active_among_inactive_picked(self, side, contracts, entry):
        """Property: mixed list — yang aktif (contracts > 0) selalu dipilih."""
        positions = [
            {"side": "long", "contracts": 0, "entryPrice": 50.0},  # inactive
            {"side": side, "contracts": contracts, "entryPrice": entry},  # active
            {"side": "short", "contracts": 0, "entryPrice": 200.0},  # inactive
        ]
        status, payload = position_manager._parse_position_response(positions)
        assert status == "update"
        out_side, _, _, _ = payload
        assert out_side == side
