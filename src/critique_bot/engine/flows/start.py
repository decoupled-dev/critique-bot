# SPDX-License-Identifier: Apache-2.0
"""Stage 7 for one task, as the session loop starts it: which flow, the verified flow, the
impact list and the plan triggers.

The flow setting comes from ``--flow`` (``_flow_flag``), then the agent file's ``flow:``,
then the ``flow`` setting, then the session's default (``_flow_default``: ``auto`` in
crit's sessions, plain for a bare ``run_agent_loop``).
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from critique_bot import log
from critique_bot.engine import dispatch
from critique_bot.tool_api import ToolContext


def flow_setting(options: dict[str, Any], role: str | None, workspace: Path) -> tuple[str, str, Any]:
    """(flow, where it came from, the agent definition) for a task: --flow, then the agent file's
    ``flow:``, then the ``flow`` setting, then the session's default (``auto`` in crit, plain otherwise)."""
    from critique_bot.engine.task_class import normalize_flow

    flag = normalize_flow(options.get("_flow_flag"))
    agent = None
    if role:
        try:
            from critique_bot.extensions import agent_files

            agent = agent_files.find_agent(str(role), workspace)
        except Exception as exc:  # noqa: BLE001 - a broken agent file must not stop the task
            log.debug(f"agent {role}: {exc}")
    if flag:
        return flag, "flag", agent
    from_agent = normalize_flow(getattr(agent, "flow", "")) if agent is not None else ""
    if from_agent:
        return from_agent, "agent", agent
    configured = normalize_flow(options.get("flow"))
    if configured:
        return configured, "settings", agent
    return normalize_flow(options.get("_flow_default")) or "plain", "default", agent


def _index_nav(ctx: ToolContext, index_path: Path | None) -> Callable[[dict[str, Any]], str] | None:
    """code_nav through the tool registry when the project has an index (Stage 8), else None."""
    if index_path is None or not Path(index_path).is_file():
        return None

    def nav(arguments: dict[str, Any]) -> str:
        result = dispatch._execute("code_nav", arguments, ctx)
        return str(result.get("output") or "") if result.get("ok") else ""

    return nav


def _word_refs(workspace: Path) -> Callable[[str], list[str]] | None:
    """Files that use a name, by a word search with rg (None when rg is missing: the estimate uses paths only)."""
    rg = shutil.which("rg")
    if rg is None:
        return None

    def refs(name: str) -> list[str]:
        try:
            # a fixed argv (no shell); the name is passed as a fixed string (-F)
            done = subprocess.run([rg, "-l", "-w", "--max-count", "1", "-F", "--", name, "."], cwd=workspace,
                                  capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return []
        return [line.strip().removeprefix("./") for line in done.stdout.splitlines() if line.strip()][:50]

    return refs


def start_flow(
    run: Any,
    task: str,
    options: dict[str, Any],
    *,
    role: str | None,
    workspace: Path,
    index_path: Path | None,
    chat: Any,
    seed: Callable[[], str],
    review_session: Callable[[], Any] | None,
    side_factory: Callable[[], Any] | None,
    mode: str,
) -> str:
    """Stage 7 for one task: classify it, start the verified flow and the impact list, fire the plan
    triggers. The text that goes with the first message ("" for a plain task)."""
    from critique_bot.engine import modes as modes_mod
    from critique_bot.engine import task_class
    from critique_bot.engine.flows import VerifiedFlow
    from critique_bot.engine.impact import ImpactTracker

    if run.read_only or run.answer is not None:
        return ""
    setting, source, agent = flow_setting(options, role, workspace)
    if setting == "plain" and options.get("impact_list") is not True and options.get("plan_triggers") is not True:
        return ""
    classifier = options.get("_task_classifier")
    if not callable(classifier) and setting == "auto" and side_factory is not None:
        classifier = task_class.model_classifier(side_factory)
    kind = task_class.classify(task, classifier if callable(classifier) else None)
    choice = task_class.flow_for(kind, setting, source=source)
    nav = _index_nav(run.ctx, index_path)
    text = ""
    if choice.verified:
        budgets = getattr(agent, "budget", None) if agent is not None else None
        run.flow = VerifiedFlow(
            run, choice, settings=options,
            reviewer=options.get("_reviewer") if callable(options.get("_reviewer")) else None,
            review_session=review_session, seed=seed,
            nav=(lambda rel: nav({"action": "imported_by", "path": rel})) if nav is not None else None,
            budgets={str(key).upper(): value for key, value in (budgets or {}).items()} if isinstance(budgets, dict) else None,
        )
        text = run.flow.start()
    if choice.verified or options.get("impact_list") is True:
        run.impact = ImpactTracker(
            workspace, nav=nav,
            on_item=lambda item: run.hooks.ui("note", f"Impact: {item.todo}"),
        )
        if run.flow is not None:
            run.flow.impact = run.impact
    triggers = options.get("plan_triggers")
    if (triggers is True or (triggers is not False and choice.verified)) and mode != "plan" \
            and kind.kind not in {"question", "ops"}:
        refs: Any = None
        if nav is not None:
            refs = lambda name: [line.split(":", 1)[0] for line in nav({"action": "refs", "name": name}).splitlines()[1:]  # noqa: E731
                                 if ":" in line and "." in line.split(":", 1)[0]]
        else:
            refs = _word_refs(workspace)
        reason = modes_mod.plan_trigger(task, files=modes_mod.estimate_files(task, refs=refs))
        if reason:
            run.payload = ""
            if run._suggest_plan(reason):
                text = (run.payload.strip() + "\n\n" + text).strip()
                run.payload = ""
    return text
