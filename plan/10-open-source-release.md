# Stage 10: Open-source release

## Goal
Publish crit as a clean, well-governed open-source project that people can install in one
command, trust, and contribute to. This stage runs alongside Stages 5–7; the public beta
(M2) ships when it and Stages 5–7 are done.

## Scope
Repository hygiene, naming, licensing, docs, CI/release, community files, a security
policy, and the launch.

## Decisions needed from you (record in `decisions.md`)
1. **Name.** "crit" / "critique-bot": check for clashes on PyPI, GitHub, Homebrew, and
   trademarks before launch. The package name and CLI name can differ.
2. **History.** Publish the existing history or a fresh one. The history has 169+ commits,
   with company-specific defaults, internal hostnames and possibly config files. A fresh
   history (squashed initial commit) is safer unless a full secret and identity scan comes
   back clean.
3. **The web-chat backend** is a removable module (`critique_bot/webchat/`, optional extra)
   since Stage 1. Decide before launch whether the public repo ships it, ships it as a
   separate package, or leaves it out (driving third-party web UIs can conflict with those
   services' terms). Leaving it out is deleting the folder.
4. **Company-specific integrations** (GitLab posting, the job queue, Jira): keep them as
   generic integrations (they are useful to everyone), with no company defaults.
5. **Governance:** a single maintainer to start, CONTRIBUTING with a DCO sign-off, or a CLA.
   DCO is lighter.

## Work items

### Hygiene
1. Secret and identity scan of every file and the whole history: `gitleaks detect`,
   `trufflehog git`, plus a grep for internal hostnames, emails, company names and tokens. Clean
   up or choose a fresh history (decision 2).
2. Remove or genericize: internal URLs in defaults, `config.studio.json`, `config.json*`
   in working directories (make sure they are gitignored), company AI-service and web-UI
   wording in the README and docs, internal test fixtures.
3. `.gitignore` audit: `out/`, `build/`, `dist/`, `.crit/`, `research_notes/` if they shouldn't
   be public, `reports/`.
4. License check: Apache-2.0 is in place; verify `NOTICE` and `THIRD_PARTY_NOTICES.md`
   against every dependency and bundled grammar (tree-sitter grammars have their own
   licenses); add SPDX headers.

### Docs
5. README rewrite for the public: what crit is in one paragraph, a 60-second quickstart with a
   local model (llama.cpp/vLLM/Ollama preset), the comparison table with **measured** numbers
   from `crit eval` (same model, opencode vs crit, with the method and commit linked), the
   security model, and links.
6. A docs site (mkdocs-material, GitHub Pages): install, local models and profiles, tools and
   extensions, agents, sandbox and security, server API (from the OpenAPI spec), ACP/IDE,
   review/CI, AOSP pack, eval harness, architecture, contributing.
7. Comparison page: fair and sourced. No claims beyond what the eval shows; name the
   opencode version tested.

### Community
8. `CONTRIBUTING.md` (dev setup with the scratch venv, the layer rules, test commands, how to add
   a tool/agent/provider, the eval-before-merge rule), `CODE_OF_CONDUCT.md` (Contributor
   Covenant), `SECURITY.md` (private reporting via GitHub advisories, response times, the
   sandbox threat model), issue and PR templates, `GOVERNANCE.md` (short).
9. Labels and a public roadmap (GitHub Projects) seeded from this plan.

### CI and release
10. GitHub Actions: lint (ruff), types (mypy on the engine/tools/models layers), the test matrix
    (Linux/macOS/Windows × Python 3.10–3.13), the layer test, the grader audit, the sandbox escape
    tests (Linux), and a nightly eval smoke against a small local model (Qwen 7B-class on CPU or a
    self-hosted GPU runner).
11. Release: PyPI (trusted publishing), `pipx install` / `uv tool install`, Homebrew tap, the
    existing .deb and Windows zip, and signed artifacts (sigstore). An SBOM per release.
12. Versioning: SemVer; `2.0.0` as the first public release (the engine and API changes justify
    a major version); a CHANGELOG.

### Launch
13. A launch post with the eval methodology and results (crit vs opencode on the same local
    model), the security model, and the AOSP pack (if 9.2 is done, otherwise a preview).
14. Example configs for popular local setups: an RTX 4090 with Qwen3.6-27B Q4_K_M on
    llama.cpp, vLLM on 2×GPU, LM Studio on a Mac.

## Completion targets
- [ ] Secret/identity scans clean on the published history
- [ ] Fresh clone → `pipx install` → first task with a local model in ≤ 5 minutes, following only the README
- [ ] CI green on all three OSes; nightly eval running
- [ ] Docs site live, with every page in item 6
- [ ] CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, templates in place
- [ ] Release 2.0.0 on PyPI with signed artifacts and SBOM
- [ ] Comparison numbers in the README reproducible with one documented `crit eval` command

## Risks
- **Publishing something private by accident:** the scans in item 1 must pass before
  the repo is made public, and a second person reviews the diff of removed material.
- **Support load after launch:** issue templates that ask for `crit doctor` output; a
  "good first issue" label seeded with small tasks from Stages 6 and 8.
