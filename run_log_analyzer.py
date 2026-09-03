#!/usr/bin/env python3
"""Run the Android log analyzer without pip install -e.

From the repository root, after a venv:

    python3 -m pip install -r log_analyzer/requirements.txt
    python3 run_log_analyzer.py /path/to/android-project -o log-report.html

Do not run pip install -e . from this repo root — that installs critique-bot,
not the analyzer, and will spend a long time building unrelated dependencies.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from log_analyzer.analyze import main

if __name__ == "__main__":
    raise SystemExit(main())
