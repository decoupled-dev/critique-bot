"""Monthly revenue report."""

from collections import defaultdict
from decimal import Decimal

from .totals import compute_total


def monthly_revenue(invoices) -> dict[str, Decimal]:
    """Gross revenue per ``YYYY-MM``, computed with compute_total per invoice."""
    out: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for inv in invoices:
        key = inv.issued.strftime("%Y-%m")
        out[key] += compute_total(inv.lines, inv.tax_rate).gross
    return dict(sorted(out.items()))


def top_customers(invoices, n: int = 3) -> list[tuple[str, Decimal]]:
    per: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for inv in invoices:
        per[inv.customer] += compute_total(inv.lines, inv.tax_rate).net
    return sorted(per.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
