# =============================================================================
# tests/test_precision.py — Fixed-point precision (InstrumentSpec, round_money)
# =============================================================================
# Pola ala Nautilus: make_price/make_qty membulatkan ke presisi instrumen pakai
# Decimal, menghilangkan float drift dan mencegah qty/ harga invalid.
# =============================================================================
from __future__ import annotations

from decimal import Decimal

import pytest

from precision import InstrumentSpec, round_money, to_decimal


# =============================================================================
# InstrumentSpec — derivasi presisi
# =============================================================================
class TestInstrumentSpecInit:
    def test_precision_derived_from_increment(self):
        spec = InstrumentSpec(price_increment="0.0001", size_increment="0.1")
        assert spec.price_precision == 4
        assert spec.size_precision == 1

    def test_integer_increment_zero_precision(self):
        spec = InstrumentSpec(price_increment="1", size_increment="1")
        assert spec.price_precision == 0
        assert spec.size_precision == 0

    def test_trailing_zero_increment_normalized(self):
        # "0.10" tick sebenarnya presisi 1 (0.1), bukan 2
        spec = InstrumentSpec(price_increment="0.10", size_increment="0.010")
        assert spec.price_precision == 1
        assert spec.size_precision == 2

    def test_rejects_nonpositive_increment(self):
        with pytest.raises(ValueError):
            InstrumentSpec(price_increment="0", size_increment="0.1")
        with pytest.raises(ValueError):
            InstrumentSpec(price_increment="0.1", size_increment="-0.1")


# =============================================================================
# make_qty — floor ke step, raise kalau nol
# =============================================================================
class TestMakeQty:
    def test_floors_to_step(self):
        spec = InstrumentSpec("0.0001", "0.1")
        # 4.56 XRP @ step 0.1 -> 4.5 (floor, jangan over-order)
        assert spec.make_qty(4.56) == Decimal("4.5")

    def test_exact_multiple_unchanged(self):
        spec = InstrumentSpec("0.0001", "0.1")
        assert spec.make_qty(4.4) == Decimal("4.4")

    def test_step_half_unit(self):
        """Step 0.5 bukan sekadar 1 desimal — harus kelipatan 0.5."""
        spec = InstrumentSpec("0.01", "0.5")
        assert spec.make_qty(1.7) == Decimal("1.5")
        assert spec.make_qty(2.0) == Decimal("2.0")

    def test_integer_step(self):
        spec = InstrumentSpec("0.01", "1")
        assert spec.make_qty(4.9) == Decimal("4")

    def test_raises_when_rounds_to_zero(self):
        spec = InstrumentSpec("0.01", "0.1")
        with pytest.raises(ValueError):
            spec.make_qty(0.05)   # < 1 step -> nol -> invalid

    def test_matches_old_round_to_step_for_clean_values(self):
        """Untuk nilai bebas-drift, make_qty == _round_to_step lama (ukuran order tak berubah)."""
        import math

        def old_round_to_step(value: float, step: float) -> float:
            precision = max(0, int(round(-math.log10(step))))
            return round(math.floor(value / step) * step, precision)

        spec = InstrumentSpec("0.0001", "0.1")
        for raw in (4.56, 4.4, 4.39, 9.0, 123.45):
            assert float(spec.make_qty(raw)) == pytest.approx(old_round_to_step(raw, 0.1))

    def test_fixes_float_drift_bug_in_old_round_to_step(self):
        """
        BUKTI NILAI: input 0.3 step 0.1.
        _round_to_step lama (float): floor(0.3/0.1)*0.1 = floor(2.9999...)*0.1 = 0.2
        -> KEHILANGAN 1 step penuh karena drift float.
        make_qty (Decimal): 0.3 -> benar.
        """
        import math

        def old_round_to_step(value: float, step: float) -> float:
            precision = max(0, int(round(-math.log10(step))))
            return round(math.floor(value / step) * step, precision)

        spec = InstrumentSpec("0.0001", "0.1")
        assert old_round_to_step(0.3, 0.1) == pytest.approx(0.2)   # bug lama terkonfirmasi
        assert spec.make_qty(0.3) == Decimal("0.3")                # perbaikan


# =============================================================================
# make_price — bulat ke tick terdekat
# =============================================================================
class TestMakePrice:
    def test_rounds_to_nearest_tick(self):
        spec = InstrumentSpec("0.0001", "0.1")
        assert spec.make_price(1.31635) == Decimal("1.3164")  # 1.31635 -> nearest 0.0001
        assert spec.make_price(1.31634) == Decimal("1.3163")

    def test_exact_tick_unchanged(self):
        spec = InstrumentSpec("0.01", "0.1")
        assert spec.make_price(50000.01) == Decimal("50000.01")


# =============================================================================
# round_money — pengganti round(x, 8) tanpa drift
# =============================================================================
class TestRoundMoney:
    def test_basic_rounding(self):
        assert round_money("1.234567895", 8) == Decimal("1.23456790")

    def test_no_float_drift(self):
        # 0.1 + 0.2 = 0.30000000000000004 di float; lewat Decimal jadi eksak
        assert round_money(to_decimal("0.1") + to_decimal("0.2"), 8) == Decimal("0.30000000")

    def test_fee_calculation_exact(self):
        # fee = qty * price * fee_rate, contoh nyata XRP
        fee = to_decimal("4.4") * to_decimal("1.31635") * to_decimal("0.0002")
        assert round_money(fee, 8) == Decimal("0.00115839")


# =============================================================================
# to_decimal — konversi aman
# =============================================================================
class TestToDecimal:
    def test_float_via_str_is_exact(self):
        assert to_decimal(0.1) == Decimal("0.1")        # bukan 0.1000000000...0055
        assert to_decimal(1.5) == Decimal("1.5")

    def test_passthrough_decimal(self):
        d = Decimal("3.14")
        assert to_decimal(d) is d
