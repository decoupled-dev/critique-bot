from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from log_analyzer.analyze import analyze_path, main
from log_analyzer.detect import detect_source
from log_analyzer.models import Finding

REPO_ROOT = Path(__file__).resolve().parents[2]

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name: str) -> tuple[str, bytes]:
    path = FIXTURES / name
    return name, path.read_bytes()


def by_method(findings: list[Finding], filename: str) -> list[Finding]:
    return [f for f in findings if f.file.endswith(filename) or f.file == filename]


class DetectTests(unittest.TestCase):
    def test_log_d_in_for_is_loop(self) -> None:
        rel, source = load("LoopLogs.java")
        findings = detect_source(rel, source)
        debugs = [f for f in findings if f.method == "d" and "loop" in f.contexts]
        self.assertTrue(debugs, findings)
        self.assertEqual(debugs[0].api, "android.util.Log")
        self.assertGreaterEqual(debugs[0].chatty_score, 15)
        self.assertIn("tree-sitter", debugs[0].parse_sources)
        warns = [f for f in findings if f.method == "w"]
        self.assertTrue(warns)
        self.assertNotIn("x));", warns[0].snippet)

    def test_logs_after_loops_are_not_loops(self) -> None:
        rel, source = load("NotInLoop.java")
        findings = detect_source(rel, source)
        after = [f for f in findings if "after" in f.snippet or f.snippet.startswith("Log.i(\"T\", \"before\")")]
        self.assertTrue(after, findings)
        for finding in after:
            self.assertNotIn("loop", finding.contexts, finding)
        inside = [f for f in findings if "inside for" in f.snippet]
        self.assertTrue(inside)
        self.assertIn("loop", inside[0].contexts)
        self.assertTrue(inside[0].source_window)
        self.assertTrue(any("for_statement" in r or "enhanced_for" in r for r in inside[0].context_reasons))

    def test_kotlin_logs_after_loops_are_not_loops(self) -> None:
        rel, source = load("NotInLoop.kt")
        findings = detect_source(rel, source)
        for finding in findings:
            if "inside for" in finding.snippet:
                self.assertIn("loop", finding.contexts, finding)
            else:
                self.assertNotIn("loop", finding.contexts, finding.snippet)

    def test_foreach_and_while_logs(self) -> None:
        rel, source = load("LoopLogs.java")
        findings = detect_source(rel, source)
        self.assertTrue(any(f.method == "v" and "loop" in f.contexts for f in findings))
        self.assertTrue(any(f.method == "w" and "loop" in f.contexts for f in findings))

    def test_timber_in_observe(self) -> None:
        rel, source = load("Observe.kt")
        findings = detect_source(rel, source)
        observed = [
            f
            for f in findings
            if f.api == "Timber" and f.method == "d" and "observer" in f.contexts
        ]
        self.assertTrue(observed, findings)
        self.assertTrue(any(f.method == "v" and "observer" in f.contexts for f in findings))

    def test_println_in_on_bind_view_holder(self) -> None:
        rel, source = load("ItemAdapter.java")
        findings = detect_source(rel, source)
        prints = [f for f in findings if f.level in {"println", "print"}]
        self.assertTrue(prints, findings)
        self.assertEqual(prints[0].enclosing_function, "onBindViewHolder")
        self.assertIn("hot_path", prints[0].contexts)
        self.assertGreaterEqual(prints[0].chatty_score, 24)
        errors = [f for f in findings if f.level == "e"]
        self.assertTrue(any("loop" in f.contexts and "hot_path" in f.contexts for f in errors))

    def test_listener_lambda(self) -> None:
        rel, source = load("Listener.kt")
        findings = detect_source(rel, source)
        self.assertTrue(any("listener" in f.contexts and f.method == "d" for f in findings), findings)
        self.assertTrue(any("listener" in f.contexts and f.method == "i" for f in findings))
        clicks = [f for f in findings if f.method == "println"]
        self.assertTrue(clicks)
        self.assertEqual(clicks[0].enclosing_function, "onClick")

    def test_false_positives_skipped(self) -> None:
        rel, source = load("FalsePositive.java")
        findings = detect_source(rel, source)
        snippets = " ".join(f.snippet for f in findings)
        self.assertFalse(any(f.receiver == "view" for f in findings), findings)
        self.assertFalse(any(f.receiver == "drawer" for f in findings), findings)
        self.assertNotIn("string literal", snippets)
        self.assertNotIn("commented out", snippets)
        wrappers = [f for f in findings if f.api == "wrapper"]
        self.assertTrue(any(f.receiver == "LogUtils" and f.method == "e" for f in wrappers), findings)
        self.assertTrue(any(f.receiver == "logger" and f.method == "debug" for f in wrappers), findings)

    def test_broken_syntax_still_finds_logs(self) -> None:
        rel, source = load("Broken.java")
        findings = detect_source(rel, source)
        self.assertTrue(any(f.level == "e" for f in findings), findings)
        self.assertTrue(any(f.api in {"Timber", "android.util.Log"} and f.level == "w" for f in findings) or any(f.method == "w" for f in findings), findings)

    def test_scan_fixtures_and_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.html"
            findings, errors, stats = analyze_path(
                FIXTURES,
                jobs=1,
                include_generated=False,
                extensions={".java", ".kt"},
            )
            self.assertGreaterEqual(stats.files_scanned, 6)
            self.assertGreater(len(findings), 10)
            from log_analyzer.report import render_html

            render_html(findings, errors, stats, out)
            html = out.read_text(encoding="utf-8")
            self.assertIn("Android Log Analyzer", html)
            self.assertIn("LoopLogs.java", html)
            self.assertIn("Files by log count", html)
            self.assertIn("Filters", html)
            self.assertIn("Copy investigation JSON for AI", html)
            self.assertNotIn("<<<LOG_ANALYZER_JSON>>>", html)
            start = html.find(">", html.find("log-analyzer-data")) + 1
            end = html.find("</script>", start)
            payload = json.loads(html[start:end])
            counts = [item["count"] for item in payload["files"]]
            self.assertEqual(counts, sorted(counts, reverse=True))
            self.assertGreaterEqual(counts[0], counts[-1])
            self.assertIn("ai_guide", payload)
            self.assertTrue(payload["findings"][0]["source_window"])
            sidecar = out.with_suffix(".investigation.json")
            self.assertTrue(sidecar.is_file())
            self.assertIn("source_window", sidecar.read_text(encoding="utf-8"))

    def test_cli_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "cli.html"
            code = main([str(FIXTURES), "-o", str(out), "--jobs", "1"])
            self.assertEqual(code, 0)
            self.assertTrue(out.is_file())
            self.assertGreater(out.stat().st_size, 1000)

    def test_run_py_works_from_any_cwd(self) -> None:
        script = REPO_ROOT / "log_analyzer" / "run.py"
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd="/tmp",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Android Java/Kotlin", result.stdout)

    def test_analyze_py_runs_as_a_script(self) -> None:
        script = REPO_ROOT / "log_analyzer" / "analyze.py"
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd="/tmp",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("relative", result.stderr.lower())
        self.assertIn("usage:", result.stdout.lower())

    def test_root_launcher_runs_as_a_script(self) -> None:
        script = REPO_ROOT / "run_log_analyzer.py"
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd="/tmp",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout.lower())

    def test_typo_module_log_nalayzer(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "log_nalayzer", "--help"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
