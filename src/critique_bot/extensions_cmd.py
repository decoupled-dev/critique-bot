# SPDX-License-Identifier: Apache-2.0
"""``crit trust``, ``crit tool`` and ``crit mcp``: look at, approve and test extensions.

- ``crit trust``                list the project's extension files and whether each is on
- ``crit trust FILE...``        approve files as they are now (an edit needs a new approval)
- ``crit trust --revoke FILE``  take an approval back
- ``crit trust --list``         every approval on this machine
- ``crit tool list``            manifest tools, with their scope and state
- ``crit tool show NAME``       one tool: what the model sees, the argv, the description lint
- ``crit tool test NAME [--args JSON]``  check the schema, run the examples (or the given
  arguments), and check the output cap and the timeout
- ``crit mcp list``             configured MCP servers and their tools (connects to them)
- ``crit mcp approve SERVER [TOOL...]``  show what changed and pin the server's tools again
"""

from __future__ import annotations

import argparse
import json
import shlex
import time
from pathlib import Path
from types import SimpleNamespace

from critique_bot.extensions import agent_files, commands, hooks, manifest, mcp_client, trust

__all__ = ["main_mcp", "main_tool", "main_trust"]


def _root(raw: str | None = None) -> Path:
    from critique_bot.project_home import find_bot_home

    start = Path(raw or ".").resolve()
    home = find_bot_home(start)
    return home.root if home is not None else start


def _builtin_names() -> tuple[str, ...]:
    from critique_bot.tool_api import ALLOWED_TOOLS

    return tuple(ALLOWED_TOOLS)


def describe(item: trust.ExtFile) -> list[str]:
    """What a file grants, in plain lines: shown before it is approved."""
    lines: list[str] = []
    try:
        if item.kind == "tools":
            tool = manifest.load_manifest(item.path, scope=item.scope, builtin_names=_builtin_names())
            access = "read only" if tool.read_only else "may change things"
            lines.append(f"tool {tool.name} ({access}, sandbox {tool.sandbox}, timeout {tool.timeout_s:g}s)")
            lines.append(f"  runs: {shlex.join(tool.argv)}")
            if tool.network:
                lines.append(f"  network: {', '.join(tool.network)}")
            if tool.env:
                lines.append(f"  env: {', '.join(tool.env)}")
        elif item.kind == "hooks":
            for hook in hooks.load_hooks(item.path):
                matcher = f" on {hook.matcher}" if hook.matcher else ""
                lines.append(f"hook {hook.event}{matcher}: runs {shlex.join(hook.argv)}")
        elif item.kind == "mcp":
            servers, problems = mcp_client.load_config(item.path, scope=item.scope)
            lines.extend(f"MCP server {server.describe()}" for server in servers)
            lines.extend(f"problem: {problem}" for problem in problems)
        elif item.kind == "agents":
            agent = agent_files.load_agent(item.path, scope=item.scope)
            lines.append(f"agent {agent.name}: mode {agent.mode or 'of the session'}, tools "
                         f"{', '.join(agent.tools) if agent.tools is not None else 'all'}")
        elif item.kind == "commands":
            command = commands.parse_command(item.path.read_text(encoding="utf-8"), path=item.path)
            lines.append(f"command {command.word}: {command.description}" + (f" (mode {command.mode})" if command.mode else ""))
    except Exception as exc:  # noqa: BLE001 - shown, not raised: the user is looking at a broken file
        lines.append(f"problem: {exc}")
    return lines


# --------------------------------------------------------------------------- crit trust

def main_trust(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="crit trust", description="Approve the project's extension files.")
    parser.add_argument("files", nargs="*", help="files to approve as they are now")
    parser.add_argument("--list", action="store_true", help="every approval on this machine")
    parser.add_argument("--revoke", metavar="FILE", action="append", default=[], help="take an approval back")
    parser.add_argument("--repo", default=None, help="the project folder (default: here)")
    args = parser.parse_args(argv)
    if args.list:
        found = trust.trusted()
        values = trust.settings_approvals()
        if not found and not values:
            print("No approvals yet.")
        for path, entry in sorted(found.items()):
            state = trust.state_of(Path(path)) if Path(path).exists() else "missing"
            print(f"{state:9} {path}  ({entry.get('approved', '?')}, sha256 {str(entry.get('sha256'))[:12]})")
            for name in sorted(entry.get("covers") or {}):
                print(f"            also covers {name}")
        for path, entry in sorted(values.items()):
            keys = ", ".join(entry.get("keys") or []) or "values"
            print(f"{'settings':9} {path}  ({entry.get('approved', '?')}: {keys})")
        return 0
    code = 0
    for raw in args.revoke:
        path = Path(raw).resolve()
        print(("Revoked " if trust.untrust(path) else "No approval for ") + str(path))
    for raw in args.files:
        path = Path(raw).resolve()
        if not path.is_file():
            print(f"No such file: {raw}")
            code = 1
            continue
        if _is_settings_file(path) or _is_project_config(path):
            code = _trust_values(path) or code
            continue
        kind = _kind_of(path)
        item = trust.ExtFile(kind or "tools", "project", path)
        print(f"{path}:")
        for line in describe(item) if kind else ["not an extension file crit reads; approving it anyway"]:
            print("  " + line)
        covers = trust.covered_files(path)
        for name, digest in sorted(covers.items()):
            print(f"  runs {name}" + ("" if digest else " (not there yet: it must stay absent)"))
        try:
            digest = trust.trust(path, kind=kind)
        except (OSError, ValueError) as exc:
            print(f"  not approved: {exc}")
            code = 1
            continue
        print(f"  approved (sha256 {digest[:12]}). An edit to the file"
              + (" or to a program it runs" if covers else "") + " turns it off until you approve it again.")
    if args.files or args.revoke:
        return code
    root = _root(args.repo)
    from critique_bot.project_home import BotHomeError, describe_unapproved, find_bot_home

    try:
        home = find_bot_home(root)
    except BotHomeError:
        home = None
    if home is not None and home.unapproved:
        print(f"{'off: not approved':28} {home.settings_path}")
        for line in describe_unapproved(home):
            print("    " + line)
    items = [item for item in trust.discover(root) if item.scope == "project"]
    if not items:
        print(f"No extension files in {trust.project_ext_dir(root)}.")
        return 0
    for item in items:
        if item.needs_trust:
            state = trust.state_of(item.path)
            label = {"approved": "on", "changed": "off: changed", "new": "off: not approved"}[state]
        else:
            label = "on (a prompt)" + ("" if trust.is_trusted(item.path) else "; mode limited until approved")
        print(f"{label:28} {item.path}")
        for line in describe(item):
            print("    " + line)
    print("Approve a file with: crit trust <file>")
    return 0


def _is_settings_file(path: Path) -> bool:
    from critique_bot.project_home import BOT_DIR_NAME, LEGACY_DIR_NAME, SETTINGS_NAME

    return path.name == SETTINGS_NAME and path.parent.name in {BOT_DIR_NAME, LEGACY_DIR_NAME}


def _is_project_config(path: Path) -> bool:
    from critique_bot.config import CONFIG_FILE_NAME

    return path.name == CONFIG_FILE_NAME and path.parent.name not in {".crit", ".bot"}


def _trust_values(path: Path) -> int:
    """``crit trust .crit/settings.json`` / ``crit trust config.json``: approve what they set now."""
    from critique_bot import project_home

    print(f"{path}:")
    try:
        if _is_settings_file(path):
            keys = project_home.approve_settings_file(path)
            print("  approved: " + (", ".join(keys) if keys else "nothing in it needs approval")
                  + ". A change to these values turns them off until you approve them again.")
        else:
            project_home.approve_root_config(path)
            print("  approved as this project's config (the model server it names). "
                  "An edit turns it off until you approve it again.")
    except project_home.BotHomeError as exc:
        print(f"  not approved: {exc}")
        return 1
    return 0


def _kind_of(path: Path) -> str:
    if path.name in {"hooks.toml", "hooks.json"}:
        return "hooks"
    if path.name == "mcp.json":
        return "mcp"
    parent = path.parent.name
    return parent if parent in {"tools", "agents", "commands"} else ""


# --------------------------------------------------------------------------- crit tool

def _all_tools(root: Path) -> list[tuple[trust.ExtFile, manifest.ManifestTool | None, str]]:
    out = []
    for item in trust.discover(root, ["tools"]):
        try:
            tool = manifest.load_manifest(item.path, scope=item.scope, builtin_names=_builtin_names())
            out.append((item, tool, ""))
        except (OSError, manifest.ManifestError) as exc:
            out.append((item, None, str(exc)))
    return out


def _find_tool(root: Path, name: str) -> tuple[trust.ExtFile, manifest.ManifestTool] | None:
    found = None
    for item, tool, _ in _all_tools(root):
        if tool is not None and tool.name == name:
            found = (item, tool)
    return found


def main_tool(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="crit tool", description="List, show and test manifest tools.")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="every manifest tool")
    show = sub.add_parser("show", help="one tool in detail")
    show.add_argument("name")
    test = sub.add_parser("test", help="check and run a tool")
    test.add_argument("name")
    test.add_argument("--args", default=None, help="arguments as a JSON object (default: the examples)")
    parser.add_argument("--repo", default=None, help="the project folder (default: here)")
    args = parser.parse_args(argv)
    root = _root(args.repo)
    if args.action == "list":
        rows = _all_tools(root)
        if not rows:
            print(f"No manifest tools. Add one to {trust.user_dir() / 'tools'} or {trust.project_ext_dir(root) / 'tools'}.")
            return 0
        for item, tool, problem in rows:
            state = "on" if item.active() else "off (crit trust)"
            if tool is None:
                print(f"{'broken':8} {item.scope:8} {item.path}: {problem}")
                continue
            access = "read" if tool.read_only else "write"
            print(f"{tool.name:24} {item.scope:8} {access:6} {state:18} {item.path}")
        return 0
    found = _find_tool(root, args.name)
    if found is None:
        print(f"No manifest tool named {args.name}. crit tool list shows them.")
        return 1
    item, tool = found
    if args.action == "show":
        print(json.dumps(tool.spec().to_openai(), indent=2, ensure_ascii=False))
        print(f"file: {item.path} ({item.scope}, {'on' if item.active() else 'off until crit trust'})")
        print(f"argv: {json.dumps(tool.argv)}" + (f"; stdin: {tool.stdin}" if tool.stdin else ""))
        problems = manifest.lint_description(tool.description)
        print("description: " + ("good" if not problems else "missing " + "; ".join(problems)))
        return 0
    return _test_tool(item, tool, root, args.args)


def _test_tool(item: trust.ExtFile, tool: manifest.ManifestTool, root: Path, raw_args: str | None) -> int:
    if not item.active():
        print(f"{item.path} came with the project and is not approved; testing would run it. Approve it first: "
              f"crit trust {item.path}")
        return 1
    failures = 0
    lint = manifest.lint_description(tool.description)
    for problem in lint:
        print(f"lint: {problem}")
    if raw_args is not None:
        try:
            given = json.loads(raw_args)
        except json.JSONDecodeError as exc:
            print(f"--args is not JSON: {exc}")
            return 1
        cases = [manifest.Example(given if isinstance(given, dict) else {}, True, "")]
    else:
        cases = list(tool.examples)
    if not cases:
        print("No [[examples]] in the manifest and no --args: only the schema and the description were checked.")
        return 1 if lint else 0
    ctx = SimpleNamespace(workspace=root, max_chars=0, cancel=None)
    cap_chars = tool.output_cap_tokens * manifest.CHARS_PER_TOKEN
    for index, case in enumerate(cases, start=1):
        checked, problems = tool.check(case.args)
        label = f"example {index} {json.dumps(case.args, ensure_ascii=False)}"
        if problems:
            if case.expect_ok:
                failures += 1
                print(f"FAIL {label}: {'; '.join(problems)}")
            else:
                print(f"ok   {label}: refused as expected ({problems[0]})")
            continue
        started = time.monotonic()
        result = tool.execute(checked, ctx)
        seconds = time.monotonic() - started
        output = str(result.get("output") or result.get("error") or "")
        why = []
        if bool(result.get("ok")) != case.expect_ok:
            why.append(f"ok is {result.get('ok')}, expected {case.expect_ok}")
        if case.expect_output and case.expect_output not in output:
            why.append(f"the output does not contain {case.expect_output!r}")
        if len(output) > cap_chars + 400:
            why.append(f"the output ({len(output)} chars) is over the cap ({cap_chars})")
        if seconds > tool.timeout_s + 5:
            why.append(f"it ran {seconds:.0f}s, over the {tool.timeout_s:g}s timeout")
        if why:
            failures += 1
            print(f"FAIL {label}: {'; '.join(why)}")
            print("     " + output[:600].replace("\n", "\n     "))
        else:
            print(f"ok   {label} ({seconds:.1f}s, {len(output)} chars)")
    print(f"{len(cases) - failures} of {len(cases)} passed" + (f"; {len(lint)} description problem(s)" if lint else ""))
    return 1 if failures else 0


# --------------------------------------------------------------------------- crit mcp

def _servers(root: Path) -> tuple[list[mcp_client.McpServer], list[str]]:
    servers: dict[str, mcp_client.McpServer] = {}
    notes: list[str] = []
    for item in trust.discover(root, ["mcp"]):
        if not item.active():
            notes.append(f"{item.path} is off until approved: crit trust {item.path}")
            continue
        found, problems = mcp_client.load_config(item.path, scope=item.scope)
        notes.extend(problems)
        for server in found:
            servers[server.name] = server
    return list(servers.values()), notes


def main_mcp(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="crit mcp", description="MCP servers: list them, approve changed tools.")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="the servers and their tools")
    approve = sub.add_parser("approve", help="pin a server's tools as they are now")
    approve.add_argument("server")
    approve.add_argument("tools", nargs="*")
    approve.add_argument("--yes", action="store_true", help="do not ask")
    parser.add_argument("--repo", default=None, help="the project folder (default: here)")
    args = parser.parse_args(argv)
    root = _root(args.repo)
    servers, notes = _servers(root)
    for note in notes:
        print(note)
    if args.action == "list":
        if not servers:
            print("No MCP servers. Add them to ~/.crit/mcp.json (the mcpServers shape Claude Code uses).")
            return 0
        for server in servers:
            print(server.describe())
            try:
                client = mcp_client.connect(server, root)
            except mcp_client.McpError as exc:
                print(f"  not connected: {exc}")
                continue
            try:
                listed = client.list_tools()
            finally:
                client.close()
            pinned = mcp_client.pin_tools(server.name, listed)
            blocked = {name for name, _, _ in pinned.changed}
            for tool in listed:
                state = "CHANGED (crit mcp approve)" if tool["name"] in blocked else "pinned"
                first = str(tool.get("description") or "").split("\n", 1)[0][:90]
                print(f"  {mcp_client.tool_name(server.name, tool['name']):40} {state:26} {first}")
        return 0
    chosen = next((server for server in servers if server.name == args.server), None)
    if chosen is None:
        print(f"No MCP server named {args.server}.")
        return 1
    try:
        client = mcp_client.connect(chosen, root)
        try:
            listed = client.list_tools()
        finally:
            client.close()
    except mcp_client.McpError as exc:
        print(f"Could not reach {chosen.name}: {exc}")
        return 1
    pinned = mcp_client.pin_tools(chosen.name, listed)
    changed = [item for item in pinned.changed if not args.tools or item[0] in args.tools]
    if not changed:
        print(f"Nothing changed on {chosen.name}: its tools match the pinned definitions.")
        return 0
    for name, old, new in changed:
        print(f"--- {chosen.name}/{name}")
        print(mcp_client.definition_diff(old, new))
    if not args.yes:
        answer = input("Pin these definitions? Text in a description is read by the model as instructions. [y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            print("Not pinned; the changed tools stay off.")
            return 1
    done = mcp_client.approve_tools(chosen.name, listed, [name for name, _, _ in changed])
    print(f"Pinned {', '.join(done)}.")
    return 0

