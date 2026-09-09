# LogCritique

Offline audit of Android Java and Kotlin log calls. Scan a project, then open a single HTML report.

```bash
python3 -m pip install -r requirements.txt
python3 logcritique.py /home/you/MyApp -o logcritique.html
```

Use a Linux path (`/home/...` or `~/MyApp`). Do not pass `C:\...`. Quotes around the path are fine.

Also valid:

```bash
python3 -m log_analyzer /path/to/android-project -o logcritique.html
python3 log_analyzer/analyze.py /path/to/android-project -o logcritique.html
```

The run writes `logcritique.html` and a JSON sidecar next to it (`*.investigation.json`) with source windows and evidence for each tag.

| Flag | Meaning |
| --- | --- |
| `-o`, `--output` | HTML path (default: `logcritique.html`) |
| `--jobs N` | Parallel file parsers (default: 1; use 8 on large trees) |
| `--include-generated` | Also scan `build/`, `generated/`, `out/`, `.gradle/` |
| `--extensions .java,.kt` | File types to include |

`build/`, `.gradle/`, `.idea/`, `generated/`, `out/`, and `.git/` are skipped by default.

## What it flags

- `android.util.Log` — `v`, `d`, `i`, `w`, `e`, `wtf`, `println`
- Timber, including `Timber.tag(...).d(...)`
- `println` / `print` / `System.out` / `System.err`
- Wrappers when both sides match, e.g. `logger.debug`, `LogUtils.e`

`view.d(...)` and similar non-log calls are ignored.

A call is tagged when it is still inside:

- a loop (`for` / `while` / `do` / `forEach` / `repeat`)
- an observer (`observe`, `collect`, `subscribe`, …)
- a listener (`setOnClickListener`, `addTextChangedListener`, …)
- a hot method (`onBindViewHolder`, `onDraw`, `onScrolled`, …)

Logs after the closing brace of those constructs are not tagged.

## Tests

```bash
python3 -m unittest log_analyzer.tests.test_detect
```
