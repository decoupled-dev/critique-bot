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
  so an argument cannot turn into an option of the program (``--output=/etc/passwd``).
- An optional argument with no value and no default drops its element.
- JSON manifests have the same fields; they work on every Python (TOML needs 3.11+).
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

__all__ = ["ManifestError", "ManifestTool", "lint_description", "load_manifest", "render_argv"]

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_WHOLE_RE = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)(?:\|(.*))?\}$", re.DOTALL)
_ANY_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*(?:\|[^}]*)?\}")
SANDBOXES = ("read-only", "workspace-write", "full")
MAX_TIMEOUT_S = 3600
DEFAULT_TIMEOUT_S = 30
DEFAULT_OUTPUT_CAP_TOKENS = 4000
CHARS_PER_TOKEN = 4


class ManifestError(ValueError):
    """The manifest file is not a valid tool."""


@dataclass(frozen=True)
class Example:
    args: dict[str, Any]
    expect_ok: bool = True
    expect_output: str = ""


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

    # ------------------------------------------------------------------ the registry's view

    def spec(self):
        from critique_bot.tool_api import ToolSpec

        return ToolSpec(self.name, self.description, self.parameters, read_only=self.read_only and not self.network)

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
        args = schema.with_defaults(self.parameters, arguments)
        problems = schema.validate(self.parameters, args)
        if not problems:
            problems = self._value_problems(args)
        return args, problems

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
        return problems

    def command_line(self, args: dict[str, Any]) -> list[str]:
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
        if self.read_only and not self.network and self.sandbox != "full":
            return Permission("read", label, "", shown, tool=self.name)
        risk = "it runs with full access (sandbox = \"full\")" if self.sandbox == "full" else ""
        return Permission("command", label, f"tool:{self.name}", shown, risk=risk, tool=self.name)

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
        env = runner.argv_env({key: _render_value(value, args) for key, value in self.env.items()
                               if _render_value(value, args) is not None})
        stdin = json.dumps(args).encode("utf-8") if self.stdin == "json" else None
        workspace = Path(getattr(ctx, "workspace", ".") or ".")
        result = runner.run_argv(argv, cwd=workspace, env=env, stdin=stdin, timeout=self.timeout_s,
                                 cancel=getattr(ctx, "cancel", None))
        return self._result(argv, result, ctx)

    def _result(self, argv: list[str], run: runner.ArgvResult, ctx: Any) -> dict[str, Any]:
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
            status = f"timed out after {self.timeout_s:g}s"
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
        if self.taint:
            untrusted.mark(result, f"tool:{self.name}")
        return result


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


def load_manifest(path: Path, *, scope: str = "user", builtin_names: tuple[str, ...] = ()) -> ManifestTool:
    """Read and check one manifest. Raises :class:`ManifestError` with what to fix."""
    path = Path(path)
    return from_data(_read_data(path), path=path, scope=scope, builtin_names=builtin_names)


def from_data(data: dict[str, Any], *, path: Path | None = None, scope: str = "user",
              builtin_names: tuple[str, ...] = ()) -> ManifestTool:
    where = path.name if path is not None else "manifest"
    known = {"name", "description", "read_only", "timeout_s", "output_cap_tokens", "sandbox", "network", "taint",
             "parameters", "run", "examples"}
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
    run = data.get("run")
    if not isinstance(run, dict):
        raise ManifestError(f"{where}: a [run] table with argv is required")
    unknown_run = sorted(set(run) - {"argv", "stdin", "env", "allow_dash_values"})
    if unknown_run:
        raise ManifestError(f"{where}: unknown [run] keys {', '.join(unknown_run)} (there is no shell; use argv)")
    argv = run.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        raise ManifestError(f"{where}: run.argv must be a non-empty list of strings")
    props = params.get("properties") or {}
    _check_templates(where, "run.argv", argv, props)
    if _WHOLE_RE.match(argv[0]):
        raise ManifestError(f"{where}: run.argv[0] (the program) cannot be an argument")
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
    if taint not in {"", "untrusted", "trusted"}:
        raise ManifestError(f'{where}: taint must be "untrusted" (or left out)')
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
        taint="" if taint == "trusted" else taint,
        stdin=stdin,
        env=dict(env),
        allow_dash_values=bool(run.get("allow_dash_values", False)),
        examples=tuple(examples),
    )


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
