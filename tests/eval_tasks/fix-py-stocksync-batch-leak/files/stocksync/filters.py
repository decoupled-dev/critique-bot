"""Selecting the items a job reports on."""

from .config import JobConfig
from .inventory import StockItem


def select(items: list[StockItem], cfg: JobConfig) -> list[StockItem]:
    out = []
    for item in items:
        if cfg.warehouses and item.warehouse not in cfg.warehouses:
            continue
        if item.qty < cfg.min_qty:
            continue
        if item.discontinued and not cfg.include_discontinued:
            continue
        out.append(item)
    return out


def sort_items(items: list[StockItem], cfg: JobConfig) -> list[StockItem]:
    if cfg.sort_by == "qty":
        return sorted(items, key=lambda i: (-i.qty, i.sku))
    if cfg.sort_by == "value":
        return sorted(items, key=lambda i: (-i.value(cfg.currency), i.sku))
    return sorted(items, key=lambda i: (i.sku, i.warehouse))
