# stocksync

Builds stock-level reports from the warehouse export (`stocksync/data/stock.csv`).

    python3 -m stocksync.cli job <name>     # one job
    python3 -m stocksync.cli nightly        # every job in NIGHTLY_JOBS
    python3 -m unittest discover -s tests

Jobs are defined in `stocksync/jobs.py`. Each job gives overrides on top of
`stocksync/defaults.py`; `stocksync/config.py` merges them.
