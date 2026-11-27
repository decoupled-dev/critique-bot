"""Quotes: like invoices but with a validity period and optional items."""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from . import totals as totals_mod


@dataclass
class Quote:
    customer: str
    issued: date
    valid_days: int = 30
    lines: list = field(default_factory=list)
    optional: list = field(default_factory=list)
    tax_rate: Decimal = Decimal("0.19")

    @property
    def valid_until(self) -> date:
        return self.issued + timedelta(days=self.valid_days)

    def total(self, with_optional: bool = False):
        lines = self.lines + (self.optional if with_optional else [])
        return totals_mod.calc_tot(lines, self.tax_rate)

    def price_range(self) -> tuple[Decimal, Decimal]:
        return self.total().gross, self.total(with_optional=True).gross
