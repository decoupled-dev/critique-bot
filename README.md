# LogCritique

Offline audit of Android Java and Kotlin log calls. Scan a project, then open a single HTML report.

## Linux

```bash
python3 -m pip install -r requirements.txt
python3 logcritique.py /home/you/MyApp -o logcritique.html
```

## Windows PowerShell

```powershell
python -m pip install -r requirements.txt
python logcritique.py C:\Users\you\MyApp -o logcritique.html
```

Quotes are fine when the path has spaces:

```powershell
python logcritique.py "C:\Users\you\Android Studio Projects\MyApp" -o logcritique.html
```

`py -3` works if `python` is not on PATH. `~\MyApp` is also accepted.

The run writes `logcritique.html` and a JSON sidecar next to it (`*.investigation.json`).

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
