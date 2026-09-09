from __future__ import annotations

import argparse
import multiprocessing
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

# Allow `python log_analyzer/analyze.py` without installing the package.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_ROOT_STR = str(_REPO_ROOT)
if _ROOT_STR not in sys.path:
    sys.path.insert(0, _ROOT_STR)
_pythonpath = os.environ.get("PYTHONPATH", "")
if _ROOT_STR not in _pythonpath.split(os.pathsep):
    os.environ["PYTHONPATH"] = (
        _ROOT_STR + (os.pathsep + _pythonpath if _pythonpath else "")
    )

from collections import Counter

from log_analyzer.detect import detect_source, probe_parsers
from log_analyzer.models import FileError, Finding, ScanStats
from log_analyzer.report import render_html
from log_analyzer.scan import (
    DEFAULT_EXTENSIONS,
    iter_source_files,
    normalize_user_path,
    relative_posix,
)


def _analyze_one(payload: tuple[str, str]) -> tuple[str, list[dict], str | None, int]:
    abs_path, relpath = payload
    try:
        source = Path(abs_path).read_bytes()
    except OSError as exc:
        return relpath, [], f"read failed: {exc}", 0
    try:
        findings = detect_source(relpath, source)
        return relpath, [f.to_dict() for f in findings], None, len(source)
    except Exception as exc:  # pragma: no cover - defensive per-file guard
        return relpath, [], f"{type(exc).__name__}: {exc}", len(source)


def _parse_extensions(raw: str) -> set[str]:
    exts: set[str] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.startswith("."):
            part = "." + part
        exts.add(part.lower())
    return exts or set(DEFAULT_EXTENSIONS)


def analyze_path(
    root: Path,
    *,
    jobs: int,
    include_generated: bool,
    extensions: set[str],
) -> tuple[list[Finding], list[FileError], ScanStats]:
    files = iter_source_files(
        root, include_generated=include_generated, extensions=extensions
    )
    stats = ScanStats(root=str(root.resolve()), files_scanned=len(files))
    payloads = [(str(path), relative_posix(root, path)) for path in files]
    findings: list[Finding] = []
    errors: list[FileError] = []
    files_with_hits: set[str] = set()

    worker_count = max(1, jobs)
    if worker_count == 1 or len(payloads) <= 1:
        results = [_analyze_one(item) for item in payloads]
    else:
        results = []
        try:
            ctx = multiprocessing.get_context("spawn")
            with ProcessPoolExecutor(max_workers=worker_count, mp_context=ctx) as pool:
                futures = [pool.submit(_analyze_one, item) for item in payloads]
                for future in as_completed(futures):
                    results.append(future.result())
        except Exception:
            results = [_analyze_one(item) for item in payloads]

    for relpath, raw_findings, error, size in results:
        stats.bytes_scanned += size
        if error:
            errors.append(FileError(file=relpath, error=error))
            stats.parse_failures += 1
        parsed = [Finding(**item) for item in raw_findings]
        if parsed:
            files_with_hits.add(relpath)
            findings.extend(parsed)

    findings.sort(key=lambda item: (-item.chatty_score, item.file, item.line))
    stats.files_with_findings = len(files_with_hits)
    stats.findings = len(findings)
    return findings, errors, stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="logcritique",
        description=(
            "LogCritique — scan Android Java/Kotlin sources for noisy "
            "Log/Timber/println calls and write an HTML report."
        ),
    )
    parser.add_argument(
        "root",
        help=(
            "Android project directory or a single .java/.kt file "
            "(Linux: /home/you/MyApp  PowerShell: C:\\Users\\you\\MyApp)"
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        default="logcritique.html",
        help="HTML report path (default: logcritique.html)",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="Parallel file parsers (default: 1). Use --jobs 8 on large trees.",
    )
    parser.add_argument(
        "--include-generated",
        action="store_true",
        help="Also scan build/, generated/, out/, and .gradle/",
    )
    parser.add_argument(
        "--extensions",
        default=".java,.kt",
        help="Comma-separated extensions (default: .java,.kt)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = normalize_user_path(args.root)
    if not root.exists():
        print(f"error: path not found: {root}", file=sys.stderr)
        print(
            "hint: pass the project folder that contains app/src. "
            r"PowerShell: C:\Users\you\MyApp   Linux: /home/you/MyApp",
            file=sys.stderr,
        )
        return 2
    if not root.is_dir() and not root.is_file():
        print(f"error: not a file or directory: {root}", file=sys.stderr)
        return 2

    findings, errors, stats = analyze_path(
        root,
        jobs=max(1, args.jobs),
        include_generated=args.include_generated,
        extensions=_parse_extensions(args.extensions),
    )
    output = normalize_user_path(args.output)
    render_html(findings, errors, stats, output)

    print(f"scanned {stats.files_scanned} files ({stats.bytes_scanned} bytes)")
    print(f"found {stats.findings} log calls in {stats.files_with_findings} files")
    parsers = Counter()
    contexts = Counter()
    for finding in findings:
        for src in finding.parse_sources:
            parsers[src] += 1
        for tag in finding.contexts:
            contexts[tag] += 1
    print(
        "contexts: "
        f"loop={contexts['loop']} observer={contexts['observer']} "
        f"listener={contexts['listener']} hot_path={contexts['hot_path']}"
    )
    if parsers:
        print("parsers: " + ", ".join(f"{name}={count}" for name, count in parsers.most_common()))
    if findings and not parsers.get("tree-sitter") and not parsers.get("javalang"):
        health = probe_parsers()
        print(
            "warning: tree-sitter/javalang did not tag any calls; "
            "loop/observer/listener tags used brace matching instead.",
            file=sys.stderr,
        )
        for name, note in health.items():
            if note != "ok":
                print(f"  {name}: {note}", file=sys.stderr)
    if stats.files_scanned == 0:
        print(
            "warning: no .java/.kt files found. Pass the project folder "
            "that contains app/src (Linux or Windows path for this machine).",
            file=sys.stderr,
        )
    if errors:
        print(f"parse/read issues: {len(errors)}", file=sys.stderr)
        for err in errors[:8]:
            print(f"  {err.file}: {err.error}", file=sys.stderr)
    print(f"wrote {output.resolve()}")
    sidecar = output.with_suffix(".investigation.json")
    if sidecar.is_file():
        print(f"wrote {sidecar.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
