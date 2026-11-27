"""Default settings every job starts from."""

DEFAULTS = {
    "batch_size": 500,
    "filters": {
        "warehouses": [],      # empty = every warehouse
        "min_qty": 0,
        "include_discontinued": False,
    },
    "output": {
        "currency": "EUR",
        "sort_by": "sku",
        "title": "Stock report",
    },
}
