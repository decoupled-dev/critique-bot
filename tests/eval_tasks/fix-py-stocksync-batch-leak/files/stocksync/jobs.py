"""Job definitions and runners."""

from .config import load_config
from .filters import select, sort_items
from .inventory import load_items
from .report import render

JOBS = {
    "north-low-stock": {
        "filters": {"warehouses": ["OSL", "HEL"], "min_qty": 1},
        "output": {"sort_by": "qty", "title": "Low stock (north)"},
    },
    "uk-value": {
        "filters": {"warehouses": ["LON"], "min_qty": 10},
        "output": {"currency": "GBP", "sort_by": "value", "title": "UK stock value"},
    },
    "all-sites": {
        "output": {"title": "All sites"},
    },
    "clearance": {
        "filters": {"include_discontinued": True},
        "output": {"currency": "USD"},
    },
}

NIGHTLY_JOBS = ["north-low-stock", "uk-value", "all-sites", "clearance"]


def run_job(name: str, items=None) -> str:
    if name not in JOBS:
        raise KeyError(f"no job named {name!r}")
    cfg = load_config(name, JOBS[name])
    items = load_items() if items is None else items
    return render(sort_items(select(items, cfg), cfg), cfg)


def run_batch(names=None) -> dict[str, str]:
    names = NIGHTLY_JOBS if names is None else names
    items = load_items()
    return {name: run_job(name, items) for name in names}
