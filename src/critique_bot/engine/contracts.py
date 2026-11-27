"""Answer contracts and read-only tasks: what an app (the review) gets from the engine.

A read-only task reads the project and git history only; its final answer must pass an
:class:`AnswerContract`, so an app can parse it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class AnswerContract:
    """What the final answer of a task must look like, for callers that parse it (a review).

    A reply without tool calls ends the task when ``accepts(reply)`` is true. Otherwise
    ``nudge`` is sent back, up to ``max_nudges`` times, and then the task fails with
    ``failure``. ``last_round`` (when set) goes with the last allowed tool round.
    """

    accepts: Callable[[str], bool]
    nudge: str
    last_round: str = ""
    max_nudges: int = 2
    failure: str = "the final answer never came in the required format"
    note: str = "Asking for the answer in the required format."
