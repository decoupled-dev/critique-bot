## What and why

<!-- What does this change, and why? Link the issue: "Fixes #123". -->

## How it was tested

<!-- Commands you ran and what they showed. -->

- [ ] `PYTHONPATH=src python -m unittest discover -s tests` passes
- [ ] New or changed behaviour has tests
- [ ] A new module is in `LAYERS` in `tests/test_layers.py`, with no new layer crossings
- [ ] Core crit still works without the web chat (`python scripts/check_webchat_removable.py`), if this touches models, config or the CLI
- [ ] `crit eval audit` passes, if this adds or changes an eval task

## Harness changes: eval before and after

<!-- Required for changes to prompts, tools, tool parsing, the loop or context handling.
     Same model, same server, same suite; paste `crit eval compare A B`. Otherwise write "n/a". -->

## Security and privacy

- [ ] Nothing new leaves the machine without a config switch, and redaction, egress and secret-file gating apply to any new path
- [ ] No secrets, internal host names or private code in the diff, tests or fixtures

## Docs

- [ ] User-facing changes are in `docs/`, `COMMANDS.md` or the README

<!-- Sign-off: see CONTRIBUTING.md ("Sign-off"). -->
