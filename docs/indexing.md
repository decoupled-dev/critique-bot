# The code index

crit keeps an index of each project in `.crit/cache/index.db` (SQLite, WAL mode). It is a
cache: delete it and crit builds it again. Everything works with the standard library;
`pip install -e .[index]` adds tree-sitter grammars, which make definitions and call edges
more exact for Java, Kotlin, Go, Rust, C, C++, C#, JavaScript and TypeScript (Python always
uses the standard `ast` module; AIDL and HIDL use a regex pass).

## What is in it

| Layer | Contents |
| --- | --- |
| L0 catalog | every file: path, size, mtime, content hash, language, owning build file; build modules from `Android.bp` (Soong), Gradle, CMake and Bazel with their deps |
| L2 symbols | definitions (kind, signature, line range, container), identifier uses per file, and strings: literals, log tags and messages, error messages, Android string resources, property keys (`ro.*`, `persist.*` in code, `.rc`, `.prop`, sysprop, `property_contexts`), SELinux types and rules |
| L3 graph | imports resolved to files per language, call edges by name ("likely"), inherits/implements |
| L4 history | commits and the files they changed, co-change pairs, commit messages (full-text), cached blame |

The index is refreshed at the start of each task and after each edit. Only changed files are
parsed again: a file whose mtime or size changed is re-hashed, and only new content is parsed.
After files are added or removed, imports are resolved again so the result equals a full rebuild.
History is read incrementally from the last indexed commit.

## AOSP and other `repo` trees

On a `repo` checkout nothing is indexed up front. The first index tool that names a path builds
a database for that path's git project (for example `packages/services/Car`) in
`.crit/cache/index/`, in the background. Until it is done, tools answer from what is indexed
and their output starts with `partial:`.

## Tools

Five tools; related lookups share one tool with an `action`, so a small model picks among few:

| Tool | Actions / what it answers |
| --- | --- |
| `locate` | fused localization from a bug report, log lines and traces: ranked files and functions with the evidence for each |
| `find_strings` | the code that prints a log line, error, UI string, tag, resource or property key |
| `code_nav` | `find_symbol`, `refs`, `who_calls`, `callees`, `imports_of`, `imported_by`, `module_of`, `repo_map` |
| `code_history` | `blame`, `log` (by path, function `name`, commit-message `query`, or `changed` for `git log -G`), `cochanged` |
| `coverage_locate` | runs a failing Python test under line tracing, or reads a coverage.py JSON, JaCoCo XML or LCOV report, and ranks functions (Ochiai with passing runs); resolves stack traces |

All but `coverage_locate` are read-only; `coverage_locate` asks like `run_command` when it runs a
command. The names of the first Stage 8 build (`find_symbol`, `symbol_refs`, `who_calls`,
`callees`, `imports_of`, `imported_by`, `module_of`, `repo_map`, `git_blame`, `git_history`,
`cochanged`) still work when a model calls them: they run as `code_nav` / `code_history` with
the matching action, but they are not advertised.

With [project memory](memory.md), `locate` also searches the plain-language file cards and the
learned project glossary (a badly named symbol and the words that say what it does), so code is
found by what it does even when its names say nothing. Memory adds those entries to the index's
`glossary` table too (source `learned`), so the identifier expansion of every search uses them.

Call edges are matched by name, so `who_calls` and `callees` say "likely"; `refs` lists every
textual use when precision matters.

## Benchmarks

`scripts/eval/bench_index.py` measures build time, size, incremental update and query latency;
results are in `plan/results/08/`.
