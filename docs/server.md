# crit serve: the Engine API over HTTP

`crit serve` runs crit's engine as a server for one project. Other programs, and crit
itself in another terminal, talk to it over HTTP: they create and resume sessions, send
tasks, watch every event as it happens, and answer approvals and questions. The same API
runs in process for crit's own front ends (`critique_bot.engine.Engine`).

```
crit serve [--repo-dir DIR] [--port N] [--model NAME] [--listen ADDRESS --insecure]
crit attach [--url URL] [--session ID] [task]
crit run [--agent NAME] [--json] [--mode MODE] [--flow FLOW] [--url URL] "task"
```

## Starting and finding the server

- It listens on **127.0.0.1** and a **free port** unless `--port` says otherwise.
- At start it writes `~/.crit/server.json` (the URL, port, pid, project and version) and a
  **bearer token made for this start** to `~/.crit/server.token`. Both are readable only by
  you (mode 0600). They are removed when the server stops; a server that was killed leaves
  them behind until the next one replaces them. `CRIT_HOME` moves `~/.crit`.
- Every request needs `Authorization: Bearer <token>`; without it the answer is `401`.
- A connection that has not sent an authorized request within 30 seconds is closed, and at
  most 64 connections are served at once (more are closed at once), so a local program that
  opens connections and never finishes them cannot tie the server up. Once the token checks
  out the connection has no timeout, so an event stream stays open while you read it.
- One server serves one project (the folder it was started in, or `--repo-dir`). A session
  for another folder is refused; start another server there. Only one server's address is
  kept in `~/.crit/server.json`: the last one started.

### Other machines

There is no TLS yet, so the token and your code would cross the network in the clear.
A non-loopback address is refused unless you say you know:

```
crit serve --listen 0.0.0.0 --insecure     # a trusted network only
```

Without `--insecure`, `crit serve --listen 0.0.0.0` stops with exit code 2 before it loads
anything. The better way from another machine is an SSH tunnel to the default address
(`ssh -L 8765:127.0.0.1:PORT host`), which keeps the server private.

## The API

The contract is [`api/openapi.yaml`](api/openapi.yaml) (OpenAPI 3.1); `tests/test_server.py`
checks the server against it (every route is in it, every answer has a documented status
and the documented shape).

| Route | What it does |
| --- | --- |
| `GET /v1/health` | the version, the project, how many sessions are open |
| `GET /v1/sessions` | the project's logged sessions, newest first; open ones with their `state` |
| `POST /v1/sessions` | a new session (`mode`, `agent`, `model`, `title`; `message` starts its first task) |
| `GET /v1/sessions/{id}` | one session, with its `pending` requests and `last_seq` |
| `POST /v1/sessions/{id}/fork` | a copy up to `at_seq` (all of it without), opened to continue |
| `POST /v1/sessions/{id}/messages` | `{"text": ...}` starts a task or queues it after the running one. A logged session the server does not have open (it was restarted) is **resumed** first |
| `POST /v1/sessions/{id}/cancel` | stop the running task (it ends as `INTERRUPTED`); open requests are refused |
| `PUT /v1/sessions/{id}/mode` | `ask`, `edits`, `auto` or `plan`; the running task picks it up at its next step |
| `POST /v1/sessions/{id}/undo` | restore what the last task changed (no task running) |
| `GET /v1/sessions/{id}/events?since=N` | the events after seq `N` as server-sent events, then new ones live until the session closes (`follow=0`: only what is there) |
| `POST /v1/sessions/{id}/requests/{rid}` | answer an approval, a question or a plan review |
| `GET /v1/agents` | the file-defined agents ([extending.md](extending.md)) |
| `POST /v1/agents/{name}/runs` | one task headless with that agent as the role; the session closes after it |
| `GET /v1/tools`, `GET /v1/models` | the installed tools (JSON Schema arguments), the model |

Errors are `{"error": {"code": ..., "message": ...}}` with `400` (a bad body), `401`, `404`,
`409` (a request answered already, a task running) or `413` (a body over 1 MiB).

### Events

Events are the session log's ([sessions.md](sessions.md)): each has `seq`, `ts`, `turn` and
`type`. On the stream, `id:` is the seq and `event:` the type, so a client that lost its
connection sends `Last-Event-ID` (or `?since=`) and misses nothing; the stream starts with a
comment line and sends `: keep-alive` every 15 seconds while nothing happens. The ones a
client needs most:

| type | fields |
| --- | --- |
| `notice` | `kind` (`task`, `work`, `note`, `good`, `bad`, `diff`), `text`: what the terminal would show |
| `assistant_text` | `text`: the model's words to the person |
| `tool_started`, `tool_result` | the tool, its arguments, ok, a one-line summary, the result |
| `request` | `id`, `kind` (`permission`, `question`, `plan_review`) and what to show (see below) |
| `request_answered` | `id`, `reply`, `by` (`client`, `unattended`, `cancel`) |
| `task_finished` | `code` (`COMPLETED`, `FAILED`, `BLOCKED`, `INTERRUPTED`, ...), the files changed |
| `session_closed` | the session ended; the stream ends after it |

### Requests

An approval, a question from the model, or a plan review stops the task until someone
answers. Any client may answer; the **first answer counts** and a second one gets `409`.

| kind | the event carries | the reply |
| --- | --- | --- |
| `permission` | `perm_kind` (`edit`, `command`, `outside`, `network`), `summary`, `detail`, `risky`, `options` | `{"decision": "yes" \| "always" \| "no", "reason": "..."}` |
| `question` | `text`, `options` | `{"text": "..."}` |
| `plan_review` | `options` | `{"decision": "auto" \| "edits" \| "ask" \| "no" \| "none", "feedback": "..."}`: a mode carries the plan out, `no` with feedback asks for a revised plan, `none` stops |

A session created with `"unattended": true` (and every agent run, unless the body says
otherwise) answers its own requests at once, the way crit does with no one at the terminal:
approvals are refused, questions get no answer, a plan is not carried out. The request and
the answer are still in the events.

## crit attach

`crit attach` connects this terminal to a session of the server in `~/.crit/server.json`
(or `--url`, which uses the same token file): the newest open session, `--session ID`, or a
new one. It shows the session's new events, asks you the open approvals and questions, and
sends the tasks you type. A session the server no longer has open (the server was killed
and started again) is resumed by the first task you send: crit rebuilds the conversation
from the log, as `crit resume` does.

This is a minimal client: the terminal's own commands (`/mode`, `/role`, `/resume`, ...)
change the terminal, not the remote session, in this version.

## crit run

`crit run --agent NAME --json "task"` runs one task with no one at the terminal and prints
every event as one JSON line on stdout (like `opencode run`), then `session_closed`, then
a final object: `{"type": "final", "code": "COMPLETED", "changed": [...], "evidence": {...}}`,
where `evidence` is the verified flow's checklist and numbers ([verified-changes.md](verified-changes.md);
`null` for a plain task). The exit code is 0 when the task finished `COMPLETED`. Without
`--json` it prints the model's words, the checklist and the outcome. `--flow verified|plain|auto`
picks the flow (in this process only). `--agent` takes a file-defined agent or a role; the agent's
mode applies unless an agent file you have not approved would loosen it (then `ask`), and a
`read-only` agent runs in `ask` with no one to approve. `--mode` overrides it. `--url` runs
the task on a `crit serve` instead of in this process. `crit run` without `--agent`,
`--json` or `--url` is still a task for the agent (`crit run the tests`).

## Limits in this version

- **Sessions share one process.** Each session runs on its own thread with its own UI,
  registry and conversation, but the run counters (`/status`, `agent.json` `stats`) and the
  taint state are per process, and many sessions compete for one local model server.
- **Cancel waits for the model.** A request to the model is not interrupted; the task stops
  at its next step after the reply.
- **TLS** is not built in; see "Other machines".
- The TUI (`crit` in a terminal) runs the engine in process with the terminal as its UI;
  `crit attach` is the client for a server.
