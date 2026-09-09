#!/usr/bin/env python3
"""Launch LogCritique without installing the package.

    python3 logcritique.py /path/to/android-project -o logcritique.html
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from log_analyzer.analyze import main

if __name__ == "__main__":
    raise SystemExit(main())
