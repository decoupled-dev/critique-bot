# Stage 10: release checklist

Maps each completion target in [`plan/10-open-source-release.md`](../../10-open-source-release.md)
to the commands that prove it. Tick a box only with the output saved next to this file.
Items marked **(decision)** wait for a choice listed at the end.

## 0. Decisions first

- [ ] **(decision 1) Name.** Check PyPI, GitHub, Homebrew and trademarks for the chosen name:
  ```bash
  pip index versions <name>                  # "No matching distribution" means free on PyPI
  gh repo view <org>/<name>                  # must not exist
  brew search <name>
  ```
  Then one commit: `pyproject.toml` (`name`, `description`), `NOTICE`, `mkdocs.yml`
  (`site_name`, `repo_url`), `docs/install.md`, `scripts/install.{sh,ps1}` (`repo=`),
  `docs/README.public.md`, `docs/index.md` and `docs/contributing.md` (`OWNER/REPO`), the
  `gh attestation verify --repo` lines.
- [ ] **(decision 2) History.** Fresh or published (see scan.md M1, G6, G7).
- [ ] **(decision 3) Web chat.** Ship, separate package, or leave out.
- [ ] **(decision 5) DCO or CLA.** Keep one paragraph in `CONTRIBUTING.md` "Sign-off".

## Target: secret/identity scans clean on the published history

- [ ] Build the exact history to publish (fresh: `git checkout --orphan public && git commit`;
      published: the `main` ref only, never `--all`).
- [ ] On that history:
  ```bash
  gitleaks git . --redact --report-path plan/results/10/gitleaks-history.json
  gitleaks dir . --redact --report-path plan/results/10/gitleaks-tree.json
  trufflehog git file://$PWD --only-verified=false --json > plan/results/10/trufflehog.json
  git log --format='%an <%ae>|%cn <%ce>' | sort | uniq -c     # only approved identities
  git rev-list --objects --all | git cat-file --batch-check='%(objecttype) %(objectsize) %(rest)' \
    | awk '$1=="blob" && $2>1000000'                           # no vendored binaries
  ```
- [ ] Every remaining finding is a fake fixture with a `.gitleaksignore` fingerprint.
- [ ] A second person reviews the list of removed material (plan/10, Risks).
- [ ] `.cursor/` plan note and `.crit/settings.json` dealt with (scan.md G4, G5).

## Target: fresh clone → `pipx install` → first task with a local model in ≤ 5 minutes, README only

- [ ] Publish to TestPyPI first, then on a clean VM (Ubuntu 24.04 and macOS), timed:
  ```bash
  time pipx install <name>
  ollama pull qwen3:8b && OLLAMA_CONTEXT_LENGTH=32768 ollama serve &
  crit config --backend local --local-url http://127.0.0.1:11434 --model qwen3:8b
  crit doctor model && crit doctor sandbox
  git clone <small sample repo> && cd <repo> && crit --yes "make the failing test pass"
  ```
  following only `docs/README.public.md` (moved to `README.md`). Record the time and every
  place the README was not enough.

## Target: CI green on all three OSes; nightly eval running

- [x] Lint job (stdlib compile + SPDX check), Python 3.10–3.13 on Linux, Windows, macOS in
      `.github/workflows/build.yml` (`fd9c0da`).
- [ ] First green run of every job, then set `experimental: false` for macOS.
- [ ] Pin `ruff` (and `mypy` for the engine/tools/models layers) in `constraints.txt`, add
      them to the build-only list in `tests/test_privacy.py`, and replace the stdlib lint.
- [ ] Sandbox escape tests as their own Linux job (bubblewrap installed):
  `sudo apt-get install -y bubblewrap && python -m unittest tests.test_sandbox -v` and
  `crit doctor sandbox`.
- [ ] Nightly eval smoke (scheduled workflow, self-hosted GPU runner or a 7B model on CPU):
  `crit eval --tag quick --config <runner config> --runs 1 --out eval-nightly` and upload
  `summary.md`.

## Target: docs site live, with every page in item 6

- [x] `mkdocs.yml` with a nav over the existing pages, `docs/index.md` (`0e1ec71`).
- [ ] Pin `mkdocs-material` in `constraints.txt`; `mkdocs build --strict` passes locally.
- [ ] Pages still missing: server API (from the Stage 5 OpenAPI spec), AOSP pack (Stage 9),
      a review/CI page that is not GitLab-only.
- [ ] Pages job (`actions/upload-pages-artifact` + `actions/deploy-pages`, pinned by SHA)
      on pushes to `main`; enable GitHub Pages.

## Target: CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, templates in place

- [x] `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `GOVERNANCE.md`,
      `.github/ISSUE_TEMPLATE/`, `.github/PULL_REQUEST_TEMPLATE.md` (`bf82339`).
- [ ] Fill the placeholders: conduct contact, maintainer names, security response times,
      DCO or CLA (and its check: the DCO GitHub App, or a CLA bot).
- [ ] Turn on private vulnerability reporting (Settings → Security) and check the link in
      `.github/ISSUE_TEMPLATE/config.yml`.
- [ ] Labels and a public roadmap (GitHub Projects) seeded from `plan/`; a few "good first
      issue" items from Stages 6 and 8.

## Target: release 2.0.0 on PyPI with signed artifacts and SBOM

- [ ] `pyproject.toml` `version = "2.0.0"`, a `CHANGELOG.md`, tag `v2.0.0`.
- [ ] PyPI trusted publishing: a `pypi` environment and a publish job using
      `pypa/gh-action-pypi-publish` pinned by SHA with `id-token: write`.
- [ ] Signed artifacts: the release job already attests build provenance (Sigstore);
      check with `gh attestation verify <file> --repo <org>/<name>`.
- [ ] SBOM per release: pin `cyclonedx-bom` and add
      `cyclonedx-py environment -o artifacts/sbom.cdx.json` to the release job.
- [ ] `python scripts/third_party_notices.py` rerun with crit's environment after any pin
      change (it now lists the tree-sitter extra and the bundled runtime: `4632b24`).
- [ ] `pipx install <name>` and `uv tool install <name>` work from PyPI; Homebrew tap formula.

## Target: comparison numbers reproducible with one documented `crit eval` command

- [ ] On the reference machine, same server process for both:
  ```bash
  crit eval --config <qwen.json> --runs 3 --out .crit/eval/crit --note "<server, quant, flags>"
  crit eval --runner opencode --model <provider/model> --runs 3 --out .crit/eval/opencode --note "<same>"
  crit eval compare .crit/eval/opencode .crit/eval/crit
  ```
- [ ] Fill `docs/comparison.md` from those outputs only (commit, opencode version, server
      version and launch line); link the raw `results.jsonl`, `summary.md`, `meta.json`.
- [ ] Copy the headline row into the public README with the method link.

## Before flipping the repository to public

- [ ] `PYTHONPATH=src python -m unittest discover -s tests` green; CI green on `main`.
- [ ] `PYTHONPATH=src python -m critique_bot.eval_harness audit` passes.
- [ ] `python scripts/check_webchat_removable.py` passes (or the folder is gone, decision 3).
- [ ] `docs/README.public.md` moved to `README.md`; the old README's rollout material moved
      into `docs/` (it is the web-chat and GitLab-runner guide).
- [ ] The scans above clean, reviewed by a second person.
