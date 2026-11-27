"""``crit doctor model``: check a local model server before crit relies on it.

1. The server answers and lists the model.
2. Which server it is (llama.cpp, vLLM, SGLang, Ollama, LM Studio, TGI, OpenRouter, or a
   generic OpenAI-compatible one), its version, context window and, for llama.cpp, the KV
   cache type where it is reported (q4_0 gets a warning).
3. The model profile crit picked, and the server preset's launch line for agent use.
4. A conformance probe: twelve small native tool-call turns (a single call, two parallel
   calls, code with quotes, backslashes and newlines, a call after thinking, a no-call
   answer, a 4 KB argument, and more), each scored native-ok, recovered by crit's fallback
   parser, or failed. From the score it recommends ``native`` or ``prompted`` tool calls and
   caches that in ``~/.crit/cache/doctor.json``, where ``tool_mode: auto`` finds it.

``--write-profile`` saves the findings as a starting user profile in ``~/.crit/profiles``.
Everything goes through :mod:`critique_bot.model_api`; nothing here speaks HTTP.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Callable

from critique_bot import log, model_api
from critique_bot.config import BotConfig

NATIVE_OK, RECOVERED, FAILED = "native-ok", "recovered", "failed"

SYSTEM = (
    "You are a coding assistant working in a software project. You have tools. When the user asks "
    "for an action, call the right tool with exact arguments. When the user asks a question that "
    "needs no tool, answer in plain text without calling any tool."
)


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required}}}


TOOLS = [
    _tool("read_file", "Read a text file of the project.", {"path": {"type": "string"}}, ["path"]),
    _tool("write_file", "Create or overwrite a file with exactly the given content.",
          {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _tool("search", "Search the project's files for a pattern.",
          {"pattern": {"type": "string"}, "mode": {"type": "string", "enum": ["literal", "regex"]}}, ["pattern"]),
    _tool("run_command", "Run a shell command in the project.",
          {"command": {"type": "string"}, "timeout_s": {"type": "integer", "minimum": 1},
           "background": {"type": "boolean"}}, ["command"]),
    _tool("edit_file", "Replace exact text in a file; each edit replaces one unique occurrence.",
          {"path": {"type": "string"}, "edits": {"type": "array", "items": {
              "type": "object", "properties": {"old": {"type": "string"}, "new": {"type": "string"}},
              "required": ["old", "new"]}}}, ["path", "edits"]),
]

CODE = 'print("say \\"hi\\"")\npath = "C:\\\\temp\\\\new"\npattern = r"\\d+\\s*"\n'
LONG = "".join(f"line {i:03d}: the quick brown fox jumps over the lazy dog, then naps.\n" for i in range(1, 65))  # about 4.2 KB
UNICODE = "Grüße — naïve café, 日本語, ✓ done"


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    messages: list[dict[str, Any]]
    check: Callable[[list[dict[str, Any]], str], bool]
    #: True: the right answer is no call at all.
    no_call: bool = False


def _user(text: str) -> list[dict[str, Any]]:
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}]


def _calls(name: str, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c["arguments"] for c in calls if c.get("name") == name and isinstance(c.get("arguments"), dict)]


def _same_text(got: Any, want: str) -> bool:
    return isinstance(got, str) and got.strip("\n") == want.strip("\n")


def _one_read(path: str) -> Callable[[list[dict[str, Any]], str], bool]:
    def check(calls: list[dict[str, Any]], _text: str) -> bool:
        found = _calls("read_file", calls)
        return len(found) >= 1 and any(str(a.get("path", "")).endswith(path) for a in found)
    return check


def _parallel(calls: list[dict[str, Any]], _text: str) -> bool:
    paths = {str(a.get("path", "")) for a in _calls("read_file", calls)}
    return any(p.endswith("src/a.py") for p in paths) and any(p.endswith("src/b.py") for p in paths)


def _write(path: str, content: str) -> Callable[[list[dict[str, Any]], str], bool]:
    def check(calls: list[dict[str, Any]], _text: str) -> bool:
        return any(str(a.get("path", "")).endswith(path) and _same_text(a.get("content"), content)
                   for a in _calls("write_file", calls))
    return check


def _no_call(calls: list[dict[str, Any]], text: str) -> bool:
    return not calls and "42" in text


def _regex_search(calls: list[dict[str, Any]], _text: str) -> bool:
    return any(a.get("mode") == "regex" and "TODO" in str(a.get("pattern", "")) for a in _calls("search", calls))


def _typed(calls: list[dict[str, Any]], _text: str) -> bool:
    for a in _calls("run_command", calls):
        timeout = a.get("timeout_s")
        background = a.get("background", False)
        if ("pytest" in str(a.get("command", "")) and isinstance(timeout, int) and not isinstance(timeout, bool)
                and timeout == 120 and background is False):
            return True
    return False


def _nested(calls: list[dict[str, Any]], _text: str) -> bool:
    for a in _calls("edit_file", calls):
        edits = a.get("edits")
        if not isinstance(edits, list) or len(edits) != 2 or not all(isinstance(e, dict) for e in edits):
            continue
        pairs = {(str(e.get("old", "")).strip(), str(e.get("new", "")).strip()) for e in edits}
        if pairs == {("debug = True", "debug = False"), ("port = 80", "port = 8080")}:
            return True
    return False


def _choose_search(calls: list[dict[str, Any]], _text: str) -> bool:
    return bool(_calls("search", calls)) and "FIXME" in json.dumps(_calls("search", calls)) and not _calls("read_file", calls)


_FOLLOWUP = [
    {"role": "system", "content": SYSTEM},
    {"role": "user", "content": "Find where the config is loaded, then open that file."},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {
        "name": "search", "arguments": json.dumps({"pattern": "def load_config", "mode": "literal"})}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": "src/settings/loader.py:12: def load_config(path):"},
]

SCENARIOS: tuple[Scenario, ...] = (
    Scenario("single_call", "a single call", _user("Read the file src/app.py."), _one_read("src/app.py")),
    Scenario("parallel_calls", "two parallel calls",
             _user("Read src/a.py and src/b.py. Call read_file twice in this one reply, once per file."), _parallel),
    Scenario("code_quotes", "code with quotes, backslashes, newlines",
             _user(f"Create the file quote.py with exactly this content (the text between the markers, "
                   f"nothing else):\n<<<\n{CODE}>>>"), _write("quote.py", CODE)),
    Scenario("after_thinking", "a call after thinking",
             _user("Think step by step about which of these files most likely defines the HTTP routes: "
                   "README.md, src/routes.py, setup.cfg. Then read only that file."), _one_read("src/routes.py")),
    Scenario("no_call", "a no-call answer",
             _user("Without using any tool: what is 17 + 25? Reply with just the number."), _no_call, no_call=True),
    Scenario("long_argument", "a 4 KB argument",
             _user(f"Create the file fox.txt with exactly this content (between the markers):\n<<<\n{LONG}>>>"),
             _write("fox.txt", LONG)),
    Scenario("followup", "a call that uses a tool result", _FOLLOWUP, _one_read("src/settings/loader.py")),
    Scenario("enum_argument", "an enum argument",
             _user("Search the project for the regular expression TODO\\(\\w+\\) using regex mode."), _regex_search),
    Scenario("typed_arguments", "integer and boolean arguments",
             _user("Run the command `pytest -q` with a timeout of 120 seconds, not in the background."), _typed),
    Scenario("unicode_argument", "a non-ASCII argument",
             _user(f"Create the file notes.txt containing exactly this one line: {UNICODE}"), _write("notes.txt", UNICODE)),
    Scenario("nested_arguments", "an array of objects",
             _user("In src/app.py replace `debug = True` with `debug = False` and `port = 80` with "
                   "`port = 8080`, using one edit_file call with two edits."), _nested),
    Scenario("choose_tool", "picking the right tool",
             _user("Find every place the string FIXME appears in the project."), _choose_search),
)


def score_turn(scenario: Scenario, reply: dict[str, Any]) -> tuple[str, str]:
    """(native-ok | recovered | failed, a short reason) for one probe reply."""
    if reply.get("error"):
        return FAILED, str(reply["error"])[:160]
    native = list(reply.get("native") or [])
    recovered = list(reply.get("recovered") or [])
    text = str(reply.get("text") or "")
    if scenario.no_call:
        if native or recovered:
            return FAILED, "called a tool for a question that needs none"
        return (NATIVE_OK, "") if scenario.check([], text) else (FAILED, f"answer was {text.strip()[:60]!r}")
    if native:
        if not all(c.get("arguments_ok") for c in native):
            return FAILED, "the server returned arguments that are not a JSON object"
        if scenario.check(native, text):
            return NATIVE_OK, ""
        return FAILED, "wrong call: " + _short_calls(native)
    if recovered:
        if scenario.check(recovered, text):
            return RECOVERED, f"from the {recovered[0].get('source')} format"
        return FAILED, "recovered a wrong call: " + _short_calls(recovered)
    return FAILED, "no tool call" + (f"; text: {text.strip()[:60]!r}" if text.strip() else "")


def _short_calls(calls: list[dict[str, Any]]) -> str:
    return "; ".join(f"{c.get('name')}({json.dumps(c.get('arguments'), ensure_ascii=False)[:80]})" for c in calls)


def recommend(counts: dict[str, int]) -> str:
    """``native`` when at most one turn failed and most calls came back structured (the
    fallback parser covers the rest); else ``prompted``."""
    total = sum(counts.values()) or 1
    if counts.get(FAILED, 0) <= 1 and counts.get(NATIVE_OK, 0) >= total // 2:
        return "native"
    return "prompted"


def run_probe(config: BotConfig, model: str, *, say: Callable[[str], None] = print) -> dict[str, Any]:
    turns: dict[str, dict[str, str]] = {}
    counts = {NATIVE_OK: 0, RECOVERED: 0, FAILED: 0}
    for number, scenario in enumerate(SCENARIOS, 1):
        reply = model_api.probe_tool_turn(config, scenario.messages, TOOLS, model=model)
        status, reason = score_turn(scenario, reply)
        counts[status] += 1
        turns[scenario.name] = {"status": status, "reason": reason}
        say(f"  {number:2d}. {scenario.title:<42} {status}" + (f"  ({reason})" if reason else ""))
    return {"counts": counts, "turns": turns, "recommend": recommend(counts)}


# --------------------------------------------------------------------------- the report


def _served(model: str, models: list[str]) -> bool:
    wanted = model.lower()
    return any(name.lower() in (wanted, wanted + ":latest") for name in models)


def _slug(model: str) -> str:
    tail = re.split(r"[/\\]", model.strip())[-1]
    return re.sub(r"[^a-z0-9.]+", "-", tail.lower()).strip("-") or "model"


def write_profile(model: str, profile: Any, result: dict[str, Any], *, force: bool = False) -> tuple[bool, str]:
    """Save a starter user profile for ``model``. (written, path or reason)."""
    folder = model_api.profiles_dir()
    slug = _slug(model)
    path = folder / f"{slug}.json"
    if path.exists() and not force:
        return False, f"{path} exists; pass --force to replace it"
    counts = result.get("counts") or {}
    data: dict[str, Any] = {"id": slug}
    if profile.id != slug:  # same id: the file overrides that built-in's fields, its globs stay
        data["match"] = sorted({model.lower(), slug})
        if profile.id != "generic":
            data["extends"] = profile.id
    data["tool_mode"] = result.get("recommend") or "auto"
    data["notes"] = (
        f"Written by crit doctor model on {date.today().isoformat()}: {counts.get(NATIVE_OK, 0)} native, "
        f"{counts.get(RECOVERED, 0)} recovered, {counts.get(FAILED, 0)} failed of {len(SCENARIOS)} probes. "
        "Add the model card's sampling (temperature, top_p, top_k) and cite it here."
    )
    from critique_bot.output import private_dir

    private_dir(folder)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True, str(path)


def _preset_for(server: str) -> Any:
    presets = {p.id: p for p in model_api.server_presets()}
    return presets.get(server)


def run_model(
    config: BotConfig,
    *,
    model: str = "",
    probe: bool = True,
    write: bool = False,
    force: bool = False,
    as_json: bool = False,
    say: Callable[[str], None] = print,
) -> int:
    """The whole ``crit doctor model`` check. 0 when the server works (and the probe, if run,
    recommends something usable), 1 otherwise."""
    report: dict[str, Any] = {"url": config.url}
    out = (lambda _line: None) if as_json else say
    if not config.is_local:
        say("crit doctor model checks a model server (backend local); this config uses the web chat.")
        return 1
    reach = model_api.probe_server(config.url, config.local.api_key)
    report["reachable"] = reach.reachable
    out(f"server     {config.url}: {reach.line}")
    if not reach.reachable:
        if as_json:
            say(json.dumps(report, indent=2))
        return 1
    model = model or config.model or (reach.models[0] if reach.models else "")
    report["model"] = model
    listed = _served(model, reach.models) if reach.models else None
    report["model_listed"] = listed
    if listed is False:
        out(f"model      {model}: NOT in the server's list ({', '.join(reach.models[:6])})")
    else:
        out(f"model      {model}" + (" (listed)" if listed else ""))
    config = replace(config, model=model)
    info = model_api.server_info(config)
    report["server"] = info
    version = f" {info['version']}" if info.get("version") else ""
    out(f"type       {info['type']}{version}")
    out(f"context    {info['context'] or 'not reported'}" + (" tokens" if info.get("context") else ""))
    warnings: list[str] = []
    if info.get("kv_cache"):
        out(f"kv cache   {info['kv_cache']}")
        if "q4" in str(info["kv_cache"]).lower():
            warnings.append("the KV cache is q4_0: tool-call arguments and long code degrade; use q8_0 or f16")
    if info.get("template_has_tools") is False:
        warnings.append("the loaded chat template has no tools section: start llama-server with --jinja "
                        "(and --chat-template-file with a tool-aware template)")
    if info.get("tool_call_parser"):
        out(f"parser     {info['tool_call_parser']}")
    if info.get("speculation"):
        out(f"speculate  {info['speculation']}")
    profile = model_api.model_profile(config)
    report["profile"] = profile.id
    sampling = ", ".join(f"{k}={v}" for k, v in profile.sampling)
    out(f"profile    {profile.id} ({profile.origin}); tool_mode {profile.tool_mode}"
        + (f"; sampling {sampling}" if sampling else ""))
    preset = _preset_for(info["type"])
    limit = profile.effective_context or None
    if preset is not None:
        report["launch"] = preset.launch_line(model, profile, info.get("context") or limit)
        out(f"launch     {report['launch']}")
    else:
        out("launch     (unknown server type: see docs/local-models.md for each server's launch line)")
    for line in warnings:
        out(f"WARNING    {line}")
    report["warnings"] = warnings
    code = 0
    if probe:
        out(f"probe      {len(SCENARIOS)} native tool-call turns:")
        result = run_probe(config, model, say=out)
        counts = result["counts"]
        report["probe"] = result
        out(f"result     {counts[NATIVE_OK]} native-ok, {counts[RECOVERED]} recovered, {counts[FAILED]} failed "
            f"of {len(SCENARIOS)}; recommend tool_mode {result['recommend']}")
        try:
            saved = model_api.record_probe(config, model, {
                "recommend": result["recommend"], "counts": counts, "server": info["type"], "profile": profile.id,
                "turns": {k: v["status"] for k, v in result["turns"].items()}})
            out(f"cached     {saved} (tool_mode auto uses it)")
        except OSError as exc:
            log.warn(f"could not save the probe result: {exc}")
        if write:
            written, where = write_profile(model, profile, result, force=force)
            out(("profile    wrote " if written else "profile    not written: ") + where)
        if counts[FAILED] > len(SCENARIOS) // 2:
            code = 1  # most tool calls fail natively: the server needs fixing (or tool_mode prompted)
    elif write:
        out("profile    --write-profile needs the probe (drop --no-probe)")
    if as_json:
        say(json.dumps(report, indent=2, default=str))
    return code


def main(argv: list[str], load: Callable[[str | None], BotConfig | int]) -> int:
    """``crit doctor model [options]``; ``load`` turns ``--config`` into a config or an exit code."""
    parser = argparse.ArgumentParser(prog="crit doctor", description="check the model server crit uses")
    parser.add_argument("what", choices=["model"], help="what to check (model: the local model server)")
    parser.add_argument("--config", help="settings file (default: the one crit config shows)")
    parser.add_argument("--model", default="", help="model to check (default: the config's, else the first served)")
    parser.add_argument("--no-probe", dest="probe", action="store_false",
                        help="skip the 12-turn tool-call probe (only check the server)")
    parser.add_argument("--write-profile", action="store_true",
                        help="save the findings as a starting profile in ~/.crit/profiles")
    parser.add_argument("--force", action="store_true", help="with --write-profile: replace an existing file")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--logs", action=argparse.BooleanOptionalAction, default=False,
                        help="write diagnostic logs to stderr (default: off)")
    args = parser.parse_args(argv)
    log.configure(enabled=bool(args.logs))
    config = load(args.config)
    if isinstance(config, int):
        return config
    return run_model(config, model=args.model, probe=args.probe, write=args.write_profile, force=args.force,
                     as_json=args.json)
