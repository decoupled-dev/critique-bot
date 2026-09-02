"""Allow `python -m log_analyzer` from the repository root."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "log_analyzer"

from .analyze import main

if __name__ == "__main__":
    raise SystemExit(main())
