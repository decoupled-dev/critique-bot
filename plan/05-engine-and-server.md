# Stage 5: Engine core and server

## Goal
Split the 4,100-line `agent.py` into a small engine with one public API. Put an HTTP/SSE
server in front of it. Make every front end a client of that API: the TUI, ACP, the review
app, the job queue, headless agents, and later a web UI.

## Why
- opencode's client/server split (an OpenAPI 3.1 spec, terminal, desktop and web clients)
  is the structural feature crit lacks. It is needed for headless agents, IDEs and remote
  use, even if it doesn't change scores.
- The OpenHands SDK redesign (event-sourced state, portable runtimes) reports fewer
  system-attributable failures.
- Stage 1 leaves two paths in the engine (text and turns). This stage deletes the old one.

## Scope
Engine package layout, the Engine API, the server, the client adapters, and removing the
legacy loop.

Out of scope: a web UI client (a minimal one is optional at the end; the full one is Stage 9+).

## Design

### Package layout (engine layer)
```
critique_bot/engine/
  __init__.py      Engine, SessionHandle (public API)
  loop.py          the turn loop: assemble → complete → dispatch tools → record
  dispatch.py      permission check, sandbox run, parallel read-only calls, breaker
  modes.py         ask / plan / auto as permission profiles (plan = read-only + plan file write + approval exit)
  compaction.py    (from Stage 3)
  prompt_build.py  (from Stage 3)
  session_log.py   (from Stage 3)
  contracts.py     AnswerContract, read-only tasks
  delegate.py      sub-agent runs (fresh context, summary return, worktree for writers)
  stop_rules.py    budget, no-progress, finish detection
```
`agent.py` shrinks to compatibility wrappers (`run_agent_loop`, `run_agent`) that call the
Engine, then gets deleted once every caller has moved.

### Engine API (in-process)
```python
class Engine:
    def __init__(self, config, *, registry=None, ui=None): ...
    def create_session(self, cwd, *, agent="default", model=None, mode="ask") -> SessionHandle
    def open_session(self, session_id) -> SessionHandle          # resume
    def fork_session(self, session_id, at_seq=None) -> SessionHandle
    def list_sessions(self, cwd=None) -> list[SessionInfo]

class SessionHandle:
    id: str
    def send(self, text, *, attachments=()) -> None              # starts or continues a task
    def events(self, since_seq=0) -> Iterator[Event]               # live stream
    def answer(self, request_id, reply) -> None                    # approvals, questions, plan review
    def cancel(self) -> None
    def set_mode(self, mode) -> None
    def undo(self, checkpoint=None) -> None
```
Approvals and questions become **request events** with ids (`permission_request`,
`question`, `plan_review`). A client answers with `answer()`. `ui_api.NullUI` maps onto
this for in-process front ends during the transition.

### Server (`server.py`, apps layer; `crit serve`)
- HTTP on 127.0.0.1 by default, a random port written to `~/.crit/server.json`; a bearer token
  generated per start (file mode 0600). A non-loopback bind needs `--listen` + TLS or an
  explicit `--insecure` acknowledgment.
- REST + SSE, with the OpenAPI spec committed at `docs/api/openapi.yaml` and generated tests
  checking the server against it:
  - `POST /v1/sessions`, `GET /v1/sessions`, `GET /v1/sessions/{id}`, `POST /v1/sessions/{id}/fork`
  - `POST /v1/sessions/{id}/messages`, `POST /v1/sessions/{id}/cancel`, `PUT /v1/sessions/{id}/mode`
  - `GET /v1/sessions/{id}/events?since=N` (SSE)
  - `POST /v1/sessions/{id}/requests/{rid}` (answer)
  - `GET /v1/agents`, `GET /v1/tools`, `GET /v1/models`, `GET /v1/health`
- Implementation: stdlib `asyncio` + a small HTTP layer, or `starlette` + `uvicorn` as an
  optional extra (`pip install crit[server]`). Decide by dependency weight; record in `decisions.md`.
- One engine process can host many sessions; each session runs in a worker thread; tool
  processes run under the Stage 4 sandbox.

### Clients
| Client | Change |
|---|---|
| TUI (`agent_ui.py`) | in-process `Engine` by default; `crit attach [--url]` connects to a server |
| ACP (`agent_acp.py`) | maps ACP JSON-RPC onto `SessionHandle` (sessions, prompts, permission requests) |
| Review (`review_agent.py`, `review_session.py`) | `create_session(agent="code-reviewer", mode="read-only")` + `AnswerContract` |
| Queue / worker (`queue.py`, `worker.py`) | jobs become sessions; the worker can be a server client, so CI can submit remotely |
| Headless | `crit run --agent X --json "task"` streams events as JSONL on stdout, so it's scriptable (like `opencode run`) |

## Work items
1. Create `engine/`; move code out of `agent.py` piece by piece with tests green after each
   move (prompt_build, compaction, dispatch, modes, stop rules, delegate, loop).
2. Event-based requests for approvals, questions and plan review; adapt `ui_api`.
3. Engine API + `SessionHandle`; in-process TUI on it.
4. Delete the legacy text-reply loop (prompted tool mode lives in the provider layer since Stage 1).
5. Server + OpenAPI spec + auth token + `crit serve` / `crit attach`.
6. ACP, review, queue/worker, `crit run --json` on the Engine API.
7. Layer table updates; `docs/architecture.md` rewrite with the API.
8. Eval: no regression; record in `plan/results/05/`.

## Tests
- Every existing test green after each move (the refactor goes in small commits).
- API contract tests generated from the OpenAPI spec.
- Two clients attached to one session: both see the same events; only one answer per request is accepted.
- Server killed mid-task → `crit attach` + resume works (via Stage 3).
- Security: no token → 401; a non-loopback bind without a flag → refuses to start.

## Completion targets
- [x] `agent.py` deleted or under 300 lines of wrappers; no module in `engine/` over 800 lines
  (agent.py: 90 lines of re-exports; largest engine module `delegate.py`, 776)
- [ ] TUI, ACP, review, queue and `crit run --json` all use the Engine API only
  (`crit run --json`, `crit attach` and agent runs: yes. TUI, ACP and review use the engine
  package's interface (`engine.run_agent`, `engine.run_read_only_task`), not `SessionHandle`;
  the queue worker is unchanged. See Status.)
- [x] `crit serve` + `crit attach` work across processes; the OpenAPI spec matches (contract tests)
- [ ] Suite pass@1 within ±1 point of Stage 4; no new failures in the test suite
  (no new failures: 1,938 tests OK; the pass@1 eval needs the model machines)
- [x] Layer test passes with the new modules

## Risks
- **Big-bang refactor:** move one piece per commit with tests green; keep wrapper functions
  until every caller has migrated.
- **Concurrency against one local GPU:** many sessions on one server compete for slots.
  The engine keeps a per-server request queue sized to the server's slot count (from
  `/props` or the preset), and a session pins its slot (Stage 3).

## Status (2026-11-27)

Done:
- **Split** (work items 1, 7): `agent.py` became `critique_bot/engine/` in nine commits, the
  full suite green after each: `parse`, `json_repair`, `replies`, `text`, `prompts`, `ui`,
  `runtime`, `chat`, `context`, `task` with the `dispatch` / `modes` / `stop_rules` mixins,
  `contracts`, `delegate` (was `agent_helpers`), `loop`, `setup`, `runs`, then `api`. Every
  module is under 800 lines. `session_log`, `compaction` and `prompt_build` stay sibling
  engine modules (decisions.md). `agent.py` is 90 lines of compatibility names; apps may not
  import it (`INTERFACES[ENGINE]` = `engine`, `ui_api`, `session_log`). Tests that patched
  `agent._approve_prompt`, `_sleep`, `_execute`, ... patch `engine.ui`, `engine.chat`,
  `engine.dispatch`, ... now.
- **Engine API** (items 2, 3): `Engine` (create / open / fork / list sessions) and
  `SessionHandle` (`send`, `events(since_seq)` live, `answer`, `cancel`, `set_mode`, `undo`,
  `close`), `EventUI` (approvals, questions and plan review are `request` events with ids;
  one answer per request), `drive_ui` (a NullUI-style front end on a session). Each session
  runs on its own thread with its own UI (`ui.use_thread_ui`), registry (`ToolContext.registry`),
  native tools and extensions (`runtime`).
- **History from the engine** (Stage 3's leftover): `_Chat.messages` is the conversation of a
  model API session, installed before every request; compaction, resume and new chats
  replace it through the engine. The web chat's conversation stays on its page.
- **Server** (item 5): `crit serve` (stdlib `ThreadingHTTPServer` + SSE; decisions.md),
  127.0.0.1 and a random port in `~/.crit/server.json`, a per-start token in
  `~/.crit/server.token` (0600), 401 without it, `--listen` + `--insecure` for another
  address. `docs/api/openapi.yaml` + `tests/test_server.py` (routes = spec, every operation's
  status and schema, 401 everywhere, 0600 files, the bind refusal, two clients / one answer,
  SIGKILL mid-approval then `crit attach` resumes on a new server).
- **Clients** (item 6): `crit attach [--url] [--session]`, `crit run [--agent] [--json]
  [--url]`, `POST /v1/agents/{name}/runs` (Stage 6's hook), ACP `session/load`
  (`loadSession: true`; the ACP session id is the log id).
- **Leftovers from Stages 1-3**: profile `tool_output_cap_tokens`, `shell_timeout_s`,
  `edit_format` and the new `masking_penalty` field become loop settings; `apply_patch` only
  for `edit_format` `apply_patch` / `patch`; `tool_api.environment_snapshot` (with the running
  background jobs) in the first user turn; background jobs a task started are stopped when
  it ends (`keep_background_jobs`).

Left:
- **TUI on `SessionHandle`.** The terminal runs `engine.run_agent` in process with
  `agent_ui` as the process UI. Moving it onto `SessionHandle` + `drive_ui` needs the
  terminal's own commands (`/mode`, `/role`, `/shell`, `/resume`, `/fork`, Shift+Tab) turned
  into Engine calls (`set_mode`, a role change and a shell change on the handle, open/fork)
  instead of the `take_*` hooks the loop polls. `crit attach` has the same gap (its slash
  commands change only the local terminal).
- **ACP on `SessionHandle`.** ACP has `session/load` but still runs as a UI module of one
  `run_agent`. The port maps `session/new` to `create_session`, prompts to `send`, the
  events to `session/update` (tool calls from `tool_started` / `tool_result`, live command
  output from the handle's live-only `tool_output`), and `request` events to
  `session/request_permission`.
- **Review and queue as sessions.** The agent review uses `engine.run_read_only_task` (a
  read-only run with `AnswerContract`; it asks nothing, so it needs no request events). The
  queue worker still runs one-shot reviews; CI can use `crit serve` with
  `crit run --url ... --agent code-reviewer` meanwhile.
- **Work item 4** (delete the text-reply loop) is not done, by decision: the web chat stays
  as a removable module and speaks the text formats (decisions.md, 2026-11-27).
- **Concurrency limits**: run counters and taint are per process; cancel waits for the model's
  reply; no per-server request queue sized to the model server's slots yet.
- **Item 8** (eval: no regression, `plan/results/05/`) needs the model machines.
- TLS for `crit serve` (`--listen` without `--insecure`).
