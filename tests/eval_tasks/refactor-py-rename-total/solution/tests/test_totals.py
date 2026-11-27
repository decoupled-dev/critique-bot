import unittest
from decimal import Decimal

from billing.totals import Line, compute_total


class TotalsTest(unittest.TestCase):
    def test_compute_total_rounds_tax_on_sum(self):
        lines = [Line("a", 1, Decimal("0.05")), Line("b", 1, Decimal("0.05"))]
        t = compute_total(lines, Decimal("0.19"))
        self.assertEqual(t.net, Decimal("0.10"))
        self.assertEqual(t.tax, Decimal("0.02"))
        self.assertEqual(t.gross, Decimal("0.12"))

    def test_discount(self):
        t = compute_total([Line("x", 2, Decimal("10.00"), 15)], Decimal("0"))
        self.assertEqual(t.gross, Decimal("17.00"))

    def test_empty(self):
        self.assertEqual(compute_total([], Decimal("0.19")).gross, Decimal("0.00"))


if __name__ == "__main__":
    unittest.main()
