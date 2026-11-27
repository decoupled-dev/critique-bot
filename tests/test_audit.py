"""The audit log and crit audit."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from critique_bot import audit, audit_cmd


class AuditLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        patcher = mock.patch.dict(os.environ, {"CRIT_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(audit.set_session, audit.session())
        audit.set_session("s-one")

    def lines(self) -> list[dict]:
        return [json.loads(line) for line in audit.path().read_text(encoding="utf-8").splitlines()]

    def test_records_are_json_lines_owner_only(self) -> None:
        audit.record("command", command="make", cwd="/w", sandbox="workspace-write", exit=0)
        self.assertEqual(audit.path(), self.home / "logs" / "audit.jsonl")
        entry = self.lines()[0]
        self.assertEqual((entry["kind"], entry["session"], entry["command"], entry["exit"]), ("command", "s-one", "make", 0))
        self.assertIn("ts", entry)
        if os.name != "nt":
            self.assertEqual(audit.path().stat().st_mode & 0o777, 0o600)

    def test_secrets_are_redacted(self) -> None:
        audit.record("command", command="curl -H 'Authorization: Bearer ghp_" + "a" * 36 + "' https://x")
        self.assertNotIn("ghp_" + "a" * 36, audit.path().read_text(encoding="utf-8"))

    def test_reserved_names_are_not_overwritten(self) -> None:
        audit.record("permission", kind="command", session="other")
        entry = self.lines()[0]
        self.assertEqual((entry["kind"], entry["session"], entry["kind_"], entry["session_"]),
                         ("permission", "s-one", "command", "other"))

    def test_rotation_keeps_three_files(self) -> None:
        with mock.patch.object(audit, "MAX_BYTES", 300):
            for index in range(40):
                audit.record("write", tool="write_files", paths=[f"file{index}.py"])
        names = sorted(item.name for item in (self.home / "logs").iterdir())
        self.assertEqual(names, ["audit.jsonl", "audit.jsonl.1", "audit.jsonl.2"])

    def test_read_filters(self) -> None:
        audit.record("command", command="a")
        audit.set_session("s-two")
        audit.record("egress", host="pypi.org", allowed=True)
        self.assertEqual([e["command"] for e in audit.read(session_id="s-one")], ["a"])
        self.assertEqual([e["kind"] for e in audit.read(kinds=("egress",))], ["egress"])
        self.assertEqual(len(list(audit.read(since="1h"))), 2)
        self.assertEqual(len(list(audit.read(since="2999-01-01"))), 0)
        with self.assertRaises(ValueError):
            list(audit.read(since="yesterday-ish"))

    def test_crit_audit_prints_readable_lines(self) -> None:
        audit.record("command", command="./gradlew test", cwd="/w", sandbox="workspace-write", exit=0, result="exit 0")
        audit.record("egress", host="evil.example", port=443, method="CONNECT", allowed=False, reason="strict", sent=0, received=0)
        audit.record("taint", event="raised", source="web_fetch", detail="https://x.example")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = audit_cmd.main(["--session", "s-one"])
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("[workspace-write] ./gradlew test", text)
        self.assertIn("DENIED CONNECT evil.example:443", text)
        self.assertIn("raised by web_fetch: https://x.example", text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            audit_cmd.main(["--json", "--kind", "taint"])
        self.assertEqual(json.loads(out.getvalue().strip())["source"], "web_fetch")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            audit_cmd.main(["--sessions"])
        self.assertIn("s-one", out.getvalue())

    def test_crit_audit_is_routed(self) -> None:
        from critique_bot.cli import SUBCOMMANDS, main

        self.assertIn("audit", SUBCOMMANDS)
        self.assertIn("doctor", SUBCOMMANDS)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["audit", "--since", "1d"]), 0)

    def test_writing_never_raises(self) -> None:
        with mock.patch.object(audit, "_append", side_effect=OSError("disk full")):
            self.assertEqual(audit.record("command", command="x")["command"], "x")


class AgentAuditTests(unittest.TestCase):
    """Commands and writes the agent's tools make land in the log."""

    def test_writes_and_commands(self) -> None:
        from critique_bot import agent_shell, agent_tools

        records: list[dict] = []
        audit._sink = records
        self.addCleanup(setattr, audit, "_sink", None)
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        session = agent_shell.ShellSession(root)
        self.addCleanup(session.close)
        ctx = agent_tools.ToolContext(workspace=root, session=session)
        agent_tools.execute("write_files", {"files": [{"path": "a.txt", "content": "x\n"}]}, ctx)
        agent_tools.execute("run_command", {"command": "echo hi"}, ctx)
        kinds = [(item["kind"], item.get("tool") or item.get("command")) for item in records]
        self.assertIn(("write", "write_files"), kinds)
        self.assertIn(("command", "echo hi"), kinds)
        command = next(item for item in records if item["kind"] == "command")
        self.assertEqual(command["sandbox"], "full")
        self.assertEqual(command["exit"], 0)


if __name__ == "__main__":
    unittest.main()
