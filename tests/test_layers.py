"""The harness is layered; this test keeps it that way.

Each module belongs to one layer. A module may import from its own layer and the layers
below it, never above. Above the model layer, code reaches a model only through
``model_api`` (the interface); the backends are wired in by the routing layer. This keeps
each layer replaceable and testable on its own with fakes of its neighbours.

Imports inside functions count too: a lazy import is still a dependency.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "critique_bot"

CORE, MODELS, TOOLS, ENGINE, APPS, ROUTING = range(6)
LAYER_NAMES = ("core", "models", "tools", "engine", "apps", "routing")

LAYERS: dict[str, int] = {
    # core: shared by everything, depends on nothing above it
    "__init__": CORE,
    "log": CORE,
    "env_names": CORE,
    "config": CORE,
    "config_prompts": CORE,
    "patch": CORE,
    "redaction": CORE,
    "privacy": CORE,
    "egress": CORE,
    "output": CORE,
    "roles": CORE,
    # models: the model behind crit; model_api is its interface
    "model_api": MODELS,
    "provider": MODELS,
    "browser": MODELS,
    "chat_client": MODELS,
    "local_model": MODELS,
    "setup_ui": MODELS,
    # tools: what the agent can do on this machine; tool_api is its interface
    "tool_api": TOOLS,
    "agent_tools": TOOLS,
    "agent_shell": TOOLS,
    "agent_edit": TOOLS,
    "agent_build": TOOLS,
    "agent_rules": TOOLS,
    "code_index": TOOLS,
    "file_catalog": TOOLS,
    "big_file": TOOLS,
    "workspace": TOOLS,
    "project_home": TOOLS,
    # engine: the agent loop; sees models and the UI only through interfaces
    "agent": ENGINE,
    "agent_helpers": ENGINE,
    "ui_api": ENGINE,
    "openai_format": ENGINE,
    # apps: things built on the engine and the models
    "review_agent": APPS,
    "review_session": APPS,
    "review_comments": APPS,
    "gitlab": APPS,
    "gitlab_post": APPS,
    "queue": APPS,
    "worker": APPS,
    "agent_ui": APPS,
    "agent_acp": APPS,
    "welcome": APPS,
    "setup_wizard": APPS,
    "config_cmd": APPS,
    "installer": APPS,
    # routing: command line; the only place that wires concrete pieces together
    "cli": ROUTING,
    "agent_cli": ROUTING,
    "__main__": ROUTING,
}

#: The interface of each layer that layers above may use. Anything else in that layer is
#: an implementation detail, reachable only from the same layer or from routing.
INTERFACES: dict[int, frozenset[str]] = {
    MODELS: frozenset({"model_api"}),
    # agent_tools (the tool implementations) sits behind tool_api's registry. The workspace
    # services the engine drives directly are part of the interface.
    TOOLS: frozenset({"tool_api", "agent_shell", "agent_edit", "agent_rules", "agent_build", "code_index", "project_home"}),
}

#: Known crossings still to remove. Each entry is (importer, imported). The test fails when
#: a crossing not listed here appears, and when a listed one is gone (so the list shrinks).
KNOWN: frozenset[tuple[str, str]] = frozenset()


def _imports(path: Path) -> set[str]:
    """The critique_bot modules ``path`` imports, as bare module names."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "critique_bot" and len(parts) > 1:
                    found.add(parts[1])
        elif isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split(".")
            if parts[0] != "critique_bot" or node.level:
                continue
            if len(parts) > 1:
                found.add(parts[1])
            else:  # from critique_bot import x, y
                for alias in node.names:
                    if (PACKAGE / f"{alias.name}.py").is_file():
                        found.add(alias.name)
                    else:
                        found.add("__init__")
    return found


def crossings() -> set[tuple[str, str, str]]:
    """Every (importer, imported, why) that breaks the layering."""
    bad: set[tuple[str, str, str]] = set()
    for path in sorted(PACKAGE.glob("*.py")):
        name = path.stem
        mine = LAYERS[name]
        for dep in _imports(path):
            if dep == name or dep not in LAYERS:
                continue
            theirs = LAYERS[dep]
            if theirs > mine:
                bad.add((name, dep, f"{LAYER_NAMES[mine]} imports {LAYER_NAMES[theirs]}"))
            elif theirs < mine and mine != ROUTING and dep not in INTERFACES.get(theirs, frozenset({dep})):
                bad.add((name, dep, f"{LAYER_NAMES[mine]} uses {LAYER_NAMES[theirs]} past its interface"))
    return bad


class LayerTests(unittest.TestCase):
    def test_every_module_has_a_layer(self) -> None:
        missing = sorted(path.stem for path in PACKAGE.glob("*.py") if path.stem not in LAYERS)
        self.assertEqual(missing, [], "add new modules to LAYERS in tests/test_layers.py")
        gone = sorted(name for name in LAYERS if not (PACKAGE / f"{name}.py").is_file())
        self.assertEqual(gone, [], "remove deleted modules from LAYERS")

    def test_imports_follow_the_layers(self) -> None:
        found = crossings()
        new = sorted(item for item in found if (item[0], item[1]) not in KNOWN)
        self.assertEqual(new, [], "a module imports past its layer; go through the layer's interface")
        fixed = sorted(KNOWN - {(item[0], item[1]) for item in found})
        self.assertEqual(fixed, [], "these crossings are gone: remove them from KNOWN")


if __name__ == "__main__":
    unittest.main()
