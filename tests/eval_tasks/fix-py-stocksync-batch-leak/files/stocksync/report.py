"""Rendering a job's report as plain text."""

from .config import JobConfig
from .inventory import StockItem
from .util import money


def render(items: list[StockItem], cfg: JobConfig) -> str:
    lines = [f"{cfg.title} [{cfg.name}]", ""]
    total = 0.0
    for item in items:
        value = item.value(cfg.currency)
        total += value
        lines.append(f"{item.sku:<8} {item.warehouse:<6} {item.qty:>6} {money(value, cfg.currency):>14}")
    warehouses = sorted({i.warehouse for i in items})
    lines.append("")
    lines.append(f"warehouses: {', '.join(warehouses) or '-'}")
    lines.append(f"total: {money(total, cfg.currency)} ({len(items)} lines)")
    return "\n".join(lines)
