# SPDX-License-Identifier: Apache-2.0
"""Manifest tools: a tool defined by one TOML (or JSON) file, run as one argv.

```toml
name = "adb_logcat"
description = '''Read the device log, filtered. Use for crashes and ANRs on a device.
Not for build logs (use the log tool). Output: newest lines last, capped.'''
read_only = true
timeout_s = 30
output_cap_tokens = 4000
network = []              # hosts this tool may reach (recorded for the Stage 4 proxy)
taint = "untrusted"       # its output is wrapped as untrusted and flags the session

[parameters]              # JSON Schema subset (see schema.py)
type = "object"
required = ["filter"]
[parameters.properties.filter]
type = "string"
[parameters.properties.lines]
type = "integer"
minimum = 10
maximum = 5000

[run]
argv = ["adb", "logcat", "-d", "-t", "{lines|500}", "{filter}"]
# stdin = "json"          # the arguments as one JSON object on stdin
# env = { FILTER = "{filter}" }

[[examples]]
args = { filter = "*:E" }
expect_ok = true
```

Rules that keep it safe:

- No shell, ever. ``{name}`` and ``{name|default}`` fill a *whole* argv element (or a whole
  env value); a placeholder inside a longer element is a manifest error. So ``; rm -rf /``
  or ``$(...)`` in an argument is one literal argv element.
- A string value that starts with ``-`` is refused unless ``[run] allow_dash_values = true``,
  so an argument cannot turn into an option of the program (``--output=/etc/passwd``). A
  negative number is refused the same way, unless the parameter's schema has a negative
  ``minimum`` (``kill -TERM {pid}`` with ``pid = -1`` signals every process).
- An optional argument with no value and no default drops its element.
- JSON manifests have the same fields; they work on every Python (TOML needs 3.11+).

One tool, several actions (``[actions.<name>]``): a small model picks among few tools, so
related commands share one tool with an ``action`` argument. Each action has its own
``argv`` (or, for crit's own packs only, a Python ``handler``), and may set ``read_only``,
``required`` arguments, ``patterns`` (a regular expression each named string argument must
match in full; checked by crit, not sent to the model), ``timeout_s``, ``env``, ``taint``,
``risk`` (a reason: the call asks in every mode, auto included) and ``device = true`` (it
changes a connected device: always asks, even with ``confirm_risky`` off).
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from critique_bot.extensions import runner, schema, untrusted

__all__ = ["Action", "Call", "Handler", "ManifestError", "ManifestTool", "DEVICE_RISK", "lint_description",
           "load_manifest", "render_argv"]

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_WHOLE_RE = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)(?:\|(.*))?\}$", re.DOTALL)
_ANY_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*(?:\|[^}]*)?\}")
SANDBOXES = ("read-only", "workspace-write", "full")
MAX_TIMEOUT_S = 3600
DEFAULT_TIMEOUT_S = 30
DEFAULT_OUTPUT_CAP_TOKENS = 4000
CHARS_PER_TOKEN = 4
#: Scopes whose manifests may name a Python handler (crit's own content, never a user or project file).
HANDLER_SCOPES = frozenset({"builtin", "pack"})
#: Why an action that changes a device asks in every mode.
DEVICE_RISK = ("it changes the connected Android device (install, push, flash, reboot or a setting); crit asks "
               "every time, also in auto mode")
#: The permission key prefix of such calls; the engine never drops their question.
DEVICE_KEY_PREFIX = "device:"
_TAINTS = {"": "", "trusted": "", "untrusted": "untrusted", "device": "device"}


class ManifestError(ValueError):
    """The manifest file is not a valid tool."""


@dataclass(frozen=True)
class Example:
    args: dict[str, Any]
    expect_ok: bool = True
    expect_output: str = ""


@dataclass(frozen=True)
class Action:
    """One action of a tool with ``[actions]``: its own argv (or handler) and policy."""

    name: str
    argv: tuple[str, ...] = ()
    handler: str = ""
    #: None: the tool's read_only.
    read_only: bool | None = None
    risk: str = ""
    device: bool = False
    required: tuple[str, ...] = ()
    #: argument -> regular expression its string value must match in full
    patterns: tuple[tuple[str, str], ...] = ()
    timeout_s: float | None = None
    env: tuple[tuple[str, str], ...] = ()
    #: None: the tool's taint.
    taint: str | None = None


@dataclass
class Call:
    """What a handler gets: the tool, the action, the checked arguments, the argv (if any), the context."""

    tool: "ManifestTool"
    action: str
    args: dict[str, Any]
    argv: list[str]
    ctx: Any


@dataclass(frozen=True)
class Handler:
    """Python behind an action of a pack tool: ``run(call) -> result``; ``permission(call)`` when it
    needs something other than the action's declared policy (a command it composes, a path it reads)."""

    run: Any
    permission: Any = None


@dataclass
class ManifestTool:
    name: str
    description: str
    parameters: dict[str, Any]
    argv: list[str]
    path: Path | None = None
    scope: str = "user"
    read_only: bool = False
    timeout_s: float = DEFAULT_TIMEOUT_S
    output_cap_tokens: int = DEFAULT_OUTPUT_CAP_TOKENS
    sandbox: str = "workspace-write"
    network: tuple[str, ...] = ()
    taint: str = ""
    stdin: str = ""
    env: dict[str, str] = field(default_factory=dict)
    allow_dash_values: bool = False
    examples: tuple[Example, ...] = ()
    actions: dict[str, Action] = field(default_factory=dict)
    handlers: dict[str, Handler] = field(default_factory=dict)

    # ------------------------------------------------------------------ the registry's view

    def spec(self):
        from critique_bot.tool_api import ToolSpec

        read_only = self.read_only and not self.network
        if self.actions:
            read_only = read_only and all(self._action_read_only(item) for item in self.actions.values())
        return ToolSpec(self.name, self.description, self.parameters, read_only=read_only)

    def _action_read_only(self, action: Action) -> bool:
        return (self.read_only if action.read_only is None else action.read_only) and not action.device

    def action_of(self, args: dict[str, Any]) -> Action | None:
        if not self.actions:
            return None
        return self.actions.get(_action_key(args.get("action")))

    def check(self, arguments: Any) -> tuple[dict[str, Any], list[str]]:
        """The arguments with defaults filled in, and what is wrong with them."""
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                return {}, ["arguments must be a JSON object"]
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return {}, ["arguments must be a JSON object"]
        if self.actions and isinstance(arguments.get("action"), str):
            arguments = {**arguments, "action": _action_key(arguments["action"])}
        args = schema.with_defaults(self.parameters, arguments)
        problems = schema.validate(self.parameters, args)
        if not problems:
            problems = self._value_problems(args)
        if not problems and self.actions:
            problems = self._action_problems(args)
        return args, problems

    def _action_problems(self, args: dict[str, Any]) -> list[str]:
        action = self.action_of(args)
        if action is None:
            return [f"action must be one of: {', '.join(self.actions)}"]
        problems = [f"action {action.name} needs {name}" for name in action.required
                    if args.get(name) in (None, "", [])]
        for name, pattern in action.patterns:
            value = args.get(name)
            values = value if isinstance(value, list) else [value]
            for item in values:
                if item is None:
                    continue
                if not isinstance(item, str) or not re.fullmatch(pattern, item):
                    problems.append(f"{name} {str(item)[:60]!r} is not allowed here (it must match {pattern})")
        return problems

    def _value_problems(self, args: dict[str, Any]) -> list[str]:
        problems = []
        for name, value in args.items():
            text = _as_text(value)
            if "\x00" in text:
                problems.append(f"{name} contains a NUL character")
            if isinstance(value, str) and value.startswith("-") and not self.allow_dash_values:
                problems.append(
                    f"{name} starts with '-', which the program would read as an option; "
                    "this tool does not take option-like values"
                )
            elif self._negative_refused(name, value):
                problems.append(
                    f"{name} is negative ({text}): as an argument it starts with '-', which the program would read "
                    "as an option or a special value (kill -1 is every process); this tool does not take negative numbers"
                )
        return problems

    def _negative_refused(self, name: str, value: Any) -> bool:
        """A negative number becomes ``-1``: refused like a dash string (SF-11), unless the manifest
        allows dash values or the parameter's schema has a negative ``minimum``."""
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not value < 0 or self.allow_dash_values:
            return False
        prop = (self.parameters.get("properties") or {}).get(name) if isinstance(self.parameters, dict) else None
        minimum = prop.get("minimum") if isinstance(prop, dict) else None
        return not (isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and minimum < 0)

    def command_line(self, args: dict[str, Any]) -> list[str]:
        action = self.action_of(args)
        if action is not None:
            return render_argv(list(action.argv), args)
        return render_argv(self.argv, args)

    def permission(self, arguments: Any, ctx: Any) -> Any:
        """What this call needs: a read-only, offline tool only reads; anything else is a command."""
        from critique_bot.tool_api import Permission

        args, problems = self.check(arguments)
        argv = [] if problems else self.command_line(args)
        shown = shlex.join(argv) if argv else self.name
        label = f"Run tool {self.name}" + (f": {_short(shown, 120)}" if argv else "")
        if problems:
            return Permission("read", label, "", shown, tool=self.name)
        action = self.action_of(args)
        if action is not None:
            return self._action_permission(action, args, argv, ctx, label, shown)
        if self.read_only and not self.network and self.sandbox != "full":
            return Permission("read", label, "", shown, tool=self.name)
        risk = "it runs with full access (sandbox = \"full\")" if self.sandbox == "full" else ""
        return Permission("command", label, f"tool:{self.name}", shown, risk=risk, tool=self.name)

    def _action_permission(self, action: Action, args: dict[str, Any], argv: list[str], ctx: Any, label: str,
                           shown: str) -> Any:
        from critique_bot.tool_api import Permission

        label = f"{self.name} {action.name}" + (f": {_short(shown, 120)}" if argv else "")
        if action.device:
            return Permission("command", label, f"{DEVICE_KEY_PREFIX}{self.name}:{action.name}", shown,
                              risk=action.risk or DEVICE_RISK, tool=self.name)
        handler = self.handlers.get(action.handler) if action.handler else None
        if handler is not None and handler.permission is not None:
            perm = handler.permission(Call(self, action.name, args, argv, ctx))
            if perm is not None:
                return perm
        if action.risk:
            return Permission("command", label, f"tool:{self.name}:{action.name}", shown, risk=action.risk,
                              tool=self.name)
        if self._action_read_only(action) and not self.network and self.sandbox != "full":
            return Permission("read", label, "", shown, tool=self.name)
        risk = "it runs with full access (sandbox = \"full\")" if self.sandbox == "full" else ""
        return Permission("command", label, f"tool:{self.name}:{action.name}", shown, risk=risk, tool=self.name)

    def execute(self, arguments: Any, ctx: Any) -> dict[str, Any]:
        args, problems = self.check(arguments)
        if problems:
            return {
                "tool": self.name,
                "ok": False,
                "error": "bad arguments: " + "; ".join(problems[:6]),
                "parameters": self.parameters,
            }
        argv = self.command_line(args)
        action = self.action_of(args)
        if action is not None and action.handler:
            handler = self.handlers.get(action.handler)
            if handler is None:
                return {"tool": self.name, "ok": False, "error": f"action {action.name} has no handler here"}
            result = handler.run(Call(self, action.name, args, argv, ctx))
            result["tool"] = self.name
            return self._taint(result, action)
        return self.run_argv(argv, args, ctx, action=action)

    def run_argv(self, argv: list[str], args: dict[str, Any], ctx: Any, *, action: Action | None = None,
                 timeout: float | None = None) -> dict[str, Any]:
        """Run ``argv`` (no shell) the way this tool runs: its env, timeout, output cap, redaction and taint."""
        templates = dict(self.env)
        if action is not None:
            templates.update(dict(action.env))
        env = runner.argv_env({key: _render_value(value, args) for key, value in templates.items()
                               if _render_value(value, args) is not None})
        stdin = json.dumps(args).encode("utf-8") if self.stdin == "json" else None
        workspace = Path(getattr(ctx, "workspace", ".") or ".")
        seconds = timeout or (action.timeout_s if action is not None and action.timeout_s else self.timeout_s)
        result = runner.run_argv(argv, cwd=workspace, env=env, stdin=stdin, timeout=seconds,
                                 cancel=getattr(ctx, "cancel", None))
        return self._result(argv, result, ctx, action=action, timeout=seconds)

    def _taint(self, result: dict[str, Any], action: Action | None) -> dict[str, Any]:
        taint = self.taint if action is None or action.taint is None else action.taint
        if taint == "device":
            untrusted.mark(result, f"device:{self.name}")
        elif taint:
            untrusted.mark(result, f"tool:{self.name}")
        return result

    def _result(self, argv: list[str], run: runner.ArgvResult, ctx: Any, *, action: Action | None = None,
                timeout: float | None = None) -> dict[str, Any]:
        from critique_bot.redaction import redact_secrets

        if run.error:
            return {"tool": self.name, "ok": False, "error": run.error, "ui": _ui(f"{self.name}: {run.error}")}
        cap = self.output_cap_tokens * CHARS_PER_TOKEN
        ctx_cap = int(getattr(ctx, "max_chars", 0) or 0)
        if ctx_cap:
            cap = min(cap, ctx_cap)
        body = run.stdout
        if run.stderr.strip():
            body = (body.rstrip("\n") + "\n" if body.strip() else "") + "stderr:\n" + run.stderr
        body, _count = redact_secrets(body)
        body = _cap(body, cap)
        if run.timed_out:
            status = f"timed out after {timeout or self.timeout_s:g}s"
        elif run.cancelled:
            status = "interrupted"
        else:
            status = f"exit {run.exit_code}"
        output = f"{status} ({run.seconds:.1f}s)\n{body}".rstrip()
        ok = run.exit_code == 0
        result: dict[str, Any] = {"tool": self.name, "ok": ok, "output": output, "exit_code": run.exit_code,
                                  "ui": _ui(f"{self.name}: {status}", output)}
        if not ok:
            result["error"] = status
        return self._taint(result, action)


def _action_key(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _ui(summary: str, output: str = "") -> dict[str, Any]:
    rows = [line for line in output.split("\n")[1:] if line.strip()][-3:]
    return {"summary": summary, "diff": None, "lines": [_short(row, 160) for row in rows] or None}


def _cap(text: str, cap: int) -> str:
    if cap <= 0 or len(text) <= cap:
        return text
    head = cap // 4
    tail = cap - head
    return text[:head] + f"\n... {len(text) - cap} characters cut (output_cap_tokens) ...\n" + text[-tail:]


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _render_value(template: str, args: dict[str, Any]) -> str | None:
    """A whole-element template filled in; None when its argument is missing and has no default."""
    match = _WHOLE_RE.match(template)
    if not match:
        return template
    name, default = match.group(1), match.group(2)
    if name in args and args[name] is not None:
        return _as_text(args[name])
    return default


def render_argv(template: list[str], args: dict[str, Any]) -> list[str]:
    """The argv for ``args``: each ``{name}`` element becomes exactly one element (or none)."""
    out: list[str] = []
    for element in template:
        value = _render_value(element, args)
        if value is not None:
            out.append(value)
    return out


# --------------------------------------------------------------------------- loading

def _read_data(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ManifestError(f"{path.name}: not valid JSON: {exc}") from exc
    else:
        if sys.version_info < (3, 11):
            raise ManifestError(f"{path.name}: TOML manifests need Python 3.11 or newer; use a .json manifest")
        import tomllib

        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ManifestError(f"{path.name}: not valid TOML: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{path.name}: the manifest must be a table/object")
    return data


def load_manifest(path: Path, *, scope: str = "user", builtin_names: tuple[str, ...] = (),
                  handlers: dict[str, Handler] | None = None) -> ManifestTool:
    """Read and check one manifest. Raises :class:`ManifestError` with what to fix.

    ``handlers`` (name -> :class:`Handler`) are only for crit's own manifests (``scope`` builtin or pack).
    """
    path = Path(path)
    return from_data(_read_data(path), path=path, scope=scope, builtin_names=builtin_names, handlers=handlers)


def from_data(data: dict[str, Any], *, path: Path | None = None, scope: str = "user",
              builtin_names: tuple[str, ...] = (), handlers: dict[str, Handler] | None = None) -> ManifestTool:
    where = path.name if path is not None else "manifest"
    known = {"name", "description", "read_only", "timeout_s", "output_cap_tokens", "sandbox", "network", "taint",
             "parameters", "run", "examples", "actions"}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ManifestError(f"{where}: unknown keys {', '.join(unknown)}")
    name = data.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ManifestError(f"{where}: name must be letters, digits and _ (start with a letter, at most 64)")
    from critique_bot.tool_api import ALLOWED_TOOLS

    if name in builtin_names or name in ALLOWED_TOOLS or name.startswith("mcp__") or name == "tool_search":
        raise ManifestError(f"{where}: {name} is a built-in tool name; pick another")
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ManifestError(f"{where}: description is required")
    params = data.get("parameters", {"type": "object", "properties": {}})
    try:
        schema.check_schema(params)
    except schema.SchemaError as exc:
        raise ManifestError(f"{where}: {exc}") from exc
    if params.get("type", "object") != "object":
        raise ManifestError(f"{where}: parameters must have type = \"object\"")
    params = {"type": "object", **params}
    raw_actions = data.get("actions")
    run = data.get("run")
    if run is None and raw_actions is not None:
        run = {}
    if not isinstance(run, dict):
        raise ManifestError(f"{where}: a [run] table with argv is required")
    unknown_run = sorted(set(run) - {"argv", "stdin", "env", "allow_dash_values"})
    if unknown_run:
        raise ManifestError(f"{where}: unknown [run] keys {', '.join(unknown_run)} (there is no shell; use argv)")
    argv = run.get("argv")
    props = params.get("properties") or {}
    actions: dict[str, Action] = {}
    if raw_actions is not None:
        actions, params = _load_actions(where, raw_actions, params, scope, handlers or {})
        props = params.get("properties") or {}
        argv = [] if argv is None else argv
    if raw_actions is None or argv:
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
            raise ManifestError(f"{where}: run.argv must be a non-empty list of strings")
        _check_templates(where, "run.argv", argv, props)
        if _WHOLE_RE.match(argv[0]):
            raise ManifestError(f"{where}: run.argv[0] (the program) cannot be an argument")
    for action in actions.values():
        if action.argv:
            _check_templates(where, f"actions.{action.name}.argv", list(action.argv), props)
            if _WHOLE_RE.match(action.argv[0]):
                raise ManifestError(f"{where}: actions.{action.name}.argv[0] (the program) cannot be an argument")
        _check_templates(where, f"actions.{action.name}.env", [value for _key, value in action.env], props)
    env = run.get("env") or {}
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise ManifestError(f"{where}: run.env must map names to strings")
    _check_templates(where, "run.env", list(env.values()), props)
    stdin = run.get("stdin") or ""
    if stdin not in {"", "json"}:
        raise ManifestError(f'{where}: run.stdin can only be "json"')
    sandbox = data.get("sandbox", "workspace-write")
    if sandbox not in SANDBOXES:
        raise ManifestError(f"{where}: sandbox must be one of {', '.join(SANDBOXES)}")
    network = data.get("network", [])
    if not isinstance(network, list) or not all(isinstance(host, str) for host in network):
        raise ManifestError(f"{where}: network must be a list of host names")
    taint = data.get("taint", "")
    if taint not in _TAINTS:
        raise ManifestError(f'{where}: taint must be "untrusted" or "device" (or left out)')
    timeout = data.get("timeout_s", DEFAULT_TIMEOUT_S)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= MAX_TIMEOUT_S:
        raise ManifestError(f"{where}: timeout_s must be a number of seconds up to {MAX_TIMEOUT_S}")
    cap = data.get("output_cap_tokens", DEFAULT_OUTPUT_CAP_TOKENS)
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 100:
        raise ManifestError(f"{where}: output_cap_tokens must be an integer of at least 100")
    examples = []
    for index, item in enumerate(data.get("examples") or [], start=1):
        if not isinstance(item, dict) or not isinstance(item.get("args", {}), dict):
            raise ManifestError(f"{where}: examples[{index}] needs args = {{...}}")
        examples.append(Example(dict(item.get("args") or {}), bool(item.get("expect_ok", True)),
                                str(item.get("expect_output") or "")))
    return ManifestTool(
        name=name,
        description=description.strip(),
        parameters=params,
        argv=list(argv),
        path=path,
        scope=scope,
        read_only=bool(data.get("read_only", False)),
        timeout_s=float(timeout),
        output_cap_tokens=cap,
        sandbox=sandbox,
        network=tuple(network),
        taint=_TAINTS[taint],
        stdin=stdin,
        env=dict(env),
        allow_dash_values=bool(run.get("allow_dash_values", False)),
        examples=tuple(examples),
        actions=actions,
        handlers={name: (handlers or {})[name] for name in {a.handler for a in actions.values() if a.handler}},
    )


_ACTION_KEYS = {"argv", "handler", "read_only", "risk", "device", "required", "patterns", "timeout_s", "env", "taint",
                "description"}


def _load_actions(where: str, raw: Any, params: dict[str, Any], scope: str,
                  handlers: dict[str, Handler]) -> tuple[dict[str, Action], dict[str, Any]]:
    """The ``[actions]`` table checked, and ``parameters`` with an ``action`` enum that lists them."""
    if not isinstance(raw, dict) or not raw:
        raise ManifestError(f"{where}: actions must be a table of action names")
    props = dict(params.get("properties") or {})
    actions: dict[str, Action] = {}
    for name, item in raw.items():
        if not isinstance(name, str) or not _NAME_RE.match(name) or name != _action_key(name):
            raise ManifestError(f"{where}: action name {name!r} must be lower-case letters, digits and _")
        if not isinstance(item, dict):
            raise ManifestError(f"{where}: actions.{name} must be a table")
        unknown = sorted(set(item) - _ACTION_KEYS)
        if unknown:
            raise ManifestError(f"{where}: actions.{name}: unknown keys {', '.join(unknown)}")
        argv = item.get("argv") or []
        handler = str(item.get("handler") or "")
        if argv and (not isinstance(argv, list) or not all(isinstance(part, str) for part in argv)):
            raise ManifestError(f"{where}: actions.{name}.argv must be a list of strings")
        if handler:
            if scope not in HANDLER_SCOPES:
                raise ManifestError(f"{where}: actions.{name}: only crit's own packs may name a handler")
            if handler not in handlers:
                raise ManifestError(f"{where}: actions.{name}: unknown handler {handler!r}")
        if not argv and not handler:
            raise ManifestError(f"{where}: actions.{name} needs argv")
        required = item.get("required") or []
        if not isinstance(required, list) or not all(isinstance(r, str) and r in props for r in required):
            raise ManifestError(f"{where}: actions.{name}.required must list parameters")
        patterns = item.get("patterns") or {}
        if not isinstance(patterns, dict) or not all(isinstance(k, str) and k in props and isinstance(v, str)
                                                     for k, v in patterns.items()):
            raise ManifestError(f"{where}: actions.{name}.patterns must map parameters to regular expressions")
        for key, pattern in patterns.items():
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ManifestError(f"{where}: actions.{name}.patterns.{key}: {exc}") from exc
        env = item.get("env") or {}
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ManifestError(f"{where}: actions.{name}.env must map names to strings")
        timeout = item.get("timeout_s")
        if timeout is not None and (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                                    or not 0 < timeout <= MAX_TIMEOUT_S):
            raise ManifestError(f"{where}: actions.{name}.timeout_s must be seconds up to {MAX_TIMEOUT_S}")
        taint = item.get("taint")
        if taint is not None and taint not in _TAINTS:
            raise ManifestError(f'{where}: actions.{name}.taint must be "untrusted", "device" or "trusted"')
        risk = item.get("risk") or ""
        if not isinstance(risk, str):
            raise ManifestError(f"{where}: actions.{name}.risk must be a sentence")
        read_only = item.get("read_only")
        actions[name] = Action(
            name=name, argv=tuple(argv), handler=handler,
            read_only=None if read_only is None else bool(read_only), risk=risk, device=bool(item.get("device")),
            required=tuple(required), patterns=tuple(patterns.items()),
            timeout_s=None if timeout is None else float(timeout), env=tuple(env.items()),
            taint=None if taint is None else _TAINTS[taint],
        )
    declared = props.get("action")
    if declared is None:
        props["action"] = {"type": "string", "enum": list(actions)}
    elif not isinstance(declared, dict) or sorted(declared.get("enum") or []) != sorted(actions):
        raise ManifestError(f"{where}: parameters.action must be a string enum of exactly the actions")
    required = list(params.get("required") or [])
    if "action" not in required:
        required.insert(0, "action")
    return actions, {**params, "properties": props, "required": required}


def _check_templates(where: str, field_name: str, values: list[str], props: dict[str, Any]) -> None:
    for value in values:
        whole = _WHOLE_RE.match(value)
        if whole:
            if whole.group(1) not in props:
                raise ManifestError(f"{where}: {field_name} uses {{{whole.group(1)}}}, which is not in parameters")
            continue
        if _ANY_RE.search(value):
            raise ManifestError(
                f"{where}: {field_name} element {value!r} mixes text and a placeholder; a placeholder must be "
                "the whole element (write \"--lines\", \"{lines}\" as two elements)"
            )


# --------------------------------------------------------------------------- description lint

_NOT_FOR_RE = re.compile(r"\b(not for|do not use|don't use|never use)\b", re.IGNORECASE)
_OUTPUT_RE = re.compile(r"\b(output|returns?|result|prints?)\b", re.IGNORECASE)


def lint_description(description: str) -> list[str]:
    """What a good tool description is missing: a purpose, when not to use it, and its output."""
    text = description.strip()
    problems = []
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0] if text else ""
    if len(first) < 15:
        problems.append("purpose: start with one sentence that says what the tool does")
    if not _NOT_FOR_RE.search(text):
        problems.append("not-for: say when not to use it (\"Not for build logs (use the log tool).\")")
    if not _OUTPUT_RE.search(text):
        problems.append("output: say what comes back (\"Output: newest lines last, capped.\")")
    if len(text) > 1200:
        problems.append("length: keep it under 1200 characters; small models skim")
    return problems
