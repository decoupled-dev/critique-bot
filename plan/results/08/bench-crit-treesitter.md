## crit-treesitter

- tree: `/home/dc/Abhinav/critique-bot/.claude/worktrees/s8-index` (project); python 3.12.3; tree-sitter: True
- files indexed: 779; rows: files 779, symbols 6204, refs 102104, edges 30261, strings 33941, modules 18
- full build (L0-L3): 3.95 s
- database: 41.2 MB for 4.5 MB of source (919%)
- incremental update of 10 changed files: 0.172 s; no-op refresh: 0.031 s
- git history (first read): 179 commits in 0.06 s

| tool | p50 ms | p95 ms |
| --- | --- | --- |
| find_symbol | 2.2 | 3.2 |
| symbol_refs | 1.1 | 17.4 |
| who_calls | 0.8 | 2.5 |
| callees | 1.0 | 2.6 |
| imports_of | 0.5 | 0.6 |
| imported_by | 0.5 | 0.6 |
| module_of | 0.6 | 1.3 |
| find_strings | 26.3 | 35.8 |
| repo_map | 56.5 | 85.7 |
| locate | 115.6 | 143.2 |
| cochanged | 4.8 | 7.1 |
| git_history | 4.6 | 7.0 |

