import unittest
from decimal import Decimal

from billing.totals import Line, calc_tot


class TotalsTest(unittest.TestCase):
    def test_calc_tot_rounds_tax_on_sum(self):
        lines = [Line("a", 1, Decimal("0.05")), Line("b", 1, Decimal("0.05"))]
        t = calc_tot(lines, Decimal("0.19"))
        self.assertEqual(t.net, Decimal("0.10"))
        self.assertEqual(t.tax, Decimal("0.02"))
        self.assertEqual(t.gross, Decimal("0.12"))

    def test_discount(self):
        t = calc_tot([Line("x", 2, Decimal("10.00"), 15)], Decimal("0"))
        self.assertEqual(t.gross, Decimal("17.00"))

    def test_empty(self):
        self.assertEqual(calc_tot([], Decimal("0.19")).gross, Decimal("0.00"))


if __name__ == "__main__":
    unittest.main()
