# Stage 10: secret and identity scan

Date: 2026-11-27. Branch `wip/s10-release` (from `471dd10`). Read-only: no history was
rewritten. **No secret value is reproduced here**; snippets are redacted, and personal
names and addresses are described, not quoted.

## What was scanned

| Scope | How |
|---|---|
| Working tree: 586 tracked files | `git grep` with the patterns below |
| Full history: every ref (`git log --all`, 224 commits, 972 paths ever added, 29 local branches incl. `v1`, `v2`, `wip/*`, `worktree-agent-*`) | `git log --all -p -U0` piped through a pattern script (added lines only); commit metadata (`%an %ae %cn %ce`) and messages; the list of paths ever added and deleted; blobs over 1 MB |
| Patterns | AWS keys, PEM private keys, GitHub/GitLab/Slack/Google/Hugging Face tokens, `sk-` keys, JWTs, `Bearer` literals, `password/secret/token/api_key = "…"` assignments, `user:pass@` URLs, e-mail addresses, RFC 1918 addresses, `*.corp/.internal/.intra/.lan/.local` hosts, a list of company and personal names, home-directory paths, every `http(s)://host` (history hosts diffed against the tree's) |
| Not run | `gitleaks` and `trufflehog` are **not installed** on this machine and were not installed (rule for this stream). They must still run before publishing: see the checklist |

## Summary

| Class | Count | Meaning |
|---|---|---|
| **must-fix-before-publish** | 2 | blocks publishing the existing history as it is |
| **genericize** | 8 | change before launch; 3 of them done on this branch |
| **fine** | 9 groups | fake test data, documentation examples, upstream notices |

No real credential was found in the tree or in the history. Every key-shaped string is a
test fixture (made-up values the redaction tests must catch).

## must-fix-before-publish

| # | Where | Kind | Finding | Fix |
|---|---|---|---|---|
| M1 | History: commit metadata of 11 commits (first `bed963d`, 2026-10-07; they are reachable from `main` and 20 branches) | identity | A second human contributor is the author and committer, with a **corporate e-mail address of an IT-services company**. Publishing the history publishes that person's name and employer address, and implies a corporate contribution whose rights must be cleared. See it locally with `git log --all --format='%an <%ae>' \| sort \| uniq -c`. | Either a fresh history (decision 2), or the contributor's written consent plus confirmation from their employer that the contribution may be published under Apache-2.0 (a `.mailmap` can change the displayed address, but the original stays in the objects). |
| M2 | Scan coverage | process | The required `gitleaks detect` / `trufflehog git` runs could not be done here (tools absent). Regex scans cover the common patterns but not entropy-based detection. | Run both on the exact history to be published, on a machine where that is allowed; add fingerprints for the fake fixtures to `.gitleaksignore`; record the result in this file. |

## genericize

| # | Where | Kind | Finding | Status |
|---|---|---|---|---|
| G1 | Tree: README, `docs/config.json.md`, `docs/config-file.md`, `docs/install.md`, `docs/agent.md`, `config-file.example.json`, `scripts/setup-crit.{sh,ps1}`, `src/critique_bot/{config,config_cmd,config_prompts}.py`, `webchat/{provider,setup_ui}.py`, 3 tests | example hosts | Examples used `ai.company.com`, `gitlab.company.com`, `*.company.net`, `static.company-cdn.net`, `name@company.com`, `you@COMPANY.COM`: real, registrable domains | **Done** (`3e4c966`): RFC 2606 `example.com` / `example.net` |
| G2 | Tree: `tests/test_queue.py` | identity | The GitLab CI fixture used the project's real GitLab group path | **Done** (`3e4c966`): `example-group/...` |
| G3 | Tree: newer test fixtures in `tests/test_gitlab_jira_access.py`, `tests/test_sessions.py`, `tests/test_agent_tools.py` | fake secrets | Fake GitLab tokens (`glpat-…`), a fake `sk-…` key, a fake Google key and a fake password without a `gitleaks:allow` marker, so a gitleaks run would report them although `docs/security.md` says the scan is clean | **Done** (`a1ebfe4`) for the tree; history fingerprints wait for M2 |
| G4 | Tree: `.cursor/plans/agent_accuracy_hardening_926a8f41.plan.md` (last changed `88602b7`) | internal note | An editor-generated planning note from the early web-chat days; it describes driving a specific commercial chat site through Edge and refers to a doc that no longer exists | Recommend: delete it from the tree before launch (not done: it may be the user's working note) |
| G5 | Tree: `.crit/settings.json` | personal config | The maintainer's own crit settings (theme, role) are committed. Harmless, but a public repo shouldn't impose them | Recommend: untrack it (`.crit/.gitignore` then covers it), or keep it as a deliberate dogfooding example |
| G6 | History: `config.chatgpt.example.json`, `config.openai.example.json` and related (added from `dd40529592`, later deleted), the branch `test/chatgpt-web-e2e-mr`, 6 commit subjects | third-party service | Configs and tests for driving a specific commercial chat web UI. Not secret, but it is exactly what decision 3 is about (third-party terms) | Goes with decision 2 / 3: a fresh history drops it; with the published history, accept it as past |
| G7 | History: `vendor/codegraph/v1.6.2/*` (added `5e23bce`, removed `6cd97a2`) | size, third-party binaries | Six prebuilt archives of a third-party MIT tool, 50–63 MB each; the pack is ~325 MB. The license file was included, so it is not a licence breach, but every clone downloads it | A fresh history drops it; otherwise `git filter-repo --path vendor/ --invert-paths` (history rewrite, needs decision 2) |
| G8 | Tree: `NOTICE` (copyright holder), `docs/install.md`, `scripts/install.{sh,ps1}`, `.github/workflows/build.yml` (comment) | org name | The copyright line and the one-line installers name the current GitHub org/repo; the git remotes name the current GitHub org and GitLab group | Goes with decision 1 (name); change in one commit when it is made |

## fine

| Group | Where | Why it is fine |
|---|---|---|
| Fake keys and tokens | `tests/test_egress_hardening.py` (fake AWS key, in `.gitleaksignore`), `tests/test_agent_tools.py`, `tests/test_patch.py`, `tests/test_redaction.py`, `tests/test_review_session.py`, `tests/test_gating_bypass.py`, `tests/test_sandbox.py` (stub PEM blocks, marked `gitleaks:allow`), `tests/test_site_login.py` (generated passwords) | Made-up values the redaction and gating tests must catch |
| `user:pass@` URLs | `COMMANDS.md`, `docs/agent.md`, `scripts/setup-crit.{sh,ps1}`, tests | Placeholders (`username:password@10.1.2.3:8080`, `proxy.corp.example`) |
| Private IPs | docs and tests: `192.168.1.50`, `10.1.2.3`, `10.0.0.5`, `10.20.30.40`, … | Documentation examples, not real hosts |
| `*.corp.example`, `*.lan`, `*.local`, `*.internal` | tests and docs | Reserved or obviously fake names; `.local` hits are file names (`env.local`, `config.local`) and `threading.local`; `android.internal` is a Java package |
| E-mail addresses in the tree | tests and docs | Fictional (`jane.doe@acme…`, `bot@example.invalid`, …) |
| E-mail addresses in `THIRD_PARTY_NOTICES.md` and a deleted `package-lock.json` | upstream authors | Required or public upstream attribution |
| Generic "company" wording | docs, `config.py` (`company_config`, the shared config file), `webchat/browser.py` (company proxy messages) | Describes a deployment situation, not a specific company. The README's opening line ("your company's AI service … the default") is replaced by the public README draft |
| `plan/` | the staged plan, decisions, literature review | No secrets; mentions a home LAN machine and a hosted model used for evals. Publishing it as the roadmap is recommended |
| Bot authors | dependabot (5 commits), an editor agent identity (11 commits) | Public service identities |

## Local-only material outside git (main checkout)

`research_notes/`, `reports/`, `config.json`, `config.json.bak-cdp`, `config.studio.json`,
`.edge-profile/`, `.critique-queue/`, `build/`, `dist/`, `out/` exist in the main checkout.
None is tracked. `config.studio.json` was ignored only through `.git/info/exclude`;
`research_notes/` and `reports/` were not ignored at all. All are now covered by the tracked
`.gitignore` (`4f58ff4`).

## How to repeat the regex scan

```bash
git log --all -p -U0 --no-color --format='@@COMMIT %H' | python3 histscan.py   # added lines only
git log --all --format='%an <%ae>|%cn <%ce>' | sort | uniq -c                  # identities
git log --all --name-only --format= | sort -u                                  # every path ever added
git rev-list --objects --all | git cat-file --batch-check='%(objecttype) %(objectsize) %(rest)' \
  | awk '$1=="blob" && $2>1000000'                                             # large blobs
```

The pattern script used is kept out of the repository (it prints matched values to the
terminal); the patterns are the ones listed under "What was scanned".
