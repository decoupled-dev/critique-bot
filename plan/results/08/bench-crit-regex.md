## crit-regex

- tree: `/home/dc/Abhinav/critique-bot/.claude/worktrees/s8-index` (project); python 3.14.4; tree-sitter: False
- files indexed: 779; rows: files 779, symbols 6094, refs 102155, edges 30452, strings 33941, modules 18
- full build (L0-L3): 3.78 s
- database: 41.3 MB for 4.5 MB of source (920%)
- incremental update of 10 changed files: 0.214 s; no-op refresh: 0.034 s
- git history (first read): 179 commits in 0.06 s

| tool | p50 ms | p95 ms |
| --- | --- | --- |
| find_symbol | 3.4 | 4.0 |
| symbol_refs | 1.6 | 24.6 |
| who_calls | 0.9 | 2.5 |
| callees | 1.1 | 5.2 |
| imports_of | 0.7 | 0.8 |
| imported_by | 0.5 | 0.7 |
| module_of | 0.8 | 1.5 |
| find_strings | 38.7 | 51.0 |
| repo_map | 81.7 | 123.4 |
| locate | 112.1 | 137.5 |
| cochanged | 5.3 | 7.4 |
| git_history | 5.5 | 6.9 |

