# =============================================================================
# tests/test_money_invariants.py — Jalur UANG: invarian akunting & rekonsiliasi
# =============================================================================
# Melengkapi tests/test_position_manager.py dengan invarian "uang tidak bocor",
# rekonsiliasi optimistic↔sync, dan 1 property test atas urutan open/close acak.
#
# Peta ke spec densitas test 1.5:1 (jalur uang):
#   B. Invarian akunting       → TestAccountingInvariants
#   C. Rekonsiliasi state/race → TestReconciliation
#   D. Property-based          → TestMoneyMathProperties
#
# Yang SUDAH dicover di test_position_manager.py (tidak diduplikasi):
#   - fee on entry, parser balance/posisi (skip vs update), sync preserve-on-error,
#     clear-on-flat dasar.
# =============================================================================
from __future__ import annotations

import asyncio
import math

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

# Parser/akunting pure-ish — import langsung modul aslinya (no real I/O di jalur ini).
import position_manager  # noqa: E402


_DEFAULT_STATE = {
    "side": "none", "entry_price": 0.0, "qty": 0.0, "balance": 1000.0,
    "unrealized_pnl": 0.0, "equity": 1000.0, "realized_pnl_total": 0.0,
    "total_fees": 0.0, "open_time": None,
}


def _reset_state() -> None:
    position_manager._state.update(dict(_DEFAULT_STATE))


@pytest.fixture
def pm(monkeypatch):
    """position_manager dengan state fresh + MODE=paper (supaya update_pnl jalan)."""
    _reset_state()
    monkeypatch.setattr(position_manager, "MODE", "paper")
    return position_manager


# =============================================================================
# B. INVARIAN AKUNTING — uang tidak boleh "bocor"
# =============================================================================
class TestAccountingInvariants:
    def test_close_long_pnl_exact(self, pm):
        """balance = initial − entry_fee + ((exit−entry)*qty − exit_fee), PERSIS."""
        initial = pm._state["balance"]
        entry_fee, exit_fee = 0.03, 0.032
        pm.open_position("long", entry_price=1.5, qty=100, entry_fee=entry_fee)
        net, fee = pm.close_position(exit_price=1.6, exit_fee=exit_fee)

        gross = (1.6 - 1.5) * 100  # 10.0
        assert net == pytest.approx(gross - exit_fee)
        assert fee == pytest.approx(exit_fee)
        assert pm._state["balance"] == pytest.approx(initial - entry_fee + (gross - exit_fee))
        assert pm._state["realized_pnl_total"] == pytest.approx(gross - exit_fee)
        assert pm._state["total_fees"] == pytest.approx(entry_fee + exit_fee)

    def test_close_short_pnl_exact(self, pm):
        """SHORT: gross = (entry−exit)*qty; identitas akunting yang sama."""
        initial = pm._state["balance"]
        entry_fee, exit_fee = 0.02, 0.018
        pm.open_position("short", entry_price=1.5, qty=200, entry_fee=entry_fee)
        net, _ = pm.close_position(exit_price=1.45, exit_fee=exit_fee)

        gross = (1.5 - 1.45) * 200  # 10.0
        assert net == pytest.approx(gross - exit_fee)
        assert pm._state["balance"] == pytest.approx(initial - entry_fee + (gross - exit_fee))
        assert pm._state["total_fees"] == pytest.approx(entry_fee + exit_fee)

    @pytest.mark.parametrize(
        "side,entry,bid,ask",
        [("long", 1.5, 1.62, 1.63), ("short", 1.5, 1.37, 1.38)],
    )
    def test_equity_equals_balance_plus_unrealized(self, pm, side, entry, bid, ask):
        """Invarian SETELAH update tick: equity == balance + unrealized."""
        pm.open_position(side, entry_price=entry, qty=100, entry_fee=0.0)
        pm.update_pnl(bid_price=bid, ask_price=ask)
        assert pm._state["equity"] == pytest.approx(
            pm._state["balance"] + pm._state["unrealized_pnl"]
        )

    def test_equity_equals_balance_when_flat(self, pm):
        """Tidak ada posisi → unrealized 0, equity == balance."""
        pm.update_pnl(bid_price=1.5, ask_price=1.51)
        assert pm._state["unrealized_pnl"] == 0.0
        assert pm._state["equity"] == pytest.approx(pm._state["balance"])

    def test_open_does_not_change_realized_or_qty_negative(self, pm):
        """Buka posisi tidak menyentuh realized_pnl_total; qty tidak pernah negatif."""
        pm.open_position("long", entry_price=1.5, qty=100, entry_fee=0.05)
        assert pm._state["realized_pnl_total"] == 0.0
        assert pm._state["qty"] >= 0.0


# =============================================================================
# C. REKONSILIASI STATE & RACE — optimistic vs sync
# =============================================================================
class TestReconciliation:
    def test_optimistic_set_then_sync_overwrites_money_fields(self, pm, monkeypatch):
        """
        set_position_optimistic catat posisi TANPA sentuh uang. Lalu _sync_position
        + _sync_balance dgn data exchange → sync MENANG utk entry/qty/unrealized &
        balance. (Akunting uang milik sync loop, bukan optimistic.)
        """
        pm._state["balance"] = 1000.0
        pm._state["realized_pnl_total"] = 5.0
        pm._state["total_fees"] = 2.0

        # 1. Optimistic set — money fields HARUS tetap.
        pm.set_position_optimistic("long", 1.50, 100.0)
        assert pm._state["side"] == "long"
        assert pm._state["qty"] == 100.0
        assert pm._state["balance"] == 1000.0
        assert pm._state["realized_pnl_total"] == 5.0
        assert pm._state["total_fees"] == 2.0

        # 2. Sync authoritative dari exchange (nilai BEDA dari optimistic).
        async def _pos(*a, **kw):
            return [{"side": "long", "contracts": 98.5, "entryPrice": 1.5005, "unrealizedPnl": 3.3}]

        async def _bal(*a, **kw):
            return {"USDT": {"total": 1234.56}}

        monkeypatch.setattr(pm.exchange, "fetch_positions", _pos, raising=False)
        monkeypatch.setattr(pm.exchange, "fetch_balance", _bal, raising=False)

        asyncio.run(pm._sync_position())
        asyncio.run(pm._sync_balance())

        # Sync menang untuk posisi & uang.
        assert pm._state["qty"] == pytest.approx(98.5)
        assert pm._state["entry_price"] == pytest.approx(1.5005)
        assert pm._state["unrealized_pnl"] == pytest.approx(3.3)
        assert pm._state["balance"] == pytest.approx(1234.56)

    def test_sync_says_flat_while_bot_thinks_long_reconciles(self, pm, monkeypatch):
        """Bot internal long (optimistic), exchange bilang flat → bot ikut exchange."""
        pm.set_position_optimistic("long", 1.50, 100.0)
        assert pm._state["side"] == "long"

        async def _flat(*a, **kw):
            return []  # exchange: tidak ada posisi

        monkeypatch.setattr(pm.exchange, "fetch_positions", _flat, raising=False)
        asyncio.run(pm._sync_position())

        assert pm._state["side"] == "none", "bot tidak boleh nyangkut di posisi hantu"
        assert pm._state["qty"] == 0.0
        assert pm._state["entry_price"] == 0.0
        assert pm._state["unrealized_pnl"] == 0.0


# =============================================================================
# D. PROPERTY-BASED — urutan open/close acak, akunting tidak pernah bocor
# =============================================================================
# Tidak pakai fixture (hindari function_scoped_fixture health check); reset state
# manual di awal tiap example. open/close MODE-agnostik (tidak panggil I/O).
_price = st.floats(min_value=0.01, max_value=1000.0, allow_nan=False, allow_infinity=False)
_qty = st.floats(min_value=0.001, max_value=10_000.0, allow_nan=False, allow_infinity=False)
_fee = st.floats(min_value=0.0, max_value=5.0, allow_nan=False, allow_infinity=False)

_ops = st.lists(
    st.fixed_dictionaries({
        "side": st.sampled_from(["long", "short"]),
        "entry": _price,
        "qty": _qty,
        "entry_fee": _fee,
        "exit": _price,
        "exit_fee": _fee,
    }),
    min_size=0, max_size=40,
)


class TestMoneyMathProperties:
    # deadline=None: test ini MODE-agnostik & murni aritmetika, tapi di suite
    # penuh (CPU sibuk) satu example bisa melar > deadline default 200ms → gagal
    # TIMING palsu, bukan bocor uang. Matikan deadline; cakupan tetap 200 example.
    @settings(max_examples=200, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(ops=_ops)
    def test_balance_never_loses_money_unexpectedly(self, ops):
        """
        Untuk URUTAN open/close APA PUN:
          balance akhir == initial + Σ(gross_realized) − Σ(semua fee).
        Plus invarian per-langkah: balance/equity finite, qty tidak pernah negatif.

        Mirror akunting dihitung dengan operasi & urutan SAMA seperti produksi
        (open: −entry_fee; close: +(gross−exit_fee)), jadi tidak ada drift float
        palsu — kalau beda, berarti produksi memang bocor.
        """
        _reset_state()
        initial = position_manager._state["balance"]
        expected_balance = initial

        flat = True
        cur_side = cur_entry = cur_qty = None

        for op in ops:
            if flat:
                position_manager.open_position(
                    op["side"], op["entry"], op["qty"], entry_fee=op["entry_fee"]
                )
                expected_balance -= op["entry_fee"]
                cur_side, cur_entry, cur_qty = op["side"], op["entry"], op["qty"]
                flat = False
            else:
                if cur_side == "long":
                    gross = (op["exit"] - cur_entry) * cur_qty
                else:
                    gross = (cur_entry - op["exit"]) * cur_qty
                position_manager.close_position(exit_price=op["exit"], exit_fee=op["exit_fee"])
                expected_balance += gross - op["exit_fee"]
                flat = True

            # Invarian per-langkah (harus selalu benar, apapun urutannya).
            assert math.isfinite(position_manager._state["balance"])
            assert math.isfinite(position_manager._state["equity"])
            assert position_manager._state["qty"] >= 0.0
            assert position_manager._state["side"] in ("long", "short", "none")

        # Identitas akunting akhir — tidak ada uang yang hilang/muncul misterius.
        assert position_manager._state["balance"] == pytest.approx(
            expected_balance, rel=1e-9, abs=1e-6
        )

    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(side=st.sampled_from(["long", "short"]), entry=_price, qty=_qty, exit_=_price)
    def test_round_trip_no_fee_matches_gross(self, side, entry, qty, exit_):
        """Tanpa fee: balance akhir == initial + gross persis (round-trip bersih)."""
        _reset_state()
        initial = position_manager._state["balance"]
        position_manager.open_position(side, entry, qty, entry_fee=0.0)
        position_manager.close_position(exit_price=exit_, exit_fee=0.0)

        gross = (exit_ - entry) * qty if side == "long" else (entry - exit_) * qty
        assert position_manager._state["balance"] == pytest.approx(initial + gross, rel=1e-9, abs=1e-6)
        assert position_manager._state["side"] == "none"
        assert position_manager._state["qty"] == 0.0
