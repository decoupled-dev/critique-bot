# SPDX-License-Identifier: Apache-2.0
"""Flows: state machines on top of the turn loop (plan Stage 7).

A flow does not run its own agent. Each state is a sub-goal message to the one writer
and a permission profile; the harness runs the checks between states. ``verified`` is
the repro -> fix -> test -> review flow (:mod:`.verified`); :mod:`.checks` holds the
model-free parts it checks with.
"""

from __future__ import annotations

from critique_bot.engine.flows.verified import PASS, Evidence, VerifiedFlow

__all__ = ["PASS", "Evidence", "VerifiedFlow"]
