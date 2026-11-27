# Project memory

crit keeps a picture of each project it works in: what the files are for, which files change
together, what past tasks changed and why, and the conventions you approved. At the start of
a task it recalls only what fits that task and adds it to the task's message as a **hunch**:

```
Project memory (hunch; verify before relying on it):
- Likely files: src/hvac/TempHelper.java (0.8), src/hvac/HvacPropertyTest.kt (0.5)
  why: TempHelper.java, HvacPropertyTest.kt: past bugfix e14
- Similar past bugfix (e14, verified, code changed since): cause: display unit read before the config was loaded; fix: defer the unit lookup until onConfigLoaded
- Tried before without success (e9, failed): tried: raising the cache size
- Change recipe (r3, 4x): types.hal -> VehiclePropertyIds.java -> CarPropertyService.java
- Module CarHvacService (soong android_app, Android.bp; 212 files under packages/apps/Hvac) (m2)
  Tests: CarHvacUnitTests
- Convention (n4, approved): Tests in this repo run with `atest CarHvacUnitTests`, not Gradle.
Open a source with the memory tool: {"action": "show", "id": "e12"}.
```

The model is told to treat it as a hint and check it by reading the code. The block is at
most 600 tokens and goes into the task's user message, never into the instructions at the
start of the conversation, so the prompt cache stays valid.

## What is stored

Everything is in `.crit/memory/memory.db` (SQLite) in the project. Like `.crit/sessions/`,
it is in `.gitignore` and readable only by you. Text is redacted before it is written, with
the same redaction as a message to the model. Every item records where it came from (its
**provenance**): a file and its content hash, a commit, a session id, or an import.

| Kind | Id | What | Written |
| --- | --- | --- | --- |
| File card | `f12` | One or two lines: what the file is for, its main symbols, what it logs, who imports it | When a task reads or edits the file, and for the top files of the module it worked in. Rebuilt when the file's hash changes |
| Module card | `m3` | A build module (Soong, Gradle, CMake, Bazel): its build file, entry points, key classes, dependencies, test module | With the file cards of a module |
| Recipe | `r4` | Files that changed together in several commits or sessions, with the commits and their subjects | Mined from the code index's git history after each task (new commits only) |
| Hot spot | `h9` | A file with many fix commits (`fix`, `bug`, `crash`, ... in the subject) | Same |
| Episode | `e7` | A finished task: what was asked, the files changed, symbols, the root cause and fix when the final message states them, the tests that passed after the last edit, the outcome, the session and commit, the hashes of the changed files | At the end of every task of a logged session |
| Convention | `n2` | A project-specific note, with its evidence | Proposed by the agent, approved by you |
| Glossary | `g5` | A badly named symbol and the words that say what it does (`a1` <-> `fahrenheit`) | From episodes and approved conventions |

**Outcomes.** An episode is `verified` when a test or build command passed after the last edit,
`accepted` when you approved its edits, and `completed` otherwise. A task that failed or was
interrupted is kept as a **negative** episode ("tried X, it was not the cause"); recall shows
it as a warning. An episode from a session that read untrusted content (a web page, a file
outside the project, a device log; see [security.md](security.md)) is stored **untrusted** and
is not recalled unless `memory_untrusted` is true.

**Cards** are written without a model by default, from the code index: the file's own doc
comment, its classes and functions, the messages it logs, and the files that import it. With
`"memory_cards": "model"` crit asks the model for each new card in one short extra request,
with the same facts as input; a reply that names none of the file's symbols is replaced by
the plain card.

**Conventions** never enter memory without your approval. The agent may propose two per task,
and each must name a file, symbol, command or value and come with evidence; general advice
("write clean code", "make sure to test") is refused when it is proposed. `crit memory review`
shows the proposals; `crit memory review --stale` checks the approved ones and flags those whose
files, symbols or commands are gone.

## How recall works

1. The task's words, identifiers, quoted text, error messages and file names are the query.
2. Candidates: similar episodes (BM25 full-text search over the task, cause, fix, symbols and
   files; a simple word scorer when SQLite has no FTS5), the files that define the task's
   identifiers or that it names, file cards that match its words, recipes and co-changed files
   of those, module cards, hot spots, and approved conventions.
3. Each file's confidence combines the signals: similarity, how recent the episode is, its
   outcome (verified counts most), co-change strength, and a penalty when the file changed
   since the episode.
4. The best sections that fit the budget go into the hunch block, each with its id.

If more than half of the recent hunches put the wrong file first (at least 6 tasks with edits),
the likely-files line is left out until that improves; `crit memory status` shows the rate.

The `locate` tool also uses memory: file cards and the learned glossary are two of its
retrievers, so it finds badly named code by what its card says or by a glossary entry.

## The memory tool

The model has one `memory` tool with three actions: `search` (a query), `show` (an id, with
its sources, and "code changed since" when the files differ now), and `propose` (a convention
and its evidence, queued for your review). Search and show only read.

## Commands

| Command | What it does |
| --- | --- |
| `crit memory` / `crit memory status` | The setting, the database, counts per kind, the wrong-hunch rate |
| `crit memory show <id>` | One item with every field and its provenance |
| `crit memory list [episodes\|conventions\|cards\|recipes\|hotspots\|glossary]` | The items of one kind |
| `crit memory forget <id>` | Delete one item |
| `crit memory review` | Approve or reject proposed conventions one by one (`--approve n3`, `--reject n4` without asking) |
| `crit memory review --stale` | Check approved conventions; remove or keep the stale ones |
| `crit memory export [-o FILE]` | Episodes, approved conventions, recipes, hot spots and the glossary as JSON. Untrusted episodes stay out unless `--include-untrusted`; cards only with `--cards` |
| `crit memory import FILE` | Add an export from a teammate. Its items keep `import:<file>` as provenance; episodes are stored untrusted unless `--trust`; conventions arrive as proposals, so they still need your approval here |
| `crit memory prune` | Remove negative episodes older than 90 days, episodes whose files are all gone (older than a year), rejected conventions, and cards of changed or deleted files (`--dry-run` to see first) |
| `crit memory mine` | Read new git history into recipes and hot spots now |

## Settings

In `.crit/settings.json`:

| Key | Meaning |
| --- | --- |
| `memory` | `full` (default): recall, write and propose. `recall`: recall and search only, nothing is written. `off`: no memory at all. `--no-memory` sets `off` for one run |
| `memory_budget` | Tokens of the hunch block. Default 600 |
| `memory_untrusted` | `true`: recall episodes from tainted sessions too. Default `false` |
| `memory_cards` | `extractive` (default) or `model` (one short request per new card) |
| `kv_primer` | llama.cpp only: save the KV state of the session's fixed start and restore it next time (see below). Default off |

Memory is written only by sessions that keep an event log (`session_log` on, the default).

## KV primer (llama.cpp)

A llama.cpp server started with `--slot-save-path DIR` can save a slot's KV cache to a file and
load it back (`POST /slots/{id}?action=save` / `restore`). crit can use this for the part of the
conversation that is the same in every session (instructions, tools, project notes): restoring
it costs no prompt processing. The file name is a hash of that text's exact bytes, the model,
its quantization and the context size, so any change gives a new file. `critique_bot.memory.primer`
implements save and restore with that key and falls back to a normal start on any error; the
call at session start is wired when the engine owns the session (Stage 5). vLLM's automatic
prefix caching gives the same effect within one server's lifetime without files.

## Measuring it

`scripts/eval/memory_eval.py` runs the protocol from the plan: warm-up tasks then held-out
tasks in one repository, with memory on and off, and reports turns to the first correct file,
tokens before the first edit, the hunch's acc@5, the wrong-hunch rate and pass@1
([eval.md](eval.md)).
