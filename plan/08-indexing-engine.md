# Stage 8: Indexing engine

## Goal
An index that works from a small repo up to AOSP. Lexical search, a tree-sitter symbol
graph (defines / references / imports / calls / inherits), and git history, all exposed as
tools the agent can call. It updates incrementally and is scoped per module, building on
crit's existing file catalog and code index.

## Why
- Agentic search (grep/glob) is the right default: Claude Code dropped vector RAG; Cline
  refuses to index. Structure on top of it helps most for mid-size open models: LocAgent
  (import/invoke/inherit graph) gave a fine-tuned 32B model 92.7% file-level accuracy at 86%
  lower cost; RepoGraph improved all four agents it was added to; Aider's tree-sitter repo
  map with a 1K-token budget.
- Cursor: semantic search adds +12.5% on average, but only combined with grep, and gains
  grow on repos with 1,000+ files. Embeddings stay optional (Stage 9).
- The Oct 2026 FL study: better localization mostly saves tokens (up to 39.2%) rather than
  raising repair success, and a description of the cause helps more. So localization is a
  tool that reports a hypothesis, not a mandatory pre-pass.
- No AOSP-scale evaluation exists, so we build one (localization tasks in the suite).

## Scope
Storage, lexical layer, symbol graph, git layer, incremental updates, per-module scoping,
tools, and a scoped repo map.

Out of scope: SCIP / compile_commands precise refs, embeddings, shareable prebuilt indexes
(all Stage 9); full LSP (opt-in only, below).

## Design

### Layers
| Layer | What | Built from | Store |
|---|---|---|---|
| L0 catalog | every file: path, size, mtime, hash, language, module | `file_catalog.py` (exists) + Soong/Gradle/CMake module mapping (`soong.py` exists) | SQLite `catalog` |
| L1 lexical | fast substring/regex search | `rg` streaming (exists); optional trigram index for huge trees (zoekt-style, built per module) | trigram postings files |
| L2 symbols | definitions with kind, signature, range, container; references by name | tree-sitter queries per language (`code_index.py` exists; extend) | SQLite `symbols`, `refs` |
| L3 graph | imports, calls (name-resolved, best effort), inherits/implements, module deps | L2 + import resolution per language + build files | SQLite `edges` |
| L4 history | blame per line range, recent commits per file, co-change pairs | `git log --name-only`, `git blame --porcelain` (lazy, cached) | SQLite `cochange`, cache |

One SQLite file per project in `.crit/cache/index.db` (WAL mode). For AOSP, one db per
module group (`frameworks/base`, `packages/services/Car`, …), created on first use, so
nobody indexes 455 GB up front.

### Incremental updates
- Change detection: mtime + size, then hash; `git diff --name-only` since the last indexed commit
  when inside git; a file watcher (inotify / FSEvents / ReadDirectoryChangesW) while a session runs.
- Re-parse only changed files; update edges of the files that reference them by name.
- Index builds run in a background thread with a progress note; tools answer from whatever
  is indexed and say when results are partial.

### Tools (all read-only, output-capped, concise by default)
| Tool | Answers |
|---|---|
| `find_symbol(name, kind?, module?)` | definitions with path:line and signature |
| `symbol_refs(name or path:line, limit)` | references, grouped by file |
| `who_calls(symbol)` / `callees(symbol)` | call edges, depth ≤ 2 |
| `imports_of(path)` / `imported_by(path)` | module/file dependency edges |
| `module_of(path)` | build module (Soong/Gradle/CMake) and its deps |
| `repo_map(scope, budget_tokens)` | Aider-style ranked map of a directory or module, PageRank over L3 edges, personalized to files in the context |
| `git_blame(path, lines)` / `git_history(path or symbol)` / `cochanged(path)` | L4 |
| `locate(description)` | hybrid localization: keywords → lexical hits → graph expansion → ranked files/functions with **a one-line hypothesis per candidate** (one short model call) |

The repo map goes into the prompt only at session start and at compaction (Stage 3), scoped
to the cwd's module, at ≤ 1K tokens by default (profile can raise it).

### Fast finding: fewer round trips, precomputed answers
On a local model a search *turn* (5–20 s of model time) costs far more than the search
itself, so the design minimizes turns:
- **Multi-pattern search:** `search_code(patterns=[...], paths?, kinds?)` runs all patterns in
  parallel over one pass and groups results per pattern. Kinds: `code`, `strings`, `comments`,
  `tests`, `build`. The tool description tells the model to send its variants together.
- **Counts first:** files and hit counts per pattern, then the top snippets, all within the cap.
  Never paged (Stage 2).
- **Index-backed for big trees:** with an index present, `search_code` answers from the
  trigram index (L1) and only verifies candidate files, so AOSP-wide queries take
  milliseconds instead of minutes. Without one it streams `rg` (exists).
- **Parallel read-only calls** in one turn (Stage 1), for example `search_code` +
  `find_symbol` + `module_of` together.

### Finding code with bad names: many signals, fused
Names are one signal among several. Each retriever below returns `(file, line or symbol,
score, reason)`, and `locate` merges them.

| Retriever | Index | Name-independent? |
|---|---|---|
| **String literals and log tags** | L2 extension: every string literal, log tag (`TAG = "…"`, `Log.e(TAG, "…")`, `ALOG*`), error/exception message, UI string and resource id (`strings.xml`, `R.string.*`), config/property key (`ro.*`, `persist.*`, settings keys), with its location | yes: bug reports and logs quote these exactly |
| **Identifier splitting + synonyms** | `camelCase`/`snake_case`/digit splitting, abbreviation expansion (`tmp`/`temp`→temperature, `fmt`→format, `mgr`→manager, `cfg`→config, `ctx`→context, `btn`→button, …), a built-in glossary plus a **project glossary** learned in Stage 8b ("zone = seat area"), and stemming | partly |
| **Symbols and graph** | L2/L3: definitions, references, callers from known entry points (AIDL/HAL methods, `main`, Activities/Services, request handlers, tests) | yes, structure only |
| **Runtime evidence** | `coverage_locate(test or command)`: run a failing test or a repro under coverage (coverage.py, JaCoCo, gcov/llvm-cov when configured) and rank the executed functions, spectrum-based (Ochiai) when passing tests are also given. Stack frames from a given trace are parsed and resolved to symbols | yes |
| **Commit history** | L4: commit messages, `git log -G` on changed lines, blame of candidate regions; plain-language messages describe what badly named code does | yes |
| **File and module cards** (when Stage 8b is present) | full-text search over the plain-language cards ("converts HVAC temperature units for display") | yes |
| **Tests** | test names and assertions mapped to the code they exercise (imports + calls) | partly |
| **Embeddings** (optional, Stage 9.5) | symbol-level chunks | yes |

`locate(description, evidence?)`:
1. Extract identifiers, quoted strings, error text, stack frames and file hints from the
   description and the evidence (logs, traces, test names).
2. Query every available retriever in parallel; each gets a time budget (default 1.5 s total)
   and returns its top 20.
3. Merge with **reciprocal rank fusion**, with weights per retriever (exact string and stack
   frame matches rank highest; runtime evidence next; names last), tuned on the localization set.
4. Return the top 10 symbols/files, each with the retrievers that found it ("log message
   match + executed by the failing test + 2 commits mention 'fahrenheit'") and one short
   hypothesis line (one helper call, as before).
5. Results feed Stage 8b: when the agent confirms what a badly named symbol does, its card
   and the project glossary are updated, so the next search finds it by meaning.

### LSP (opt-in)
`settings.lsp = {"kotlin": "kotlin-language-server", ...}`. If configured, diagnostics for
changed files can feed the Stage 2 guard, and `find_symbol`/`symbol_refs` can use LSP results
when present. Off by default; never for AOSP-wide use.

## Work items
1. Schema + migrations for `index.db`; move `code_index.py` storage onto it.
2. L0: unify `file_catalog.py` with module mapping (Soong `Android.bp`, Gradle, CMake, Bazel `BUILD`).
3. L2: tree-sitter queries for Python, Java, Kotlin, C, C++, Go, Rust, TS/JS, AIDL, HIDL;
   signatures and containers.
4. L3: import resolution per language; call edges (name-based with container hints); inherits.
5. L4: blame/log/co-change with a cache.
6. Incremental: change detection, watcher, partial-result notes.
7. Tools + descriptions (Stage 2 checklist); `repo_map` with ranking; `locate`.
7a. Multi-pattern `search_code` with kinds, counts-first output, and the index-backed path.
7b. String-literal / log-tag / resource / property-key index per language (Java, Kotlin, C/C++,
    Python, XML resources, `.rc` and `.prop` files, SELinux `.te`).
7c. Identifier splitting, built-in abbreviation glossary, project glossary table (filled by
    Stage 8b), stemming; applied to symbol and card search.
7d. `coverage_locate` (coverage.py, JaCoCo, llvm-cov/gcov when configured; Ochiai scoring with
    passing tests) and stack-trace resolution (Java, Kotlin, native tombstone frames after
    symbolization).
7e. Commit-message and `log -G` retriever; test-to-code mapping.
7f. `locate` with parallel retrievers, time budgets, reciprocal rank fusion and per-retriever
    weights tuned on the localization set.
8. Impact list (Stage 7) moves from grep to the graph.
9. Benchmarks: index time and size for (a) crit itself, (b) a 1M-line Kotlin/Java app, (c)
   AOSP `frameworks/base`, (d) `packages/services/Car`; query latency p50/p95.
10. Localization eval: 20 tasks (10 AOSP, 10 general) with a known fault file/function;
    measure acc@1/acc@5 with and without index tools. Record in `plan/results/08/`.
11. **Bad-naming test set:** `scripts/eval/obfuscate.py` copies each localization fixture and
    renames identifiers (classes, methods, fields, locals) to meaningless names (`a1`, `Mgr2`,
    `doIt`) with a language-aware rename, keeping behavior, strings, comments-optional and
    tests runnable. Each task runs on the original and the obfuscated copy (with and
    without comments). Report acc@5, search turns and seconds to the first correct candidate.

## Tests
- Golden-file tests per language: fixture sources → expected symbols/edges.
- Incremental: modify/rename/delete files → the index matches a full rebuild.
- Partial index: tools label partial results.
- Large-repo smoke (manual or nightly with `$AOSP`).

## Completion targets
- [ ] `frameworks/base` indexed (L0–L3) in ≤ 15 min on the dev machine; incremental update of 10
      changed files ≤ 3 s; query p95 ≤ 300 ms
- [ ] Index db ≤ 10% of the source size for that module
- [ ] Localization acc@5 ≥ 80% on the 20-task set with index tools (baseline: without)
- [ ] Tokens per solved task −20% on the suite's navigation-heavy tasks, at equal pass@1
- [ ] `repo_map` stays within its budget and is cache-stable within a session
- [ ] Works without the `index` extra installed (lexical only, with a clear note)
- [ ] **Speed:** median search turns before the first correct file ≤ 2 on the localization set;
      `locate` returns within 2 s p95 on `frameworks/base`; AOSP-wide `search_code` with an index ≤ 1 s p95
- [ ] **Bad names:** on the obfuscated copies, acc@5 drops by ≤ 10 points against the original
      names (comments kept) and ≤ 20 points (comments stripped); without the fused retrievers
      (names only) the drop is measured for comparison
- [ ] String/log-tag retriever finds the emitting line for ≥ 95% of log messages taken from the
      log-crash eval tasks
- [ ] `coverage_locate` puts the fault function in its top 5 for ≥ 70% of tasks that have a
      failing test

## Risks
- **Name-based call edges are imprecise:** label them "likely" and keep `symbol_refs` as the
  fallback; precise refs come with SCIP in Stage 9.
- **Tree-sitter grammar packaging on Windows and in the .deb:** pin versions; CI builds the
  wheels into the release bundle.
- **Coverage runs are slow or unavailable** (AOSP native code, device-only tests):
  `coverage_locate` is an optional tool with a time budget; stack traces and logs carry most of
  the runtime signal there.
- **Synonym expansion adds noise:** expanded terms get a lower weight than exact terms in fusion,
  and the weights are tuned on the eval set, not guessed.
