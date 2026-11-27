#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run crit on the eval suite: the same as ``crit eval`` (see ``critique_bot/eval_harness.py``).

    python3 scripts/eval/run_eval.py --config ~/.crit/qwen.json --runs 3
    python3 scripts/eval/run_eval.py audit
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from critique_bot.eval_harness import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
