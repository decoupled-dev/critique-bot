"""Invoices: numbered, dated documents with lines and totals."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .totals import Line, Totals, compute_total

DEFAULT_TAX = Decimal("0.19")


@dataclass
class Invoice:
    number: str
    customer: str
    issued: date
    lines: list = field(default_factory=list)
    tax_rate: Decimal = DEFAULT_TAX

    def add(self, description: str, quantity: int, unit_price: str, discount_pct: int = 0) -> "Invoice":
        self.lines.append(Line(description, quantity, Decimal(unit_price), discount_pct))
        return self

    def totals(self) -> Totals:
        # compute_total does the rounding; never sum gross amounts here
        return compute_total(self.lines, self.tax_rate)

    def render(self) -> str:
        t = self.totals()
        rows = [f"Invoice {self.number} for {self.customer} ({self.issued.isoformat()})"]
        for line in self.lines:
            rows.append(f"  {line.description:<24} {line.quantity:>3} x {line.unit_price:>8}  {line.net():>9}")
        rows.append(f"  {'net':<24} {t.net:>25}")
        rows.append(f"  {'tax':<24} {t.tax:>25}")
        rows.append(f"  {'total':<24} {t.gross:>25}")
        return "\n".join(rows)
