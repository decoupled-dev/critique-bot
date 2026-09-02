# Android Log Analyzer

Offline Python tool that scans an Android Java/Kotlin tree for chatty logging calls and writes a single HTML report you can open in a browser.

The package name is **`log_analyzer`** (not `log_nalayzer` or `log_analzyer`).

It is meant for large codebases: thousand-line files and deep source trees. Parsing uses tree-sitter (Java + Kotlin), javalang as a Java second pass, and regex as a fallback when a file does not parse cleanly.

## Install

Python 3.10+ is required. From the repository root:

```bash
pip install -e ./log_analyzer
```

That installs the `log-analyzer` command and the `log_analyzer` module so you can run it from any directory. All packages run locally. No network calls are made while scanning.

If you only want the parser libraries and will launch the script yourself:

```bash
pip install -r log_analyzer/requirements.txt
```

## Usage

After `pip install -e ./log_analyzer` (works from any directory):

```bash
log-analyzer /path/to/android-project --output log-report.html
```

Without installing the package, run the launcher (works from any directory):

```bash
python /path/to/critique-bot/log_analyzer/run.py /path/to/android-project --output log-report.html
```

`python -m log_analyzer` only works when the **repository root** (the folder that contains `log_analyzer/`) is the current directory, or when the package is installed:

```bash
cd /path/to/critique-bot
python -m log_analyzer /path/to/android-project --output log-report.html
```

Do not `cd` into `log_analyzer/` and then run `python -m log_analyzer`. Python will look for a nested `log_analyzer` package and raise `No module named log_analyzer`.

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
python -m unittest log_analyzer.tests.test_detect
```
