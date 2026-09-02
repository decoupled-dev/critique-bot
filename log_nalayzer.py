#!/usr/bin/env python3
"""Typo-friendly launcher. The real package name is log_analyzer."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from log_analyzer.analyze import main

if __name__ == "__main__":
    raise SystemExit(main())
