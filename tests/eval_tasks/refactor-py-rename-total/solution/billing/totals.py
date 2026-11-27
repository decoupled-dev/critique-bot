"""Money math shared by invoices, quotes and reports."""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


@dataclass(frozen=True)
class Line:
    description: str
    quantity: int
    unit_price: Decimal
    discount_pct: int = 0

    def net(self) -> Decimal:
        gross = self.unit_price * self.quantity
        return (gross * (100 - self.discount_pct) / 100).quantize(CENT, ROUND_HALF_UP)


@dataclass(frozen=True)
class Totals:
    net: Decimal
    tax: Decimal
    gross: Decimal


def compute_total(lines, tax_rate: Decimal) -> Totals:
    """Sum ``lines`` and add tax at ``tax_rate`` (e.g. Decimal("0.19")).

    Tax is computed on the summed net amount, rounded half-up to cents.
    """
    net = sum((line.net() for line in lines), Decimal("0.00"))
    tax = (net * tax_rate).quantize(CENT, ROUND_HALF_UP)
    return Totals(net=net, tax=tax, gross=net + tax)
