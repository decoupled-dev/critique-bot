import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal

import billing
import billing.totals
from billing.cli import main
from billing.invoice import Invoice
from billing.quotes import Quote
from billing.reports import monthly_revenue
from billing.totals import Line, compute_total

OLD = "calc" + "_tot"


class HiddenRenameTest(unittest.TestCase):
    def test_no_alias(self):
        self.assertFalse(hasattr(billing.totals, OLD))
        self.assertFalse(hasattr(billing, OLD))
        self.assertIs(billing.compute_total, compute_total)
        self.assertIn("compute_total", billing.__all__)
        self.assertNotIn(OLD, billing.__all__)

    def test_behaviour_same(self):
        lines = [Line("a", 3, Decimal("120.00")), Line("b", 1, Decimal("89.90"), 10)]
        t = compute_total(lines, Decimal("0.19"))
        self.assertEqual((t.net, t.tax, t.gross), (Decimal("440.91"), Decimal("83.77"), Decimal("524.68")))
        inv = Invoice("9", "Z", date(2024, 2, 2)).add("a", 1, "0.05").add("b", 1, "0.05")
        self.assertEqual(inv.totals().gross, Decimal("0.12"))
        q = Quote("Q", date(2024, 1, 1), lines=[Line("a", 1, Decimal("100"))], optional=[Line("b", 1, Decimal("50"))])
        self.assertEqual(q.price_range(), (Decimal("119.00"), Decimal("178.50")))
        self.assertEqual(monthly_revenue([inv]), {"2024-02": Decimal("0.12")})

    def test_cli(self):
        data = {"number": "1", "customer": "C", "issued": "2024-05-14", "tax_rate": "0.19",
                "lines": [{"description": "x", "quantity": 2, "unit_price": "10.00"}]}
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(main(["total", p]), 0)
            self.assertEqual(buf.getvalue().strip(), "net 20.00 tax 3.80 gross 23.80")


if __name__ == "__main__":
    unittest.main()
