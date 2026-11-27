"""Command line: print an invoice or a quick total from a JSON file."""

import argparse
import json
import sys
from datetime import date
from decimal import Decimal

from billing import compute_total
from billing.invoice import Invoice
from billing.totals import Line


def _lines(items):
    return [Line(i["description"], int(i["quantity"]), Decimal(i["unit_price"]), int(i.get("discount_pct", 0)))
            for i in items]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="billing")
    p.add_argument("cmd", choices=["invoice", "total"])
    p.add_argument("file")
    args = p.parse_args(argv)
    with open(args.file, encoding="utf-8") as fh:
        data = json.load(fh)
    if args.cmd == "total":
        t = compute_total(_lines(data["lines"]), Decimal(data.get("tax_rate", "0.19")))
        print(f"net {t.net} tax {t.tax} gross {t.gross}")
        return 0
    inv = Invoice(data["number"], data["customer"], date.fromisoformat(data["issued"]))
    inv.lines.extend(_lines(data["lines"]))
    print(inv.render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
