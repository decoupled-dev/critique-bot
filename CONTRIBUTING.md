# Contributing to crit

Thanks for helping. This page covers the dev setup, the rules every change follows, and
how to add the common kinds of extension. The design behind it is in
[`docs/architecture.md`](docs/architecture.md); the roadmap is in [`plan/`](plan/README.md).

## Dev setup

crit needs Python 3.10 or newer and has two base dependencies (`rich`, `prompt_toolkit`).
Every package version is pinned in [`constraints.txt`](constraints.txt); install with it.

```bash
git clone <this repository> crit && cd crit
python3 -m venv .venv
. .venv/bin/activate                     # Windows: .venv\Scripts\activate
python -m pip install -c constraints.txt -e .            # core
python -m pip install -c constraints.txt -e ".[webchat]" # optional: the web-chat backend (Playwright)
python -m pip install -c constraints.txt -e ".[index]"   # optional: tree-sitter parsers
```

You can also run from the checkout without installing crit itself: put the base
dependencies in any venv and point `PYTHONPATH` at `src`:

```bash
PYTHONPATH=src python -m critique_bot --help
PYTHONPATH=src python -m unittest discover -s tests          # the whole suite, about 2 minutes
PYTHONPATH=src python -m unittest tests.test_layers -v       # one module
```

Before you open a pull request, run:

```bash
PYTHONPATH=src python -m unittest discover -s tests          # all green
PYTHONPATH=src python -m critique_bot.eval_harness audit     # crit eval audit: every grader is sound
python scripts/check_webchat_removable.py                    # core crit works without the web chat
python -W error::SyntaxWarning -m compileall -q -x tests/eval_tasks src scripts tests  # CI lint
```

CI runs the same commands on Linux, macOS and Windows (see
[`.github/workflows/build.yml`](.github/workflows/build.yml)).

## Rules every change follows

1. **Keep the layers.** crit is built in layers: core → models → tools → engine → apps →
   routing. A module imports only from its own layer and the layers below, lazy imports
   included. Above the model layer, code reaches a model only through `model_api`; the
   engine reaches tools only through `tool_api.ToolRegistry`. A new module goes into the
   `LAYERS` table in [`tests/test_layers.py`](tests/test_layers.py), which fails on any new
   crossing.
2. **Keep the tools boundary narrow.** Every tool, built-in or not, goes through
   `ToolRegistry`: spec, permission, execute. No engine special cases for one tool.
3. **The web chat stays removable.** `src/critique_bot/webchat/` is optional. Nothing
   outside it imports it or Playwright; core crit and its tests must pass with the folder
   deleted (`scripts/check_webchat_removable.py`, also in CI). A web-chat test inside a core
   test module carries `@needs_webchat` (`tests/webchat_support.py`). New features are
   designed for model APIs and never depend on a browser or on parsing a web page.
4. **Local-first and private by default.** Nothing leaves the machine unless config allows
   it. Redaction, the egress policy and secret-file gating apply on every new path. Read
   [`docs/security.md`](docs/security.md) before touching tools, commands or networking.
5. **Models differ through profiles, not code.** A model family or server quirk goes in a
   profile or server preset, never in an `if model == ...` in the engine.
6. **Measure harness changes.** A change to prompts, tools, parsing or the loop needs a
   before/after `crit eval` run on the same model and server
   ([`docs/eval.md`](docs/eval.md)); put the `crit eval compare` output in the pull request.
7. **No new base dependencies** without an issue first. A new optional dependency is an
   extra, pinned in `constraints.txt`, and listed by `scripts/third_party_notices.py`.
8. **Tests come with the change.** Use the standard library's `unittest`; tests run offline.

## How to add …

### A built-in tool

1. Add a `ToolSpec` to `BUILTIN_SPECS` in `src/critique_bot/tool_api.py`. Its description
   follows the checklist there: purpose first, then `Not for:`, `Example:` (valid JSON),
   `Returns:` and `Limits:` (`tests/test_tool_api.py` checks it). Mark `read_only` honestly.
2. Implement it in `src/critique_bot/agent_tools.py` (or a new tools-layer module, added to
   `LAYERS`) and add its name to `ALLOWED_TOOLS`, and to `MUTATING` or `READ_ONLY`.
3. Give it a permission rule (`permission_for`) and shape its output with `tool_output`.
4. Add tests, and run the eval suite before and after.

Most tools do not need to be built in: a **manifest tool** (`.crit/tools/*.toml`) or an
**MCP server** needs no code. See [`docs/extending.md`](docs/extending.md).

### An agent or a slash command

A Markdown file with front matter. Built-in agents live in `src/critique_bot/agents/`;
project and user agents in `.crit/agents/` and `~/.crit/agents/`. Commands go in
`.crit/commands/`. The format is in [`docs/extending.md`](docs/extending.md#agents).

### A model profile or server preset

A JSON file in `src/critique_bot/profiles/` (model families) or
`src/critique_bot/profiles/servers/` (servers). Set only what the model card or the
server's documentation states, and put that source in `source`. Fields and resolution:
[`docs/local-models.md`](docs/local-models.md#model-profiles). Check a real server with
`crit doctor model`.

### A model provider (backend)

Local OpenAI-compatible servers need no new backend: use a server preset. A genuinely
new backend implements `ChatProvider` / `ChatSession` from `model_api` and registers itself
with `model_api.register_backend(name, factory)`, the way `critique_bot/webchat/` does.
It lives in the models layer, is imported by nothing above it, and is an optional extra if
it needs a dependency. Cloud providers are planned for Stage 11
([`plan/11-cloud-providers.md`](plan/11-cloud-providers.md)).

### An eval task

A folder in `tests/eval_tasks/<name>/` with `task.json`, `files/`, `hidden/` and a
reference fix ([`docs/eval.md`](docs/eval.md#writing-a-task)). Then run:

```bash
PYTHONPATH=src python -m critique_bot.eval_harness audit
```

`crit eval audit` must pass: the check fails on the untouched workspace and passes with
the reference. Keep checks offline and under a minute; prefer real failures over
synthetic ones, and use no code you may not publish.

## Commits and pull requests

- Small commits with a subject like `feat: ...`, `fix: ...`, `docs: ...`, `ci: ...`,
  `test: ...`, and a body that says why.
- One topic per pull request. Fill in the template; link the issue.
- New source files start with `# SPDX-License-Identifier: Apache-2.0`.

### Sign-off

<!-- DECISION PENDING (plan/10, decision 5): DCO or CLA. Keep exactly one of the two
     paragraphs below when it is decided, and add the matching CI check. -->

**Option A, DCO:** every commit carries a `Signed-off-by: Your Name <you@example.com>`
line (`git commit -s`), which certifies the
[Developer Certificate of Origin 1.1](https://developercertificate.org/): you wrote the
change or have the right to submit it under the project's license.

**Option B, CLA:** before your first pull request is merged, you sign the project's
Contributor License Agreement (the bot comments with a link).

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).
