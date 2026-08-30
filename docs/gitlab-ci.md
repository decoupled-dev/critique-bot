# GitLab CI setup (critique-bot)

This is the runner-side setup. Field-by-field `config.json` is in [`config.json.md`](config.json.md).

Jobs **do not launch a browser**. One long-running **worker** on the runner PC owns Microsoft Edge. Each CI job only **submits** a patch to a shared on-disk queue and waits for `out/review.md`.

```text
MR pipeline  →  critique-bot submit  →  .critique-queue  →  worker + Edge  →  review.md
                                                                    ↓
                                                         critique-bot gitlab-post  →  MR comments
```

Copy [`.gitlab-ci.yml`](../.gitlab-ci.yml) into the **application** repo (the project whose MRs should be reviewed), not necessarily this repo.

## What you need

| Piece | Requirement |
| --- | --- |
| Runner | Self-hosted, **shell** executor (not Docker, not Kubernetes), tag `critique-bot` |
| Machine | 64-bit Linux or Windows with **Microsoft Edge** (Chrome is a fallback) |
| Shared disk | Job and worker must see the **same** `config.json` and `queue_dir` |
| Binary | `critique-bot` on `PATH` (zip unpack or pip). Default install path: `/opt/critique-bot` |
| Session | Edge signed in once on that machine (`--headed`). Later runs reuse `.edge-profile` |
| Token | Project (or group) access token, scope `api`, role Developer+, in `CRITIQUE_GITLAB_TOKEN` |

Shared GitLab.com / instance runners, Docker executors, and GitHub-hosted VMs cannot do this: they have no signed-in Edge and no shared queue.

## One-time: runner PC

1. Install Microsoft Edge (`microsoft-edge-stable` on Linux).
2. Unpack the OS zip (or `pip install` the wheel) to `/opt/critique-bot` (Windows: `C:\critique-bot`). Keep `_internal` next to the binary.
3. Copy `config.example.json` → `config.json` in that directory. Fill it in ([`config.json.md`](config.json.md)). Use the **same absolute path** the job will pass (`CRITIQUE_CONFIG`).
4. Sign in once (needs a display):

   ```bash
   /opt/critique-bot/critique-bot worker --config /opt/critique-bot/config.json --headed --logs
   ```

   Log in to the chat UI, confirm a prompt works, then Ctrl-C.
5. Start the worker at boot, **as the same OS user as the GitLab runner** (so the job can read/write the queue):

   - Linux: copy [`packaging/critique-bot-worker.service`](../packaging/critique-bot-worker.service) to `/etc/systemd/system/`, set `User=` / `Group=` to the runner account, then `systemctl enable --now critique-bot-worker`.
   - User unit: `~/.config/systemd/user/critique-bot-worker.service` on that account.
   - Windows: [`packaging/worker-start.ps1`](../packaging/worker-start.ps1) at logon or a scheduled task.

6. Register (or retag) the runner: tag `critique-bot`, executor **shell**, on this same machine.

Check the worker is alive: `queue_dir` (default `/opt/critique-bot/.critique-queue`) should contain a fresh `worker.heartbeat` (updated every 5s; submit treats it stale after 20s).

## One-time: GitLab project

1. Copy [`.gitlab-ci.yml`](../.gitlab-ci.yml) into the app repo (or merge the `review` job into an existing pipeline).
2. Confirm the runner is available to that project (project or group runner).
3. Create a **project access token**: Settings → Access Tokens → role Developer or higher, scope `api`.
4. CI/CD variable `CRITIQUE_GITLAB_TOKEN`:
   - Value: that token
   - Masked: yes
   - **Protect variable: no** (protected variables are hidden from feature-branch MR pipelines)
5. Optional variables (defaults are already in `.gitlab-ci.yml`):

   | Variable | Default | Meaning |
   | --- | --- | --- |
   | `CRITIQUE_CONFIG` | `/opt/critique-bot/config.json` | Same file the worker uses |
   | `CRITIQUE_BIN` | `critique-bot` | Binary name or absolute path |

`CI_JOB_TOKEN` cannot create MR notes or inline discussions. If `CRITIQUE_GITLAB_TOKEN` is missing, the job still saves `out/review.md` as an artifact; posting comments is skipped.

## What the job does

| When | Behavior |
| --- | --- |
| Merge request pipeline | `review` runs automatically |
| Branch / main push | `review` is **manual** (Play). A push that already has an open MR does not start a second pipeline from the branch workflow rules |
| Empty diff | Job exits 0; nothing posted |

Script, in order:

1. Write `diff.patch` (`target...HEAD` on MRs, else `HEAD~1...HEAD`).
2. `critique-bot submit --config "$CRITIQUE_CONFIG" --patch-file diff.patch --output-dir out --wait-timeout 1800`
3. If `CI_MERGE_REQUEST_IID` is set and `out/review.md` exists: `critique-bot gitlab-post --review-file out/review.md --patch-file diff.patch`

Artifacts (always, 1 week): `out/`, `diff.patch`. Job timeout: 1 hour. Submit wait: 30 minutes.

Concurrent MRs all enqueue. The worker runs up to `max_parallel_tabs` reviews at once (default **1**, same Edge, separate tabs). Starts are staggered by `min_interval_seconds` (default 30) plus jitter.

Each `submit` gets a unique job id and waits only for **that** id. It does not search the queue by MR. GitLab CI variables (`CI_MERGE_REQUEST_IID`, `CI_PROJECT_PATH`, `CI_JOB_ID`, …) are copied onto the job as `meta` and into the filename, e.g. `…-group-app-mr42-…`. `gitlab-post` comments on the MR of the **current** pipeline (`CI_MERGE_REQUEST_IID`), which is the same job that submitted. Results: `queue_dir/results/<job-id>/` (`job.json`, `status.json`, `review.md`). Optional `--label` overrides the slug.

## Do not

- Use a Docker / Kubernetes executor, or isolate the job from the worker’s filesystem.
- Launch `critique-bot --patch-file …` (no `submit`) from CI. Two one-shot processes fight over the Edge profile.
- Run two workers on the same `queue_dir`.
- Point `CRITIQUE_CONFIG` at a different file than the worker.
- Copy `.edge-profile` from another machine (session cookies will not work).

## Quick checks

| Symptom | Check |
| --- | --- |
| `worker is not running` | systemd/status; heartbeat file age; same `queue_dir` as the job |
| Job queued forever | Worker logs; chat UI login expired (`worker --headed`); selectors in `config.json` |
| Review artifact, no MR comments | `CRITIQUE_GITLAB_TOKEN` present, un-protected, scope `api` |
| `No Chromium browser was found` | Edge installed for the runner user |
| Permission denied on queue | Worker user ≠ GitLab runner user |
| Cloudflare / login page | Headless is blocked; re-login with `--headed` |
