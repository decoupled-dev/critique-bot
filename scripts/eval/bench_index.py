#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Benchmarks for the code index (plan 8, work item 9) and locate's localization accuracy.

Index a tree (read-only: the database goes to --db, never into the tree) and measure:

- full build time (L0-L3) and the database size against the size of the indexed source;
- an incremental update of 10 changed files (their rows are dropped, so the refresh parses
  them again and re-resolves every import, the "files added" path);
- the git history read (L4) for the first time;
- p50/p95 latency of each index tool over sampled names.

``--localization SUITE`` runs ``locate`` (no model) on every task with a ``fault`` in its
task.json and reports acc@1/acc@5 for the fault file and function, with all retrievers and
with names only (symbols + paths), per variant (original, -obf, -obf-nc).

Usage::

    bench_index.py --repo . --db /tmp/x/index.db --label crit --out plan/results/08
    bench_index.py --repo $AOSP/packages/services/Car --db /tmp/car.db --label car --out plan/results/08
    bench_index.py --localization tests/eval_tasks --out plan/results/08
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from critique_bot import code_index, file_catalog  # noqa: E402
from critique_bot.indexing import engine, history, locate, store, symbols, tools  # noqa: E402


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


def _source_bytes(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COALESCE(SUM(size), 0) FROM files").fetchone()[0])


def bench_repo(repo: Path, db: Path, *, samples: int, seed: int, group: str = "") -> dict:
    """Index ``repo`` (or, with ``group``, one module group of the repo tree at ``repo``, the way
    the tools do on AOSP: no trigram text, no refs table)."""
    repo = repo.resolve()
    index_path = db
    if group:  # where the tools look for the group's database
        db = store.group_db_path(index_path, group)
    db.parent.mkdir(parents=True, exist_ok=True)
    store.remove_db(db)
    file_catalog.forget()
    result: dict = {"repo": str(repo / group) if group else str(repo), "mode": "module group" if group else "project",
                    "treesitter": symbols.treesitter_available("java"), "python": sys.version.split()[0]}
    started = time.monotonic()
    if group:
        engine._build_group(repo, db, group)
        stats = code_index.refresh_index(repo, db)
    else:
        stats = code_index.refresh_index(repo, db)
    result["build_seconds"] = round(time.monotonic() - started, 2)
    conn = store.connect(db)
    counts = {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
              for table in ("files", "symbols", "refs", "edges", "strings", "modules")}
    result["rows"] = counts
    result["files"] = stats.files
    result["source_bytes"] = _source_bytes(conn)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    result["db_bytes"] = db.stat().st_size
    result["db_share_of_source"] = round(result["db_bytes"] / max(1, result["source_bytes"]), 3)

    # incremental: 10 code files changed (dropped from the index, then refreshed)
    conn = store.connect(db)
    rng = random.Random(seed)
    code_paths = [str(r[0]) for r in conn.execute(
        "SELECT path FROM files WHERE language IN ('java','kotlin','python','cpp','c','go','rust','javascript','typescript')")]
    changed = rng.sample(code_paths, min(10, len(code_paths)))
    for rel in changed:
        code_index._forget(conn, rel)
    conn.commit()
    conn.close()
    started = time.monotonic()
    again = code_index.refresh_index(repo, db)
    result["incremental_10_seconds"] = round(time.monotonic() - started, 3)
    result["incremental_changed"] = again.changed
    started = time.monotonic()
    code_index.refresh_index(repo, db)
    result["noop_refresh_seconds"] = round(time.monotonic() - started, 3)

    # history
    conn = store.connect(db)
    started = time.monotonic()
    git_root = repo / group if group else repo
    added = history.update(conn, git_root, group) if (git_root / ".git").exists() else 0
    result["history_seconds"] = round(time.monotonic() - started, 2)
    result["history_commits"] = added
    conn.close()

    result["queries"] = bench_queries(repo, db, samples=samples, seed=seed, index_path=index_path)
    return result


def bench_queries(repo: Path, db: Path, *, samples: int, seed: int, index_path: Path | None = None) -> dict:
    conn = store.connect(db)
    rng = random.Random(seed)
    names = [str(r[0]) for r in conn.execute(
        "SELECT name FROM symbols WHERE kind IN ('function','method') AND length(name) > 4 ORDER BY rowid")]
    classes = [str(r[0]) for r in conn.execute(
        "SELECT name FROM symbols WHERE kind IN ('class','interface') AND length(name) > 4 ORDER BY rowid")]
    paths = [str(r[0]) for r in conn.execute("SELECT path FROM files WHERE language NOT IN ('text','xml') ORDER BY path")]
    messages = [str(r[0]) for r in conn.execute("SELECT value FROM strings WHERE kind IN ('log','error') AND length(value) > 12")]
    conn.close()
    ctx = SimpleNamespace(workspace=repo, index_path=index_path or db, max_chars=6000, state=None)
    pick = lambda items: rng.choice(items) if items else "x"  # noqa: E731
    plans = {
        "find_symbol": lambda: {"name": pick(names)},
        "symbol_refs": lambda: {"name": pick(names)},
        "who_calls": lambda: {"symbol": pick(names)},
        "callees": lambda: {"symbol": pick(names)},
        "imports_of": lambda: {"path": pick(paths)},
        "imported_by": lambda: {"path": pick(paths)},
        "module_of": lambda: {"path": pick(paths)},
        "find_strings": lambda: {"text": pick(messages) + " 42"},
        "repo_map": lambda: {"scope": str(Path(pick(paths)).parent), "budget_tokens": 1000},
        "locate": lambda: {"description": f"the {pick(classes)} fails when {pick(names)} runs: \"{pick(messages)}\""},
        "cochanged": lambda: {"path": pick(paths)},
        "git_history": lambda: {"path": pick(paths)},
    }
    out: dict = {}
    for name, make in plans.items():
        times = []
        for _ in range(samples):
            args = make()
            started = time.monotonic()
            tools.run(name, args, ctx)
            times.append((time.monotonic() - started) * 1000)
        out[name] = {"p50_ms": round(_pct(times, 0.5), 1), "p95_ms": round(_pct(times, 0.95), 1), "n": samples}
    engine.wait_all(60)
    return out


def bench_localization(suite: Path) -> dict:
    rows = []
    logs: list[dict] = []
    for task in sorted(suite.iterdir()):
        spec_path = task / "task.json"
        if not spec_path.is_file():
            continue
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        fault = spec.get("fault")
        if not fault:
            continue
        with tempfile.TemporaryDirectory(prefix="crit-loc-") as tmp:
            work = Path(tmp) / "ws"
            shutil.copytree(task / spec.get("fixture", "files"), work)
            subprocess.run(["git", "init", "-q"], cwd=work, check=False)
            db = Path(tmp) / "index.db"
            file_catalog.forget()
            code_index.refresh_index(work, db)
            conn = store.connect(db)
            for raw in spec["prompt"].splitlines():
                parsed = locate.log_line(raw)
                if not parsed or "AndroidRuntime" in raw:
                    continue
                hits = locate.search_strings(conn, parsed[1], limit=3)
                top = hits[0] if hits else None
                emits = top is not None and (top.value == parsed[1] or locate.matches_format(top.value, parsed[1])
                                             or (len(top.value) >= 8 and parsed[1].startswith(top.value)))
                logs.append({"task": task.name, "line": raw.strip(), "top": f"{top.path}:{top.line}" if top else "",
                             "value": top.value if top else "", "score": top.score if top else 0.0, "emits": emits})
            conn.close()
            row = {"task": task.name, "variant": "obf-nc" if task.name.endswith("-obf-nc") else "obf" if task.name.endswith("-obf") else "original"}
            for label, only in (("fused", None), ("names_only", ["symbols", "paths"])):
                found = locate.locate([db], spec["prompt"], budget=3.0, limit=10, only=only)
                files = [c.path for c in found.candidates]
                file_rank = next((n for n, path in enumerate(files, 1) if path == fault["file"]), None)
                # the function counts as found when the fault file's candidate names it among its top 3
                func_rank = next((n for n, c in enumerate(found.candidates, 1) if c.path == fault["file"]
                                  and fault["function"] in [s.rpartition(".")[2] for s in c.top_symbols(3)]), None)
                row[label] = {"file_rank": file_rank, "function_rank": func_rank, "seconds": found.seconds}
            rows.append(row)
    summary: dict = {}
    for variant in ("original", "obf", "obf-nc"):
        chosen = [row for row in rows if row["variant"] == variant]
        if not chosen:
            continue
        for label in ("fused", "names_only"):
            ranks = [row[label]["file_rank"] for row in chosen]
            funcs = [row[label]["function_rank"] for row in chosen]
            summary[f"{variant}/{label}"] = {
                "tasks": len(chosen),
                "file_acc@1": round(sum(1 for r in ranks if r and r <= 1) / len(chosen), 2),
                "file_acc@5": round(sum(1 for r in ranks if r and r <= 5) / len(chosen), 2),
                "function_acc@5": round(sum(1 for r in funcs if r and r <= 5) / len(chosen), 2),
                "p95_seconds": round(_pct([row[label]["seconds"] for row in chosen], 0.95), 3),
            }
    found = [item for item in logs if item["emits"]]
    summary["log_lines"] = {"lines": len(logs), "emitting_line_top1": round(len(found) / max(1, len(logs)), 2)}
    return {"tasks": rows, "summary": summary, "log_lines": logs}


def _changed_functions(fixture: Path, solution: Path) -> set[tuple[str, str]]:
    """(file, function) pairs the reference solution changes: the ground truth for coverage_locate."""
    import difflib

    from critique_bot.indexing import facts
    from critique_bot.indexing.langs import language_of

    out: set[tuple[str, str]] = set()
    for path in solution.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(solution).as_posix()
        before_path = fixture / rel
        if not before_path.is_file():
            continue
        before = before_path.read_text(encoding="utf-8").splitlines()
        after = path.read_text(encoding="utf-8").splitlines()
        parsed = facts.parse_text("\n".join(before), language_of(rel), rel)
        for tag, a1, a2, _b1, _b2 in difflib.SequenceMatcher(None, before, after).get_opcodes():
            if tag == "equal":
                continue
            for line in range(a1 + 1, max(a1 + 1, a2) + 1):
                inner = [sym for sym in parsed.symbols if sym.line <= line <= sym.end_line and sym.kind not in {"class"}]
                if inner:
                    best = max(inner, key=lambda sym: sym.line)
                    out.add((rel, best.name))
    return out


def bench_coverage(suite: Path) -> dict:
    """coverage_locate on Python bug-fix tasks: is a function the reference fix changes in the top 5?"""
    from critique_bot.indexing import coverage

    rows = []
    for task in sorted(suite.iterdir()):
        spec_path = task / "task.json"
        if not spec_path.is_file():
            continue
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        check = spec.get("check", "")
        if not check.startswith("python") or spec.get("category") != "bugfix" or not spec.get("reference"):
            continue
        solution = task / spec["reference"]
        if not solution.is_dir():
            continue
        truth = _changed_functions(task / spec.get("fixture", "files"), solution)
        with tempfile.TemporaryDirectory(prefix="crit-cov-") as tmp:
            work = Path(tmp) / "ws"
            shutil.copytree(task / spec.get("fixture", "files"), work)
            if spec.get("hidden"):
                shutil.copytree(task / spec["hidden"], work, dirs_exist_ok=True)
            db = Path(tmp) / "index.db"
            file_catalog.forget()
            code_index.refresh_index(work, db)
            started = time.monotonic()
            run = coverage.run_python(check.replace("python3", sys.executable, 1), work, timeout=300)
            passing = []
            for folder in ("tests", "test"):
                if (work / folder).is_dir():  # the project's own tests pass: they sharpen the ranking
                    other = coverage.run_python(f"{sys.executable} -m unittest discover -s {folder} -q", work, timeout=300)
                    if other.exit == 0 and other.lines:
                        passing.append(other.lines)
            visible = sorted(work.glob("test_*.py"))
            if visible and not passing:
                other = coverage.run_python(f"{sys.executable} -m unittest -q " + " ".join(p.stem for p in visible), work, timeout=300)
                if other.exit == 0 and other.lines:
                    passing.append(other.lines)
            conn = store.connect(db)
            ranked = coverage.rank_functions(conn, coverage.ochiai([run.lines], passing), limit=10)
            conn.close()
            rank = next((n for n, item in enumerate(ranked, 1)
                         if (item.path, item.symbol.rpartition(".")[2]) in truth), None)
            rows.append({"task": task.name, "truth": sorted(truth), "rank": rank, "exit": run.exit,
                         "passing_runs": len(passing), "seconds": round(time.monotonic() - started, 2), "top": [f"{i.path}:{i.symbol}" for i in ranked[:5]]})
    hits = [row for row in rows if row["rank"] and row["rank"] <= 5]
    return {"tasks": rows, "top5": round(len(hits) / max(1, len(rows)), 2)}


def _markdown(results: dict) -> str:
    lines = []
    for label, item in results.items():
        if label in {"localization", "coverage"}:
            continue
        lines.append(f"## {label}\n")
        lines.append(f"- tree: `{item['repo']}` ({item['mode']}); python {item['python']}; tree-sitter: {item['treesitter']}")
        lines.append(f"- files indexed: {item['files']}; rows: " + ", ".join(f"{k} {v}" for k, v in item["rows"].items()))
        lines.append(f"- full build (L0-L3): {item['build_seconds']} s")
        lines.append(f"- database: {item['db_bytes'] / 1e6:.1f} MB for {item['source_bytes'] / 1e6:.1f} MB of source "
                     f"({item['db_share_of_source'] * 100:.0f}%)")
        lines.append(f"- incremental update of 10 changed files: {item['incremental_10_seconds']} s; no-op refresh: {item['noop_refresh_seconds']} s")
        lines.append(f"- git history (first read): {item['history_commits']} commits in {item['history_seconds']} s\n")
        lines.append("| tool | p50 ms | p95 ms |\n| --- | --- | --- |")
        for name, q in item["queries"].items():
            lines.append(f"| {name} | {q['p50_ms']} | {q['p95_ms']} |")
        lines.append("")
    cov = results.get("coverage")
    if cov:
        lines.append("## coverage_locate on Python bug-fix tasks (the hidden failing check; the project's own passing tests when present)\n")
        lines.append(f"Fault function (changed by the reference fix) in the top 5: {int(cov['top5'] * 100)}% of {len(cov['tasks'])} tasks.\n")
        for row in cov["tasks"]:
            lines.append(f"- {row['task']}: rank {row['rank']} ({row['seconds']} s, {row['passing_runs']} passing run(s)); fault {row['truth']}; top {row['top'][:3]}")
        lines.append("")
    loc = results.get("localization")
    if loc:
        lines.append("## locate without a model (localization tasks)\n")
        lines.append("| variant / retrievers | tasks | file acc@1 | file acc@5 | function acc@5 | p95 s |\n| --- | --- | --- | --- | --- | --- |")
        for key, item in loc["summary"].items():
            if key == "log_lines":
                continue
            lines.append(f"| {key} | {item['tasks']} | {item['file_acc@1']} | {item['file_acc@5']} | {item['function_acc@5']} | {item['p95_seconds']} |")
        lines.append("")
        logs = loc["summary"].get("log_lines")
        if logs:
            lines.append(f"Log lines quoted in the tasks: {logs['lines']}; the emitting line is find_strings' top hit "
                         f"(its literal prints the message) for {int(logs['emitting_line_top1'] * 100)}%.\n")
            for item in loc["log_lines"]:
                lines.append(f"- `{item['line'][:70]}` -> {item['top']} \"{item['value'][:50]}\" ({item['score']})")
            lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="index benchmarks")
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--label", default="repo")
    parser.add_argument("--group", default="", help="index only this module group of a repo tree (AOSP mode)")
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--seed", type=int, default=8)
    parser.add_argument("--localization", type=Path)
    parser.add_argument("--coverage", type=Path, help="run coverage_locate on the Python bug-fix tasks of a suite")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    results: dict = {}
    if args.repo:
        db = args.db or Path(tempfile.mkdtemp(prefix="crit-bench-")) / "index.db"
        results[args.label] = bench_repo(args.repo, db, samples=args.samples, seed=args.seed, group=args.group)
    if args.localization:
        results["localization"] = bench_localization(args.localization)
    if args.coverage:
        results["coverage"] = bench_coverage(args.coverage)
    text = json.dumps(results, indent=1)
    print(_markdown(results))
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        name = args.label if args.repo else "localization" if args.localization else "coverage"
        (args.out / f"bench-{name}.json").write_text(text + "\n", encoding="utf-8")
        (args.out / f"bench-{name}.md").write_text(_markdown(results) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("GIT_OPTIONAL_LOCKS", "0")
    raise SystemExit(main())
