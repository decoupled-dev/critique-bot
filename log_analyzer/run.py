#!/usr/bin/env python3
"""Run LogCritique without installing the package.

    python log_analyzer/run.py /path/to/android -o logcritique.html
"""

from __future__ import annotations

import sys
from pathlib import Path

_PARENT = str(Path(__file__).resolve().parent.parent)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from log_analyzer.analyze import main

if __name__ == "__main__":
    raise SystemExit(main())
