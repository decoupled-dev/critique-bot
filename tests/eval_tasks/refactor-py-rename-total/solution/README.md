# billing

Invoice and quote helpers.

The money math lives in `billing/totals.py`: `compute_total(lines, tax_rate)` returns a
`Totals` with net, tax and gross amounts. Everything else (invoices, quotes,
the monthly report and the CLI) calls `compute_total` so rounding is the same
everywhere.

    python3 -m billing.cli invoice examples.json
    python3 -m unittest discover -s tests
