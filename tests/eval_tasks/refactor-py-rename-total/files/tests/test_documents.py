import unittest
from datetime import date
from decimal import Decimal

from billing.invoice import Invoice
from billing.quotes import Quote
from billing.reports import monthly_revenue, top_customers
from billing.totals import Line


class DocumentsTest(unittest.TestCase):
    def test_invoice(self):
        inv = Invoice("1", "ACME", date(2024, 5, 14)).add("Consulting", 3, "120.00").add("Travel", 1, "89.90", 10)
        self.assertEqual(inv.totals().gross, Decimal("524.68"))
        self.assertIn("total", inv.render())

    def test_quote_range(self):
        q = Quote("Bob", date(2024, 1, 1), lines=[Line("a", 1, Decimal("100"))],
                  optional=[Line("b", 1, Decimal("50"))])
        self.assertEqual(q.price_range(), (Decimal("119.00"), Decimal("178.50")))

    def test_reports(self):
        a = Invoice("1", "A", date(2024, 5, 1)).add("x", 1, "10.00")
        b = Invoice("2", "B", date(2024, 5, 20)).add("x", 2, "10.00")
        c = Invoice("3", "A", date(2024, 6, 2)).add("x", 1, "5.00")
        self.assertEqual(monthly_revenue([a, b, c]), {"2024-05": Decimal("35.70"), "2024-06": Decimal("5.95")})
        self.assertEqual(top_customers([a, b, c], 1), [("B", Decimal("20.00"))])


if __name__ == "__main__":
    unittest.main()
