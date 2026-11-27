# SPDX-License-Identifier: Apache-2.0
"""The AOSP / Android device pack: device, build and triage tools, three agents, the aosp sandbox preset.

Tools (``tools/*.json``, one per area, grouped by action):

- ``triage``: crash | anr | tombstone | selinux | build, deterministic pipelines from a log to
  source, owning module and suspect commits (``triage.py``), with a refusal guard.
- ``adb``: devices, logcat, getprop, dumpsys, ... read-only queries run without asking; push,
  install, uninstall, reboot, settings_put and flash change the device and ask every time,
  in auto mode too.
- ``aosp_build``: lunch, m, atest through the session's shell (envsetup.sh sourced, the lunch
  target kept), and tests_for (Soong-aware test selection, ``tests_for.py``).

The Python here is the handlers those manifests name; see ``docs/packs/aosp.md``.
"""

from __future__ import annotations

import re
import shlex
import time
from pathlib import Path
from typing import Any

from critique_bot.extensions.manifest import Call, Handler

DESCRIPTION = ("triage pipelines for crashes, ANRs, tombstones, SELinux denials and build breaks, an adb tool (device "
               "changes always ask), lunch/m/atest and test selection, and three agents (aosp-debugger, "
               "aosp-build-fixer, selinux-helper).")
SANDBOX_PRESET = "aosp"
#: Where a saved device log goes (inside the workspace, so log and triage can read it).
DEVICE_LOG_DIR = ".crit/cache/device"


def _result(tool: str, ok: bool, text: str) -> dict[str, Any]:
    first = text.split("\n", 1)[0]
    ui = {"summary": first[:140], "diff": None, "lines": None}
    return {"tool": tool, "ok": True, "output": text, "ui": ui} if ok else {"tool": tool, "ok": False, "error": text,
                                                                          "ui": ui}


# --------------------------------------------------------------------------- triage


def _read_permission(call: Call) -> Any:
    """A triage reads files: a path outside the workspace or a secret file asks, like read_files."""
    from critique_bot import agent_tools

    paths = [str(call.args.get(key)) for key in ("path", "traces") if isinstance(call.args.get(key), str)
             and str(call.args.get(key)).strip()]
    if not paths:
        return agent_tools.Permission("read", f"triage {call.action}", "", tool=call.tool.name)
    perm = agent_tools.permission_for("read_files", {"paths": paths}, call.ctx)
    perm.tool = call.tool.name
    perm.summary = f"triage {call.action}: {perm.summary}"
    return perm


def _triage(call: Call) -> dict[str, Any]:
    from critique_bot.packs.aosp import triage

    ok, text = triage.run(call.action, call.args, call.ctx)
    return _result(call.tool.name, ok, text)


# --------------------------------------------------------------------------- adb logcat


def _logcat(call: Call) -> dict[str, Any]:
    """Dump the device log to a file in the workspace and return its summary, not raw lines."""
    from critique_bot import log_tools
    from critique_bot.extensions import runner

    from critique_bot.extensions.manifest import _render_value

    action = call.tool.actions.get(call.action)
    templates = dict(call.tool.env)
    templates.update(dict(action.env) if action else {})
    env = runner.argv_env({key: value for key, template in templates.items()
                           if (value := _render_value(template, call.args)) is not None})
    workspace = Path(getattr(call.ctx, "workspace", ".") or ".")
    run = runner.run_argv(call.argv, cwd=workspace, env=env, timeout=(action.timeout_s if action and action.timeout_s
                                                                       else call.tool.timeout_s),
                          cancel=getattr(call.ctx, "cancel", None))
    if run.error:
        return _result(call.tool.name, False, run.error)
    if run.exit_code != 0:
        detail = (run.stderr or run.stdout).strip()[-1500:]
        hint = " (no device: check adb devices; crit reaches the adb server on port 5037)" if "device" in detail else ""
        return _result(call.tool.name, False, f"adb logcat failed (exit {run.exit_code}){hint}: {detail}")
    folder = workspace / DEVICE_LOG_DIR
    try:
        folder.mkdir(parents=True, exist_ok=True)
        saved = folder / time.strftime("logcat-%Y%m%d-%H%M%S.txt")
        saved.write_text(run.stdout, encoding="utf-8")
    except OSError as exc:
        return _result(call.tool.name, False, f"could not save the log: {exc}")
    rel = saved.relative_to(workspace).as_posix()
    lines = run.stdout.count("\n")
    summary = log_tools.render_summary(log_tools.summarize(saved), max_chars=3000, name=rel)
    tail = "\n".join(run.stdout.rstrip("\n").split("\n")[-15:])
    text = (f"saved {lines} lines to {rel}\n{summary}\nlast lines:\n{tail}\n"
            f"next: triage action crash/anr/selinux with path {rel}, or log action query on it")
    return _result(call.tool.name, True, text)


# --------------------------------------------------------------------------- aosp_build


def _names(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.replace(",", " ").split()
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def changed_files(ctx: Any) -> list[str]:
    """Files edited in this task, then uncommitted and untracked files in git (newest knowledge first)."""
    from critique_bot.indexing import history

    workspace = Path(getattr(ctx, "workspace", ".") or ".")
    out: list[str] = []
    state = getattr(ctx, "state", None)
    for name in list(getattr(state, "edits", {}) or {}):
        out.append(str(name))
    for args in (("diff", "--name-only", "HEAD"), ("ls-files", "--others", "--exclude-standard")):
        code, text = history.git(workspace, *args, timeout=20)
        if code == 0:
            out += [line.strip() for line in text.splitlines() if line.strip()]
    return list(dict.fromkeys(item for item in out if not item.startswith(".crit/")))


def _modules_for_changes(ctx: Any) -> tuple[list[str], list[str]]:
    from critique_bot import soong

    workspace = Path(getattr(ctx, "workspace", ".") or ".")
    names: list[str] = []
    files = changed_files(ctx)
    for rel in files[:30]:
        owner = soong.modules_for_file(workspace, rel)
        if owner is not None and owner.modules and owner.modules[0].name not in names:
            names.append(owner.modules[0].name)
    return names, files


def _command(call: Call) -> tuple[str, str]:
    """The shell command an aosp_build action runs, or ("", why not)."""
    if call.action == "lunch":
        target = str(call.args.get("target") or "").strip()
        return (f"lunch {shlex.quote(target)}", "") if target else ("", "")
    if call.action == "m":
        modules = _names(call.args.get("modules"))
        if not modules:
            modules, files = _modules_for_changes(call.ctx)
            if not modules:
                return "", ("pass modules (Soong module names). m alone builds the whole platform (hours); no changed "
                            "file maps to a module" + (f" ({', '.join(files[:3])})" if files else " (nothing changed)"))
        return "m " + " ".join(shlex.quote(item) for item in modules), ""
    if call.action == "atest":
        tests = _names(call.args.get("tests"))
        if not tests:
            from critique_bot.packs.aosp import tests_for

            files = _names(call.args.get("paths")) or changed_files(call.ctx)
            picked = tests_for.select(Path(call.ctx.workspace), files).tests if files else []
            tests = [pick.name for pick in picked[:3]]
            if not tests:
                return "", "pass tests: no TEST_MAPPING or test module covers the changed files (aosp_build tests_for)"
        flags = ["--host"] if call.args.get("host") is True else []
        return "atest " + " ".join(flags + [shlex.quote(item) for item in tests]), ""
    return "", f"no command for action {call.action}"


def _build_permission(call: Call) -> Any:
    from critique_bot import agent_tools

    command, _why = _command(call)
    if not command:
        return agent_tools.Permission("read", f"aosp_build {call.action}", "", tool=call.tool.name)
    return agent_tools.permission_for("run_command", {"command": command}, call.ctx)


def _build(call: Call) -> dict[str, Any]:
    """lunch, m and atest run in the session's shell (envsetup.sh sourced, the lunch target kept)."""
    from critique_bot import agent_tools, soong

    command, why = _command(call)
    if not command:
        if call.action == "lunch":
            choices = soong.lunch_choices(Path(call.ctx.workspace))
            listed = ", ".join(choices) if choices else "none found in device/*/*/AndroidProducts.mk"
            return _result(call.tool.name, True, f"pass target. Lunch targets in this tree (car first): {listed}")
        return _result(call.tool.name, False, why)
    arguments: dict[str, Any] = {"command": command}
    if isinstance(call.args.get("timeout"), int):
        arguments["timeout"] = call.args["timeout"]
    result = agent_tools.execute("run_command", arguments, call.ctx)
    result = dict(result)
    if call.action == "m" and not call.args.get("modules"):
        note = f"(modules from the changed files: {command[2:]})\n"
        if isinstance(result.get("output"), str):
            result["output"] = note + result["output"]
    if not result.get("ok") and call.action in {"m", "atest"}:
        hint = "next: triage action build with the build output saved to a file, or read the first error above"
        result["error"] = f"{result.get('error') or 'failed'}. {hint}"
    return result


def _tests_for(call: Call) -> dict[str, Any]:
    from critique_bot.packs.aosp import tests_for

    files = _names(call.args.get("paths"))
    if not files:
        files = changed_files(call.ctx)
    if not files:
        return _result(call.tool.name, False, "pass paths (the changed files); nothing is changed in git or this task")
    workspace = Path(call.ctx.workspace)
    clean = []
    for item in files:
        rel = re.sub(r"^\./", "", item.replace("\\", "/"))
        path = Path(rel)
        if path.is_absolute():
            try:
                rel = path.resolve().relative_to(workspace.resolve()).as_posix()
            except (OSError, ValueError):
                return _result(call.tool.name, False, f"{item} is outside the workspace")
        if ".." in Path(rel).parts:
            return _result(call.tool.name, False, f"{item} is outside the workspace")
        clean.append(rel)
    return _result(call.tool.name, True, tests_for.render(tests_for.select(workspace, clean)))


HANDLERS: dict[str, Handler] = {
    "triage": Handler(_triage, _read_permission),
    "adb_logcat": Handler(_logcat),
    "aosp_build": Handler(_build, _build_permission),
    "tests_for": Handler(_tests_for),
}
