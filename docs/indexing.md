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

`locate` (fused localization from a bug report, logs and traces), `find_strings`, `find_symbol`,
`symbol_refs`, `who_calls`, `callees`, `imports_of`, `imported_by`, `module_of`, `repo_map`,
`git_blame`, `git_history`, `cochanged`, and `coverage_locate` (runs a failing Python test under
line tracing, or reads a coverage.py JSON, JaCoCo XML or LCOV report, and ranks functions with
Ochiai when passing runs are given). All but `coverage_locate` are read-only; `coverage_locate`
asks like `run_command` when it runs a command.

Call edges are matched by name, so `who_calls` and `callees` say "likely"; `symbol_refs` lists
every textual use when precision matters.

## Benchmarks

`scripts/eval/bench_index.py` measures build time, size, incremental update and query latency;
results are in `plan/results/08/`.
