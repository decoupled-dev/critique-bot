# GitLab CI setup (crit)

This is the runner-side setup. Field-by-field `config.json` is in [`config.json.md`](config.json.md).

Jobs **do not launch Edge**. One long-running **worker** on the runner PC owns the signed-in browser. Each CI job only **submits** a patch to a shared on-disk queue and waits for `out/review.md`.

```text
MR pipeline  →  crit submit  →  .critique-queue  →  worker + Edge  →  review.md
                                                                    ↓
                                                         crit gitlab-post  →  MR comments
```

Copy the job definition into the **application** repo (the project whose MRs should be reviewed), not necessarily this repo:

| Runner OS | Copy this file as `.gitlab-ci.yml` |
| --- | --- |
| Linux (bash) | [`.gitlab-ci.yml`](../.gitlab-ci.yml) |
| Windows (PowerShell) | [`packaging/gitlab-ci.windows.yml`](../packaging/gitlab-ci.windows.yml) |

## What you need

| Piece | Requirement |
| --- | --- |
| Runner | Self-hosted, **shell** executor (not Docker, not Kubernetes), tag `crit` |
| Machine | 64-bit Linux or Windows with Microsoft Edge (Chrome is a fallback) |
| Shared disk | Job and worker must see the **same** `config.json` and `queue_dir` |
| Binary | `crit` on `PATH` (`.deb`: `/usr/bin/crit`; zip unpack or pip). Default install: `/opt/crit` (Linux) or `C:\crit` (Windows) |
| Session | First run opens Edge when `.edge-profile` is missing. Later runs reuse it |
| Token | Project access token, role Reporter, scope `api`, in `CRIT_GITLAB_TOKEN` (protected if you can; see below) |
| Users | Worker as its own OS user (`crit`); the runner user only in its group. See [DEPLOY.md](../DEPLOY.md#separate-the-worker-from-ci) |

Shared GitLab.com / instance runners and Docker executors cannot run crit: they have no signed-in Edge and no shared queue.

## One-time: runner PC

1. Install Microsoft Edge (`microsoft-edge-stable` on Linux).
2. Unpack the OS zip (or `pip install` the wheel) to `/opt/crit` (Windows: `C:\crit`). Keep `_internal` next to the binary.
3. Copy `config.example.json` → `config.json` in that directory. Fill it in ([`config.json.md`](config.json.md)), or run `crit setup --config /opt/crit/config.json` and click the selectors in a real browser window. Use the **same absolute path** the job will pass (`CRIT_CONFIG`).
4. Sign in once (needs a display):

   ```bash
   /opt/crit/crit worker --config /opt/crit/config.json --logs
   ```

   Log in to the page, confirm a prompt works, then Ctrl-C. Then confirm the whole setup with a one-shot prompt:

   ```bash
   /opt/crit/crit ask --config /opt/crit/config.json \
     --prompt "Reply with exactly one word: PONG."
   ```
5. Start the worker at boot, **as its own OS user, not the GitLab runner user.** A CI job runs whatever the branch's `.gitlab-ci.yml` says as the runner user; if that is also the worker user, any developer who can push a branch can read the signed-in browser profile (SSO cookies), the queue, and the token.

   - Linux: copy [`packaging/crit-worker.service`](../packaging/crit-worker.service) to `/etc/systemd/system/` and run the one-time commands at its top: create user/group `crit`, add `gitlab-runner` to that group, create the queue folder `2750`. The unit sets `CRIT_QUEUE_SHARED=1`, so the worker keeps `inbox/` group-writable (`2770`), `results/` group-readable (`2750`, files `0640`), and `processing/`, failure snapshots and the profile owner-only. Then `systemctl enable --now crit-worker`. (The `.deb` already installs `crit-worker.service` and the `crit` user.)
   - Windows: install under `C:\Program Files\crit`; run [`packaging/worker-start.ps1`](../packaging/worker-start.ps1) under a dedicated account and set `$RunnerAccount` to the runner's account.

6. Register (or retag) the runner: tag `crit`, executor **shell**, on this same machine, **for this project only** (not shared). Turn on *Protected* if reviews are only needed for protected branches, and turn off *Run untagged jobs*.

Check the worker is alive with `crit queue-status --config /opt/crit/config.json`. It exits non-zero if no worker is running and also lists waiting jobs and how recent ones ended. (Under the hood: `queue_dir`, default `/opt/crit/.critique-queue`, holds a `worker.heartbeat` updated every 5s; submit treats it stale after 20s.)

## One-time: GitLab project

1. Copy [`.gitlab-ci.yml`](../.gitlab-ci.yml) (Linux) or [`packaging/gitlab-ci.windows.yml`](../packaging/gitlab-ci.windows.yml) (Windows) into the app repo as `.gitlab-ci.yml` (or merge the `review` job into an existing pipeline).
2. Confirm the runner is available to that project (project or group runner).
3. Create a **project access token**: Settings → Access Tokens → role **Reporter**, scope `api`, with an expiry. Reporter is the lowest role that can read a private MR's diff and create MR discussions; no scope narrower than `api` can post notes. Do not use a personal or group token.
4. CI/CD variable `CRIT_GITLAB_TOKEN`:
   - Value: that token
   - Masked: yes
   - **Protect variable: yes** when the reviewed MRs come from protected branches. Protected variables are hidden from pipelines on unprotected branches; if you must post on feature-branch MRs, leave it unprotected and treat the token as readable by everyone who can push a branch (hence Reporter, one project, short expiry, rotation).
5. `CRIT_BIN` and `CRIT_CONFIG` belong in the job YAML (`variables:`), not in GitLab **Settings → CI/CD → Variables**. That UI is only needed for `CRIT_GITLAB_TOKEN`.

   | YAML variable | Linux default | Windows default | Meaning |
   | --- | --- | --- | --- |
   | `CRIT_CONFIG` | `/opt/crit/config.json` | `C:\crit\config.json` | Same file the worker uses |
   | `CRIT_BIN` | `crit` | `crit.exe` | Name on `PATH`, or an absolute path to the exe |

   Put the install on the **runner PATH** so `CRIT_BIN` can stay `crit`:

   - Linux: the `.deb` puts `/usr/bin/crit` there; otherwise install so `/usr/local/bin/crit` (or similar) is on the runner user’s `PATH`.
   - Windows: set `CRIT_BIN` in the YAML to the full path of `crit.exe` (e.g. `C:\Program Files\crit\crit.exe`), or have an administrator add that folder to the **system** PATH. `worker-start.ps1` no longer edits `PATH`: a user-writable folder early in `PATH` lets anyone who can write there shadow other programs.

   `config.json` is not found via `PATH`. Edit `CRIT_CONFIG` in `.gitlab-ci.yml` to the worker’s file (pip/venv: the clone’s `config.json`).

   On Windows, invoke it with `& "$CRIT_BIN" submit …`. Do not write `$env:CRIT_BIN submit`: GitLab expands `$env` as a CI variable (usually empty), so PowerShell tries to run `submit` and fails with `submit` not found. A literal PowerShell `$` (such as `$LASTEXITCODE`) must be written `$$` in the YAML.

`CI_JOB_TOKEN` cannot create MR notes or inline discussions. If `CRIT_GITLAB_TOKEN` is missing, the job still saves `out/review.md` as an artifact; posting comments is skipped.

## What the job does

| When | Behavior |
| --- | --- |
| Merge request pipeline | `review` runs automatically |
| Branch / main push | `review` is **manual** (Play). A push that already has an open MR does not start a second pipeline from the branch workflow rules |
| Empty diff | Job exits 0; nothing posted |

Script, in order:

1. `crit submit` (no `--patch-file` in CI). See **Original files (file-by-file)** below. Empty diff: submit exits 0 and nothing is posted.
2. If `CI_MERGE_REQUEST_IID` is set and `out/review.md` exists: `crit gitlab-post --review-file out/review.md --patch-file diff.patch`

Artifacts (always, 1 week): `out/`, `diff.patch`. Job timeout: 1 hour. Submit wait: 30 minutes.

### Original files (file-by-file)

**`submit` prepares the files. The worker only talks to the page** (pastes what submit queued into Edge). The YAML job never talks to the page.

Do **not** add `--patch-file` or `--file` to the CI script. With those omitted, submit uses the job checkout:

1. Fetch `CI_MERGE_REQUEST_DIFF_BASE_SHA` (and deepen/unshallow the target branch if that SHA is still missing). GitLab's default shallow clone often omits the merge-base even when the MR is mergeable.
2. `git diff $CI_MERGE_REQUEST_DIFF_BASE_SHA $CI_COMMIT_SHA` (two-dot: GitLab already computed the merge-base). Three-dot `base...HEAD` fails with `fatal: no merge base` on a shallow clone. Branch jobs use `HEAD~1 HEAD`.
3. If local git still cannot see those commits, submit falls back to the GitLab MR diffs API.
4. Writes `diff.patch`.
5. Reads **HEAD contents** of every changed text file from the checkout (the original files, not only the hunks). Markdown/RST and binaries are skipped; deleted paths are skipped because they are not on disk.
6. Puts the result on the queue job:
   - If template + HEAD files + patch fit in `max_prompt_chars` (default `120000`): **one prompt**, ordered instructions → original files → patch.
   - If they would overflow: **instructions** (`prompt`), **staged HEAD file bodies** (`files`, one path each, cap `max_files` default `80`), and the **patch** as its own field. The worker pastes them as separate turns in that order.

The worker then pastes that job (page only):

| What submit queued | What the worker pastes |
| --- | --- |
| One prompt | A single paste (instructions, then HEAD files, then patch) |
| `prompt` + `files` + `patch` | Instructions → each HEAD file → patch |

Overflow sequence on the same Edge tab:

1. Instructions (role, MR context, output contract). The model should reply `READY`, not a review.
2. `FILE i of N` with the HEAD body. ACK means the file is **kept for the review** (line numbers, enclosing methods), not discarded. Next file as soon as the reply starts with `ACK <path>` (`turn_pause_seconds` default `0`).
3. The unified diff last, plus `REVIEW NOW` and a reminder to use the ACK'd HEAD files. Only this last reply is `review.md`.

Caps and skips (applied by **submit** when it loads the checkout):

- At most `max_files` bodies (default `80`); each body truncated at `max_file_chars` (default `32000`).
- Markdown/RST, binaries, and missing/deleted paths are not staged (their diffs stay in `diff.patch`).
- If a file turn gets no assistant reply, the worker drops remaining file pastes and still sends the patch.
- File-by-file reviews take longer than a one-shot paste. Keep `--wait-timeout 1800` and the job `timeout: 1h`. Submit logs `inlining N changed file(s)` or `prompt, then N file(s), then patch`.

Windows: this is already how [`packaging/gitlab-ci.windows.yml`](../packaging/gitlab-ci.windows.yml) works. The `& "$CRIT_BIN" submit …` line is enough; there is no extra flag.

Concurrent MRs all enqueue. The worker runs up to `max_parallel_tabs` reviews at once (default **1**, same Edge, separate tabs). Starts are staggered by `min_interval_seconds` (default 30) plus jitter.

Each `submit` gets a unique job id and waits only for **that** id. It does not search the queue by MR. GitLab CI variables (`CI_MERGE_REQUEST_IID`, `CI_PROJECT_PATH`, `CI_JOB_ID`, …) are copied onto the job as `meta` and into the filename, e.g. `…-group-app-mr42-…`. `gitlab-post` comments on the MR of the **current** pipeline (`CI_MERGE_REQUEST_IID`), which is the same job that submitted. Results: `queue_dir/results/<job-id>/` (`job.json`, `status.json`, `review.md`). Optional `--label` overrides the slug.

## Do not

- Use a Docker / Kubernetes executor, or isolate the job from the worker’s filesystem.
- Launch `crit review --patch-file …` (no `submit`) from CI. Two one-shot processes fight over the Edge profile.
- Run two workers on the same `queue_dir`.
- Point `CRIT_CONFIG` at a different file than the worker.
- Copy `.edge-profile` from another machine (session cookies will not work).

## Quick checks

Start with a one-shot `crit ask` prompt (login, selectors, real round trip) and `crit queue-status --config "$CRIT_CONFIG"` (worker, backlog, recent failures). Between them they explain most of the table below.

| Symptom | Check |
| --- | --- |
| `$env:CRIT_BIN submit` / `submit` not found | Windows job used `$env:CRIT_BIN` without `&`. Use [`packaging/gitlab-ci.windows.yml`](../packaging/gitlab-ci.windows.yml): `& "$CRIT_BIN" submit …` |
| `worker is not running` | `queue-status`; systemd status; same `queue_dir` as the job |
| Job queued forever | Worker logs; page login expired (`worker --headed`); selectors in `config.json` |
| Review cut off mid-sentence | `selectors.stop_button` is missing or wrong, so a long pause reads as "done". Re-pick it with `crit setup`. If the page shows a "Continue generating" button, set `selectors.continue_button` ([`config.json.md`](config.json.md#continue_button)) |
| Same job retried and then failed | It hit `max_attempts` (default 3). The real error is in `status.json` and the worker log |
| Review artifact, no MR comments | `CRIT_GITLAB_TOKEN` present (a protected variable is missing on unprotected branches), scope `api`, role Reporter+. `gitlab.base_url` / `CI_SERVER_URL` must be the same host as the MR, and must not redirect. `gitlab-post` exits non-zero when the summary fails to post |
| `No Chromium browser was found` | Edge installed for the runner user |
| Permission denied on queue | Runner user not in group `crit`, queue folder not `2750` setgid, or the worker unit lacks `CRIT_QUEUE_SHARED=1` |
| Cloudflare / login page | Headless is blocked; re-login with `--headed` |
| `could not build git diff` / `no merge base` | Shallow clone missed the MR merge-base. Upgrade crit (submit fetches that SHA and uses two-dot diff). Retry the job. |
