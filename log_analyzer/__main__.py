"""Allow `python -m log_analyzer` or `python log_analyzer/__main__.py`."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from log_analyzer.analyze import main

if __name__ == "__main__":
    raise SystemExit(main())
