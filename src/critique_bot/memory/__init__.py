# SPDX-License-Identifier: Apache-2.0
"""Project memory (plan Stage 8b): crit's picture of a project, built from code and sessions.

====  ======================================================  =====================
M1    file and module cards, keyed by content hash             :mod:`.cards`
M2    change sets / recipes and hot spots from git history     :mod:`.patterns`
M3    episodes from finished tasks (positive and negative)     :mod:`.episodes`
M4    conventions: proposed by the agent, approved by a person :mod:`.conventions`
      the learned project glossary                             :mod:`.glossary`
====  ======================================================  =====================

Everything lives in ``.crit/memory/memory.db`` (:mod:`.store`), every row with its provenance.
Recall turns a task into candidates (:mod:`.recall`); the engine ranks them into a short
"hunch" block for the first message of the task (``critique_bot.memory_recall``). The
``memory`` tool (:mod:`.tool`) searches and shows items and queues proposals; ``locate``
gets two retrievers (:mod:`.retrievers`). :mod:`.primer` saves and restores a llama.cpp slot's
KV state for the session primer.

This package is in the tools layer; the engine reaches it through the names below.
"""

from __future__ import annotations

from critique_bot.memory.store import MemoryStore, db_path, memory_dir, parse_id
from critique_bot.memory.tool import mode

__all__ = ["MemoryStore", "db_path", "memory_dir", "parse_id", "mode"]
