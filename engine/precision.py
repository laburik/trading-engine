# =============================================================================
# precision.py — Fixed-point precision untuk harga/qty/uang (pola ala Nautilus)
# =============================================================================
# Mengapa ada modul ini:
#   Engine lama pakai float mentah + `round(x, 8)` & `_round_to_step()` tersebar.
#   Float math rentan drift (0.1 + 0.2 != 0.3) — di jalur uang/qty itu bahaya:
#   qty bisa meleset 1 step, fee/PnL bisa beda di desimal terakhir.
#
#   Pola NautilusTrader: instrumen menyimpan price_increment & size_increment,
#   lalu SEMUA nilai dibuat lewat factory make_price()/make_qty() yang membulatkan
#   ke presisi instrumen pakai aritmetika fixed-point (Decimal). Hasilnya
#   deterministik & pasti lolos validasi presisi exchange.
#
# Referensi: https://nautilustrader.io/docs/latest/concepts/instruments/
# =============================================================================
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Union

Number = Union[int, float, str, Decimal]


def to_decimal(value: Number) -> Decimal:
    """
    Konversi aman ke Decimal. WAJIB lewat str() supaya tidak mewarisi artefak
    biner float — Decimal(0.1) == 0.1000000000000000055..., tapi
    Decimal(str(0.1)) == Decimal('0.1') (eksak).
    """
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _precision_of(increment: Decimal) -> int:
    """Jumlah desimal dari sebuah increment (0.1 -> 1, 0.001 -> 3, 1 -> 0)."""
    exp = increment.normalize().as_tuple().exponent
    return -exp if isinstance(exp, int) and exp < 0 else 0


class InstrumentSpec:
    """
    Spesifikasi presisi 1 instrumen. Padanan ringan dari Instrument Nautilus
    untuk kebutuhan make_price()/make_qty().

    Args:
        price_increment: tick size harga, mis. "0.0001"
        size_increment:  step qty,        mis. "0.1"

    Keduanya sebaiknya diberikan sebagai str (atau Decimal) agar eksak.
    """

    def __init__(self, price_increment: Number, size_increment: Number) -> None:
        self.price_increment: Decimal = to_decimal(price_increment)
        self.size_increment: Decimal = to_decimal(size_increment)
        if self.price_increment <= 0:
            raise ValueError(f"price_increment harus > 0, dapat {price_increment!r}")
        if self.size_increment <= 0:
            raise ValueError(f"size_increment harus > 0, dapat {size_increment!r}")
        self.price_precision: int = _precision_of(self.price_increment)
        self.size_precision: int = _precision_of(self.size_increment)

    def _quantum(self, precision: int) -> Decimal:
        return Decimal(1).scaleb(-precision)  # 10^-precision

    def make_price(self, value: Number) -> Decimal:
        """
        Bulatkan harga ke tick terdekat (ROUND_HALF_UP) lalu pas-kan ke presisi.
        Harga dibulatkan ke-terdekat (bukan floor) karena harga bukan komitmen
        ukuran — yang penting valid di grid tick exchange.
        """
        d = to_decimal(value)
        ticks = (d / self.price_increment).to_integral_value(rounding=ROUND_HALF_UP)
        result = ticks * self.price_increment
        return result.quantize(self._quantum(self.price_precision))

    def make_qty(self, value: Number) -> Decimal:
        """
        Bulatkan qty ke KELIPATAN size_increment dengan FLOOR (ROUND_DOWN) —
        konservatif: jangan pernah order lebih besar dari yang diniatkan.

        Raise ValueError kalau hasil <= 0 (sama seperti Nautilus make_qty):
        order qty nol = invalid, lebih baik gagal di sini daripada ditolak
        exchange atau (lebih buruk) terkirim sebagai 0.
        """
        d = to_decimal(value)
        steps = (d / self.size_increment).to_integral_value(rounding=ROUND_DOWN)
        result = (steps * self.size_increment).quantize(self._quantum(self.size_precision))
        if result <= 0:
            raise ValueError(
                f"qty {value!r} membulat ke nol pada size_increment={self.size_increment} "
                f"(order terlalu kecil)"
            )
        return result

    def __repr__(self) -> str:
        return (
            f"InstrumentSpec(price_increment={self.price_increment}, "
            f"size_increment={self.size_increment})"
        )


def round_money(value: Number, precision: int = 8) -> Decimal:
    """
    Bulatkan nilai uang (fee/PnL/balance) ke presisi tetap pakai Decimal,
    ROUND_HALF_UP. Pengganti `round(float_x, precision)` yang bisa drift.

    Return Decimal — caller konversi ke float (float(round_money(...))) hanya di
    batas API yang memang butuh float, supaya math internal tetap eksak.
    """
    d = to_decimal(value)
    quant = Decimal(1).scaleb(-precision)
    return d.quantize(quant, rounding=ROUND_HALF_UP)
