# Android Log Analyzer

Offline Python tool that scans an Android Java/Kotlin tree for chatty logging calls and writes a single HTML report you can open in a browser.

The package name is **`log_analyzer`** (not `log_nalayzer` or `log_analzyer`).

## Run it (no editable install)

`pip install -e .` from this repository root installs **critique-bot**, not the analyzer. That is why dependency building takes a long time and then fails. You do not need that.

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python3 -m pip install -r log_analyzer/requirements.txt
python3 run_log_analyzer.py /path/to/android-project -o log-report.html
```

These also work after the same `requirements.txt` install:

```bash
python3 log_analyzer/analyze.py /path/to/android-project -o log-report.html
python3 log_analyzer/run.py /path/to/android-project -o log-report.html
```

Open `log-report.html` in a browser.

Useful flags:

| Flag | Meaning |
| --- | --- |
| `-o`, `--output` | HTML path (default: `log-report.html`) |
| `--jobs N` | Parallel file parsers (default: CPU count) |
| `--include-generated` | Also scan `build/`, `generated/`, `out/`, `.gradle/` |
| `--extensions .java,.kt` | File types to include |

`build/`, `.gradle/`, `.idea/`, `generated/`, `out/`, and `.git/` are skipped by default.

## What it detects

Only calls that can be identified accurately:

- `android.util.Log` — `v`, `d`, `i`, `w`, `e`, `wtf`, `println`
- Timber — `Timber.v/d/i/w/e/wtf` and `Timber.tag(...).d(...)`
- `println` / `print` / `System.out.println` / `System.err.print(ln)`
- Wrappers when **both** sides match: method in `{v,d,i,w,e,wtf,verbose,debug,info,warn,warning,error}` and the receiver name looks like `log` / `logger` / `timber` (for example `logger.d`, `LogUtils.e`)

Unrelated methods such as `view.d(...)` are ignored.

Each hit is tagged when it sits in a high-frequency place:

- loops (`for` / `while` / `do` / `forEach` / `repeat`)
- observers (`observe`, `collect`, `subscribe`, …)
- listeners (`setOnClickListener`, `addTextChangedListener`, `*Listener`, …)
- hot methods (`onBindViewHolder`, `onDraw`, `onScrolled`, `onTouchEvent`, …)

A chatty score ranks items so the noisiest calls surface first. `Log.e` inside a loop is still flagged because it can flood logcat.

## HTML report

The report is one self-contained file (no CDN). Use the file tree, level chips, and context filters to jump around. Click a row for the snippet, enclosing function, parser source (`tree-sitter`, `javalang`, `regex`), and a short “why noisy” note.

Keyboard: `/` focuses search, `Esc` closes a detail row.

## Tests

```bash
python3 -m unittest log_analyzer.tests.test_detect
```
