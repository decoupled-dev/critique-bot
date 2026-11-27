# SPDX-License-Identifier: Apache-2.0
"""One session's extensions: the tools, MCP servers and hooks that are active, decided once.

:func:`load_session` reads every extension file at session start and returns an
:class:`ExtensionSession`. Its :meth:`~ExtensionSession.registry` wraps the session's
built-in registry into one :class:`~critique_bot.tool_api.ToolRegistry` (``tool_api.merge``):
manifest and MCP tools are asked about and run through the same ``permission`` and
``execute`` as the built-ins, and the hooks see every call there. The declared tool list
does not change during the session (the prompt cache stays valid): MCP tools found later,
or schemas loaded with ``tool_search``, come back as tool results, never as new tools.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from critique_bot import log
from critique_bot.extensions import hooks as hooks_mod
from critique_bot.extensions import agent_files, manifest, mcp_client, trust

__all__ = ["ExtensionSession", "load_session", "offer_client_servers", "DEFAULT_MAX_LOADED_TOOLS"]

#: Above this many tools in total, MCP tools are listed by name and one line only.
DEFAULT_MAX_LOADED_TOOLS = 20
TOOL_SEARCH = "tool_search"
_DEFERRED_NOTE = " [schema not loaded: call tool_search with this name first]"

_client_servers: list[dict[str, Any]] = []
_client_lock = threading.Lock()


def offer_client_servers(servers: Any) -> None:
    """MCP servers an ACP client (the user's editor) offered in session/new; used by the next session."""
    with _client_lock:
        _client_servers[:] = [item for item in (servers if isinstance(servers, list) else []) if isinstance(item, dict)]


def _client_server_list() -> list[mcp_client.McpServer]:
    with _client_lock:
        raw = list(_client_servers)
    out = []
    for item in raw:
        name = str(item.get("name") or "client")
        server = mcp_client.server_from_entry(name, item, scope="client", source="ACP client")
        if isinstance(server, mcp_client.McpServer):
            out.append(server)
        else:
            log.warn(f"MCP server from the editor skipped: {server}")
    return out


@dataclass
class ExtensionSession:
    workspace: Path
    tools: list[manifest.ManifestTool] = field(default_factory=list)
    mcp_tools: list[mcp_client.McpTool] = field(default_factory=list)
    clients: list[mcp_client.McpClient] = field(default_factory=list)
    hooks: hooks_mod.Hooks = field(default_factory=hooks_mod.Hooks)
    #: Sentences for the user: inactive files, broken manifests, blocked MCP tools.
    notes: list[str] = field(default_factory=list)
    deferred: bool = False
    #: File-defined agents by name (built-in, pack, user, project), for delegate(agent=...).
    agents: dict[str, agent_files.AgentDef] = field(default_factory=dict)
    #: Active domain packs (``"packs"`` in the settings), e.g. ["aosp"].
    packs: list[str] = field(default_factory=list)
    _denials: dict[str, str] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------------ the tools

    def specs(self) -> list[Any]:
        from critique_bot.tool_api import ToolSpec

        specs = [tool.spec() for tool in self.tools]
        for tool in self.mcp_tools:
            if self.deferred:
                specs.append(ToolSpec(tool.name, (tool.one_line() or tool.remote_name) + _DEFERRED_NOTE,
                                      {"type": "object", "properties": {}, "additionalProperties": True},
                                      read_only=tool.read_only))
            else:
                specs.append(ToolSpec(tool.name, tool.description or tool.remote_name, tool.schema,
                                      read_only=tool.read_only))
        if self.deferred and self.mcp_tools:
            specs.append(ToolSpec(
                TOOL_SEARCH,
                "Load the full schema of tools listed with only a name and a line (MCP tools). Use before the "
                "first call of such a tool. Not for running them. Output: each matching tool's JSON schema.",
                {"type": "object", "properties": {
                    "query": {"type": "string", "description": "words or a tool name to look for"},
                    "names": {"type": "array", "items": {"type": "string"}, "description": "exact tool names"},
                }},
                read_only=True,
            ))
        return specs

    def _by_name(self) -> dict[str, Any]:
        found: dict[str, Any] = {tool.name: tool for tool in self.tools}
        found.update({tool.name: tool for tool in self.mcp_tools})
        return found

    def extra_registry(self):
        """The extension tools alone, as a registry (merged into the session's by :meth:`registry`)."""
        from critique_bot.tool_api import Permission, ToolRegistry

        tools = self._by_name()

        def permission(name: str, arguments: Any, ctx: Any) -> Any:
            if name == TOOL_SEARCH:
                return Permission("read", "Search tool schemas", "", tool=name)
            tool = tools.get(name)
            if tool is None:
                return Permission("read", name, "", tool=name)
            return tool.permission(arguments, ctx)

        def execute(name: str, arguments: Any, ctx: Any) -> dict[str, Any]:
            if name == TOOL_SEARCH:
                return self._tool_search(arguments)
            tool = tools.get(name)
            if tool is None:
                return {"tool": name, "ok": False, "error": f"unknown tool {name!r}"}
            try:
                result = tool.execute(arguments, ctx)
            except Exception as exc:  # noqa: BLE001 - a tool bug comes back as a result, not a crashed task
                result = {"tool": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
            _observe_taint(name, arguments, result, ctx)
            return result

        return ToolRegistry(self.specs(), execute=execute, permission=permission)

    def _tool_search(self, arguments: Any) -> dict[str, Any]:
        args = arguments if isinstance(arguments, dict) else {}
        names = [str(n) for n in args.get("names") or [] if isinstance(n, str)]
        query = str(args.get("query") or "").lower().split()
        hits = []
        for tool in self.mcp_tools:
            text = f"{tool.name} {tool.description}".lower()
            if tool.name in names or (query and all(word in text for word in query)):
                hits.append({"name": tool.name, "description": tool.description, "parameters": tool.schema})
        if not hits:
            listing = ", ".join(tool.name for tool in self.mcp_tools[:40])
            return {"tool": TOOL_SEARCH, "ok": False, "error": f"no tool matches; the deferred tools are: {listing}"}
        text = json.dumps(hits[:8], indent=1, ensure_ascii=False)
        return {"tool": TOOL_SEARCH, "ok": True, "output": text,
                "ui": {"summary": f"Loaded {min(len(hits), 8)} tool schema(s)", "diff": None, "lines": None}}

    def active(self) -> bool:
        """True when this session changes anything about the tools."""
        return bool(self.tools or self.mcp_tools or self.hooks or self.agents or self.packs)

    def delegate_spec(self, base: Any) -> Any:
        """The delegate tool's spec with the agents it can start (``agent`` in each task)."""
        from critique_bot.tool_api import ToolSpec

        if base is None or not self.agents:
            return base
        params = json.loads(json.dumps(base.parameters))
        items = (((params.get("properties") or {}).get("tasks") or {}).get("items") or {})
        props = items.get("properties")
        if isinstance(props, dict):
            props.setdefault("agent", {"type": "string", "enum": sorted(self.agents),
                                       "description": "run this agent's profile (its tools, steps, instructions)"})
        names = ", ".join(sorted(self.agents))
        return ToolSpec(base.name, base.description + f" A task may name an agent: {names}.", params,
                        read_only=base.read_only)

    def registry(self, base: Any) -> Any:
        """``base`` (the built-in tools of this session) with the extension tools merged in and hooks around all."""
        from critique_bot import tool_api

        merged = tool_api.merge(base, self.extra_registry()) if (self.tools or self.mcp_tools) else base
        if self.agents and merged.spec("delegate") is not None:
            merged = merged.with_specs([self.delegate_spec(merged.spec("delegate"))])
        if not any(hook.event in {"pre_tool", "post_tool"} for hook in self.hooks.hooks):
            return merged
        return self._with_hooks(merged)

    def _with_hooks(self, inner: Any) -> Any:
        from critique_bot.tool_api import Permission, ToolRegistry

        def key(name: str, arguments: Any) -> str:
            return name + "\0" + json.dumps(arguments, sort_keys=True, default=str)

        def permission(name: str, arguments: Any, ctx: Any) -> Any:
            perm = inner.permission(name, arguments, ctx)
            decision = self.hooks.run("pre_tool", {
                "tool": name, "arguments": arguments,
                "permission": {"kind": getattr(perm, "kind", ""), "summary": getattr(perm, "summary", ""),
                               "detail": getattr(perm, "detail", "")},
            }, tool=name, cancel=getattr(ctx, "cancel", None))
            if decision.denied:
                with self._lock:
                    self._denials[key(name, arguments)] = decision.message or "a hook denied it"
                return Permission("read", getattr(perm, "summary", name), "", getattr(perm, "detail", ""), tool=name)
            if decision.asks:
                kind = getattr(perm, "kind", "read")
                return Permission(
                    "command" if kind == "read" else kind,
                    getattr(perm, "summary", name),
                    "",
                    getattr(perm, "detail", "") or json.dumps(arguments, default=str)[:2000],
                    risk="a hook asks for your yes" + (f": {decision.message}" if decision.message else ""),
                    tool=getattr(perm, "tool", name),
                    paths=getattr(perm, "paths", ()),
                )
            return perm

        def execute(name: str, arguments: Any, ctx: Any) -> dict[str, Any]:
            with self._lock:
                denied = self._denials.pop(key(name, arguments), None)
            if denied is not None:
                return {"tool": name, "ok": False, "error": f"denied by a hook: {denied}", "denied": True}
            result = inner.execute(name, arguments, ctx)
            if self.hooks.for_event("post_tool", name):
                decision = self.hooks.run("post_tool", {
                    "tool": name, "arguments": arguments,
                    "result": {"ok": result.get("ok"), "output": str(result.get("output") or "")[:8000],
                               "error": result.get("error")},
                }, tool=name, cancel=getattr(ctx, "cancel", None))
                if decision.append_context:
                    result["output"] = (str(result.get("output") or "") + "\n\nhook: " + decision.append_context).strip()
                if decision.denied:
                    result["ok"] = False
                    result["error"] = "a hook rejected the result" + (f": {decision.message}" if decision.message else "")
            return result

        return ToolRegistry(inner.specs(), execute=execute, permission=permission)

    # ------------------------------------------------------------------ hooks for the engine

    def run_hook(self, event: str, payload: dict[str, Any] | None = None, *, cancel: Any = None) -> hooks_mod.Decision:
        if not self.hooks.for_event(event):
            return hooks_mod.Decision("allow")
        return self.hooks.run(event, dict(payload or {}), cancel=cancel)

    # ------------------------------------------------------------------ text for prompts and the UI

    def tools_text(self) -> str:
        """The extension tools as text, for the tag format whose prompt lists only the built-ins."""
        if not (self.tools or self.mcp_tools):
            return ""
        lines = ["MORE TOOLS (call them like the others; arguments as JSON):"]
        for spec in self.specs():
            lines.append(f"- {spec.name}: {spec.description}")
            props = (spec.parameters or {}).get("properties") or {}
            if props:
                lines.append("  arguments: " + json.dumps(spec.parameters, ensure_ascii=False))
        return "\n".join(lines)

    def close(self) -> None:
        for client in self.clients:
            client.close()
        self.clients.clear()


def _observe_taint(name: str, arguments: Any, result: dict[str, Any], ctx: Any) -> None:
    """Untrusted output (an MCP result, a manifest tool with ``taint``, a device log) taints the session,
    as built-in results do in agent_tools."""
    from critique_bot import taint

    try:
        taint.observe(name, arguments if isinstance(arguments, dict) else {}, result, getattr(ctx, "workspace", None))
    except Exception:  # noqa: BLE001 - bookkeeping never breaks a tool result
        pass


def _pack_tools(names: list[str], builtin_names: tuple[str, ...], notes: list[str]) -> list[manifest.ManifestTool]:
    """The manifest tools of the active packs (crit's own files: trusted, may name Python handlers)."""
    from critique_bot import packs

    out: list[manifest.ManifestTool] = []
    for name in names:
        try:
            pack = packs.load(name)
        except (KeyError, ImportError) as exc:
            notes.append(f"pack {name} not loaded: {exc}")
            continue
        for path in pack.tool_files:
            try:
                out.append(manifest.load_manifest(path, scope="pack", builtin_names=builtin_names,
                                                  handlers=pack.handlers))
            except (OSError, manifest.ManifestError) as exc:
                notes.append(f"pack {name} tool skipped: {exc}")
    return out


def _load_tools(files: list[trust.ExtFile], builtin_names: tuple[str, ...], notes: list[str], *,
                pack_tools: list[manifest.ManifestTool] | None = None) -> list[manifest.ManifestTool]:
    # Pack tools first: the user's or the project's own tool of the same name replaces one.
    found: dict[str, manifest.ManifestTool] = {tool.name: tool for tool in pack_tools or []}
    for item in files:
        if not item.active():
            notes.append(_inactive(item))
            continue
        try:
            tool = manifest.load_manifest(item.path, scope=item.scope, builtin_names=builtin_names)
        except (OSError, manifest.ManifestError) as exc:
            notes.append(f"tool skipped: {exc}")
            continue
        found[tool.name] = tool
    return list(found.values())


def _inactive(item: trust.ExtFile) -> str:
    state = trust.state_of(item.path)
    why = "changed since you approved it" if state == "changed" else "not approved yet"
    return f"{item.path} is off ({why}): it came with the project. Check it, then run: crit trust {item.path}"


def _connect_all(servers: list[mcp_client.McpServer], workspace: Path, notes: list[str]) -> tuple[
        list[mcp_client.McpClient], list[mcp_client.McpTool]]:
    clients: list[mcp_client.McpClient] = []
    tools: list[mcp_client.McpTool] = []
    if not servers:
        return clients, tools

    def one(server: mcp_client.McpServer):
        client = mcp_client.connect(server, workspace)
        try:
            return client, client.list_tools()
        except BaseException:
            client.close()
            raise

    with ThreadPoolExecutor(max_workers=min(8, len(servers))) as pool:
        futures = [(server, pool.submit(one, server)) for server in servers]
        for server, future in futures:
            try:
                client, listed = future.result(timeout=mcp_client.CONNECT_TIMEOUT_S * 2)
            except Exception as exc:  # noqa: BLE001 - one broken server does not stop the session
                notes.append(f"MCP server {server.name} not connected: {exc}")
                continue
            clients.append(client)
            pinned = mcp_client.pin_tools(server.name, listed)
            for name, _old, _new in pinned.changed:
                notes.append(
                    f"MCP tool {server.name}/{name} changed its description or schema since it was pinned; it is "
                    f"off until you check the change: crit mcp approve {server.name}"
                )
            for definition in pinned.allowed:
                tool = mcp_client.McpTool(server, client, definition)
                if len(tool.name) > 64:
                    notes.append(f"MCP tool {tool.name} skipped: the name is longer than 64 characters")
                    continue
                tools.append(tool)
    return clients, tools


def load_session(
    workspace: Path,
    settings: dict[str, Any] | None = None,
    *,
    builtin_names: Iterable[str] = (),
    connect_mcp: bool = True,
) -> ExtensionSession:
    """Every active extension for a session in ``workspace``. Problems become notes, never errors."""
    settings = settings or {}
    workspace = Path(workspace)
    builtin = tuple(builtin_names)
    session = ExtensionSession(workspace)
    if settings.get("extensions") is False:
        return session
    from critique_bot import packs

    pack_names, pack_notes = packs.requested(settings)
    packs.activate(pack_names)
    session.packs = pack_names
    session.notes.extend(pack_notes)
    offered = packs.offer(workspace, settings, trust.project_ext_dir(workspace) / "cache")
    if offered:
        session.notes.append(offered)
    session.agents, problems = agent_files.load_agents(workspace)
    session.notes.extend(f"agent skipped: {problem}" for problem in problems)
    files = trust.discover(workspace, ["tools", "hooks", "mcp"])
    session.tools = _load_tools([f for f in files if f.kind == "tools"], builtin, session.notes,
                                pack_tools=_pack_tools(pack_names, builtin, session.notes))
    hooks: list[hooks_mod.Hook] = []
    for item in [f for f in files if f.kind == "hooks"]:
        if not item.active():
            session.notes.append(_inactive(item))
            continue
        try:
            hooks.extend(hooks_mod.load_hooks(item.path))
        except (OSError, hooks_mod.HookError) as exc:
            session.notes.append(f"hooks skipped: {exc}")
    session.hooks = hooks_mod.Hooks(hooks, workspace)
    servers: dict[str, mcp_client.McpServer] = {}
    for item in [f for f in files if f.kind == "mcp"]:
        if not item.active():
            session.notes.append(_inactive(item))
            continue
        found, problems = mcp_client.load_config(item.path, scope=item.scope)
        session.notes.extend(problems)
        for server in found:
            servers[server.name] = server
    for server in _client_server_list():
        servers.setdefault(server.name, server)
    if settings.get("offline") is True:
        for name in [name for name, server in servers.items() if server.transport == "http"]:
            session.notes.append(f"MCP server {name} skipped: offline is on and it is reached over HTTP")
            servers.pop(name)
    if connect_mcp and servers:
        session.clients, session.mcp_tools = _connect_all(list(servers.values()), workspace, session.notes)
    limit = settings.get("max_loaded_tools", DEFAULT_MAX_LOADED_TOOLS)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        limit = DEFAULT_MAX_LOADED_TOOLS
    total = len(builtin) + len(session.tools) + len(session.mcp_tools)
    session.deferred = bool(session.mcp_tools) and total > limit
    return session
