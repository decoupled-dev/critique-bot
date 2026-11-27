"""Reading the warehouse export."""

import csv
from dataclasses import dataclass
from pathlib import Path

DATA = Path(__file__).parent / "data" / "stock.csv"

# Fixed conversion table used by finance for the reports (EUR base).
RATES = {"EUR": 1.0, "USD": 1.08, "GBP": 0.86}


@dataclass(frozen=True)
class StockItem:
    sku: str
    name: str
    warehouse: str
    qty: int
    unit_price_eur: float
    discontinued: bool

    def value(self, currency: str) -> float:
        return self.qty * self.unit_price_eur * RATES[currency]


def load_items(path: Path = DATA) -> list[StockItem]:
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return [
        StockItem(
            sku=r["sku"],
            name=r["name"],
            warehouse=r["warehouse"],
            qty=int(r["qty"]),
            unit_price_eur=float(r["unit_price_eur"]),
            discontinued=r["discontinued"] == "1",
        )
        for r in rows
    ]
