# SPDX-License-Identifier: Apache-2.0
"""Extensions: tools, MCP servers, agents, slash commands and hooks defined in files.

Part of the tools layer (``docs/architecture.md``): everything here reaches the engine as
a :class:`~critique_bot.tool_api.ToolRegistry` (``session.load_session(...).registry(base)``),
so an extension tool is asked about, run, and recorded like a built-in one.

- ``trust``: where extension files live (``~/.crit`` and the project's ``.crit``) and which
  project files the user approved (``crit trust``), per content hash.
- ``manifest``: tools from one TOML/JSON file, run as one argv (never a shell).
- ``mcp_client``: a small MCP client (stdio, streamable HTTP), pinning, untrusted output.
- ``hooks``: programs run at session points with a JSON decision.
- ``agent_files``: agents defined in markdown with frontmatter (the former roles included).
- ``commands``: slash commands from markdown templates.
- ``runner``: the one place an extension's program starts (Stage 4's sandbox replaces it).
- ``worktree``: a throwaway git worktree for an agent that writes.

See ``docs/extending.md``.
"""

from critique_bot.extensions.session import (  # noqa: F401 (the package's interface)
    DEFAULT_MAX_LOADED_TOOLS,
    ExtensionSession,
    load_session,
    offer_client_servers,
)
