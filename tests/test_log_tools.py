"""The log tool: formats, events with their stacks, templates, query, around, diff."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from critique_bot import log_tools
from critique_bot.agent_tools import TaskState, ToolContext, execute

CRASH = [
    "11-25 10:37:07.117  5511  5511 E AndroidRuntime: FATAL EXCEPTION: main",
    "11-25 10:37:07.117  5511  5511 E AndroidRuntime: Process: com.example.hvac, PID: 5511",
    "11-25 10:37:07.117  5511  5511 E AndroidRuntime: java.lang.NullPointerException: boom",
    "11-25 10:37:07.117  5511  5511 E AndroidRuntime: \tat com.example.hvac.Panel.onChange(Panel.java:142)",
    "11-25 10:37:07.117  5511  5511 E AndroidRuntime: \tat android.os.Looper.loop(Looper.java:317)",
]


def _logcat(lines: int, *, crash_at: int | None = None, extra_errors: int = 0) -> str:
    rows = [f"11-25 10:00:{i % 60:02d}.{i % 1000:03d}  1234  1240 I CarService: event {i} ok" for i in range(lines)]
    for i in range(extra_errors):
        rows.insert(10 + i, f"11-25 10:00:01.000  1234  1240 E VHAL: property 0x{1000 + i:x} not available")
    rows.insert(5, "11-25 10:00:00.500  1100  1100 I ActivityManager: Start proc 5511:com.example.hvac/u0a12 for activity")
    if crash_at is not None:
        rows[crash_at:crash_at] = CRASH
    return "\n".join(rows) + "\n"


class LogToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.ctx = ToolContext(workspace=self.root, state=TaskState(task="t"))
        (self.root / "bad.txt").write_text(_logcat(3000, crash_at=2000, extra_errors=40))
        (self.root / "good.txt").write_text(_logcat(3000, extra_errors=2))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_formats(self) -> None:
        self.assertEqual(log_tools.sniff(self.root / "bad.txt"), "logcat")
        (self.root / "k.txt").write_text("".join(f"[  {i}.000001] usb 1-1: new device\n" for i in range(20)))
        self.assertEqual(log_tools.sniff(self.root / "k.txt"), "kernel")
        (self.root / "b.txt").write_text("".join(f"[ {i}% {i}/100] //a:b javac\n" for i in range(20)))
        self.assertEqual(log_tools.sniff(self.root / "b.txt"), "build")

    def test_summary_leads_with_the_crash_and_its_stack(self) -> None:
        out = execute("log", {"path": "bad.txt"}, self.ctx)["output"]
        self.assertIn("EVENTS: app crash x1", out)
        self.assertIn("Panel.java:142", out)
        self.assertLess(out.index("EVENTS"), out.index("ERRORS"))
        # 40 VHAL errors with different ids are one group, not 40 lines
        self.assertIn("x40 · ", out)
        self.assertIn("property <hex> not available", out)

    def test_query_by_package_level_and_pattern(self) -> None:
        out = execute("log", {"action": "query", "path": "bad.txt", "package": "com.example.hvac"}, self.ctx)["output"]
        self.assertIn("5 matching lines", out)
        self.assertIn("FATAL EXCEPTION", out)
        out = execute("log", {"action": "query", "path": "bad.txt", "level": "E", "tag": "VHAL", "limit": 5}, self.ctx)["output"]
        self.assertIn("first 5 matching lines from line 1; more follow", out)
        self.assertIn("more: offset=17", out)
        out = execute("log", {"action": "query", "path": "bad.txt", "pattern": "event 2999 ok"}, self.ctx)["output"]
        self.assertIn("1 matching lines", out)

    def test_around_a_line(self) -> None:
        out = execute("log", {"action": "around", "path": "bad.txt", "line": 100, "before": 2, "after": 2}, self.ctx)["output"]
        self.assertIn("98|", out)
        self.assertIn("102|", out)
        self.assertNotIn("103|", out)

    def test_diff_shows_what_is_new_in_the_bad_log(self) -> None:
        out = execute("log", {"action": "diff", "path": "bad.txt", "good": "good.txt"}, self.ctx)["output"]
        self.assertIn("app crash 0->1", out)
        self.assertIn("FATAL EXCEPTION", out.split("NEW in the bad log")[1])
        self.assertIn("MORE FREQUENT", out)

    def test_build_log_errors_are_events(self) -> None:
        (self.root / "build.log").write_text(
            "".join(f"[ {i}% {i}/100] //a:b javac\n" for i in range(50))
            + "FAILED: out/soong/.intermediates/x/CarService.jar\n"
            + "src/Foo.java:28: error: cannot find symbol\n"
            + "        return mCarPropertyManger.get();\n"
            + "  symbol:   variable mCarPropertyManger\n"
            + "1 error\n"
        )
        out = execute("log", {"path": "build.log"}, self.ctx)["output"]
        self.assertIn("compiler error at line 52", out)
        self.assertIn("symbol:   variable mCarPropertyManger", out)

    def test_errors_for_the_model(self) -> None:
        self.assertIn("action must be", execute("log", {"action": "dance", "path": "bad.txt"}, self.ctx)["error"])
        self.assertIn("not found", execute("log", {"path": "nope.txt"}, self.ctx)["error"])
        self.assertIn("good", execute("log", {"action": "diff", "path": "bad.txt"}, self.ctx)["error"])


if __name__ == "__main__":
    unittest.main()
