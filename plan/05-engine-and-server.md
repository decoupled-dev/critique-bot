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
- [ ] `agent.py` deleted or under 300 lines of wrappers; no module in `engine/` over 800 lines
- [ ] TUI, ACP, review, queue and `crit run --json` all use the Engine API only
- [ ] `crit serve` + `crit attach` work across processes; the OpenAPI spec matches (contract tests)
- [ ] Suite pass@1 within ±1 point of Stage 4; no new failures in the test suite
- [ ] Layer test passes with the new modules

## Risks
- **Big-bang refactor:** move one piece per commit with tests green; keep wrapper functions
  until every caller has migrated.
- **Concurrency against one local GPU:** many sessions on one server compete for slots.
  The engine keeps a per-server request queue sized to the server's slot count (from
  `/props` or the preset), and a session pins its slot (Stage 3).
