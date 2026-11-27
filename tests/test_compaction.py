# SPDX-License-Identifier: Apache-2.0
"""Stage 3: compaction by context pressure (mask, then summarize), on its own and in the engine."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from critique_bot import compaction
from critique_bot.agent import TOOL_FORMAT_NATIVE, _tool_setup, run_agent_loop
from critique_bot.compaction import Compactor, Context, Policy
from critique_bot.local_model import LocalModelClient, LocalModelSession
from critique_bot.session_log import SessionLog, history_from_events, load_blob, read_events
from test_native_tools import _call_chunks, _config, _Server


def _call(call_id: str, path: str, offset: int = 1) -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": "read_files", "arguments": json.dumps({"paths": [path], "offset": offset, "limit": 50})}}


def _conversation(results: int) -> list[dict]:
    messages: list[dict] = [{"role": "user", "content": "PREFIX\n\nTask: fix it"}]
    for n in range(results):
        messages.append({"role": "assistant", "content": f"step {n}", "tool_calls": [_call(f"c{n}", f"a{n}.py")]})
        messages.append({"role": "tool", "tool_call_id": f"c{n}", "content": f"contents of a{n}.py\n" + "x = 1\n" * 60})
    return messages


class PolicyTests(unittest.TestCase):
    def test_thresholds_and_the_turn_spacing(self) -> None:
        compactor = Compactor(Policy(window=1000))
        self.assertEqual(compactor.due(Context(used_tokens=600, turn=1)), "")
        self.assertEqual(compactor.due(Context(used_tokens=750, turn=1)), "mask")
        self.assertEqual(compactor.due(Context(used_tokens=900, turn=1)), "summarize")
        self.assertEqual(compactor.due(Context(used_tokens=600, pending_chars=700, turn=1)), "mask")  # the pending message counts
        self.assertEqual(compactor.due(Context(used_tokens=0, turn=1)), "")  # no usage: no guess
        compactor.last_turn = 3
        self.assertEqual(compactor.due(Context(used_tokens=900, turn=6)), "")  # at most once per 5 turns
        self.assertEqual(compactor.due(Context(used_tokens=900, turn=8)), "summarize")
        prefer = Compactor(Policy(window=1000, prefer_summary=True))
        self.assertEqual(prefer.due(Context(used_tokens=750, turn=1)), "summarize")

    def test_settings_override_the_defaults(self) -> None:
        policy = Policy.from_settings(4096, {"compaction_keep_results": 4, "masking_penalty": True,
                                             "compaction_mask_at": 2})
        self.assertEqual((policy.keep_results, policy.prefer_summary, policy.mask_at), (4, True, 0.95))


class MaskTests(unittest.TestCase):
    def test_old_results_become_pointers_to_blobs(self) -> None:
        blobs: dict[str, str] = {}

        def put(text: str) -> str:
            key = f"{len(blobs):064x}"
            blobs[key] = text
            return key

        messages = _conversation(12)
        out, entries = Compactor(Policy(window=1000), put_blob=put).mask(messages)
        self.assertEqual([entry["index"] for entry in entries], [2, 4])  # all but the last 10 results
        self.assertTrue(out[2]["content"].startswith("[elided: read_files a0.py lines 1-50 -> blob"))
        self.assertEqual(blobs[entries[0]["blob"]], messages[2]["content"])
        self.assertEqual(out[6], messages[6])
        self.assertEqual(messages[2]["content"][:11], "contents of")  # the input list is untouched
        again, more = Compactor(Policy(window=1000), put_blob=put).mask(out)
        self.assertEqual(more, [])  # already masked results stay as they are
        self.assertEqual(again, out)


class SummaryTests(unittest.TestCase):
    def test_summary_keeps_the_prefix_the_last_turns_and_recites_the_todos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for n in range(7):
                (root / f"a{n}.py").write_text(f"value = {n}\n", encoding="utf-8")
            (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
            seen: list = []

            def helper(messages, prompt):
                seen.append((len(messages), prompt))
                return "GOAL\nfix it\nDECISIONS\nnone"

            compactor = Compactor(Policy(window=1000), summarize=helper)
            messages = _conversation(6)
            ctx = Context(used_tokens=900, turn=9, task="fix it", workspace=root,
                          recent_files=[f"a{n}.py" for n in range(7)] + [".env"],
                          todos=[{"status": "in_progress", "content": "fix a3"}])
            result = compactor.compact(messages, ctx)
            self.assertEqual(result.strategy, "summarize")
            self.assertTrue(result.helper)
            new = result.messages
            self.assertTrue(new[0]["content"].startswith("PREFIX\n\nTask: fix it\n\nCONTEXT SUMMARY ("))
            self.assertEqual(new[1:], messages[-6:])  # the last 3 turns, verbatim
            self.assertEqual(seen[0][0], len(messages) - 6)
            self.assertIn("FAILING TESTS", seen[0][1])
            block = new[0]["content"]
            self.assertIn("=== a6.py ===", block)
            self.assertNotIn("=== a1.py ===", block)  # only the 5 newest files come back
            self.assertNotIn(".env", block.split("FILES YOU READ")[1])  # secret files never
            self.assertTrue(block.rstrip().endswith("- [in_progress] fix a3"))  # recitation last
            self.assertLess(result.tokens_after, result.tokens_before)
            # A later summary replaces the earlier block instead of stacking another.
            more = new + _conversation(4)[1:]
            ctx.turn = 20
            second = compactor.compact(more, ctx)
            self.assertEqual(second.messages[0]["content"].count("CONTEXT SUMMARY ("), 1)

    def test_without_a_helper_the_summary_comes_from_the_task_state(self) -> None:
        compactor = Compactor(Policy(window=1000))
        ctx = Context(used_tokens=900, turn=9, task="fix the parser", facts={"changed": "parser.py"})
        result = compactor.compact(_conversation(6), ctx)
        self.assertFalse(result.helper)
        self.assertIn("GOAL\nfix the parser", result.summary)
        self.assertIn("FILES CHANGED\nparser.py", result.summary)

    def test_reply_text_reads_responses_items(self) -> None:
        reply = json.dumps({"output": [{"type": "message", "content": [{"type": "output_text", "text": "GOAL\nx"}]}]})
        self.assertEqual(compaction.reply_text(reply), "GOAL\nx")


class EngineCompactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _Server()  # every reply reports 100 prompt + 10 completion tokens

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def setUp(self) -> None:
        self.server.replies, self.server.requests = [], []
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for n in range(4):
            (self.root / f"f{n}.py").write_text("".join(f"line_{n}_{i} = {i}\n" for i in range(40)), encoding="utf-8")
        self.setup = _tool_setup({}, helpers=0, web=False, native_ok=True)
        self.slog = SessionLog.create(self.root / ".crit" / "sessions")

    def tearDown(self) -> None:
        self.slog.close()
        self.tmp.cleanup()

    def _run(self, window: int, replies: list, every: int = 1, **extra) -> LocalModelSession:
        self.server.replies = replies
        session = LocalModelSession(LocalModelClient(_config(self.server.url)))
        settings = {"reply_retries": 0, "tool_format": TOOL_FORMAT_NATIVE, "_native_tools": self.setup.native_tools,
                    "_context_window": window, "compaction_keep_results": 1, "compaction_every_turns": every,
                    "compaction_keep_turns": 1}
        outcome: list[str] = []
        # The fake server always reports 110 tokens; leave the pending message out of the estimate.
        with mock.patch.object(compaction, "CHARS_PER_TOKEN", 1e12):
            self._loop(session, settings, outcome, **extra)
        self.assertEqual(outcome, ["COMPLETED"])
        return session

    def _loop(self, session, settings, outcome, **extra) -> None:
        run_agent_loop(
            session, workspace=self.root, index_path=None, cache_dir=None, first_task="Read the four files",
            max_rounds=10, max_result_chars=8000, seed="INSTRUCTIONS.\n\nReply with exactly READY.",
            read_message=lambda: None, emit=lambda _t: None, outcome=outcome, session_log=self.slog,
            approve_mode="auto", settings=settings, **extra,
        )

    def _reads(self) -> list:
        return [_call_chunks([(f"r{n}", "read_files", {"paths": [f"f{n}.py"]})]) for n in range(4)]

    def test_pressure_masks_old_results_and_the_log_rebuilds_the_same_list(self) -> None:
        done = [[{"choices": [{"index": 0, "delta": {"content": "All read. COMPLETED"}}]}]]
        session = self._run(150, self._reads() + done)  # 110 of 150 tokens: over 70%, under 85%
        events = read_events(self.slog.folder)
        compactions = [e for e in events if e["type"] == "compaction"]
        self.assertTrue(compactions)
        self.assertTrue(all(e["strategy"] == "mask" for e in compactions))
        self.assertGreaterEqual(compactions[0]["tokens_before"], 110)
        self.assertLess(compactions[0]["tokens_after"], compactions[0]["tokens_before"])
        history = session.history()
        elided = [m for m in history if str(m.get("content") or "").startswith("[elided:")]
        self.assertTrue(elided)
        self.assertIn("read_files f0.py", elided[0]["content"])
        blob = compactions[0]["masked"][0]["blob"]
        self.assertIn("line_0_0 = 0", load_blob(self.slog.folder, blob))
        self.assertEqual(history_from_events(events, self.slog.folder).messages, history)
        self.assertTrue(history[0]["content"].startswith("INSTRUCTIONS."))
        # The requests after a mask still start with the same first message (the cached prefix).
        self.assertEqual(len({json.dumps(r["messages"][0]) for r in self.server.requests}), 1)

    def test_high_pressure_summarizes_with_one_helper_request(self) -> None:
        summary = [{"choices": [{"index": 0, "delta": {"content": "GOAL\nread files\nNEXT STEPS\nread f2"}}]}]
        done = [{"choices": [{"index": 0, "delta": {"content": "All read. COMPLETED"}}]}]
        reads = self._reads()
        side: list = []

        def factory():
            session = LocalModelSession(LocalModelClient(_config(self.server.url)))
            side.append(session)
            return session

        # 110 of 120 tokens: over 85%. Checked at the second request (too few turns to summarize),
        # then 3 turns later: the summary request comes before the fifth main request.
        session = self._run(120, [*reads, summary, done], every=3, side_factory=factory)
        events = read_events(self.slog.folder)
        summaries = [e for e in events if e["type"] == "compaction" and e["strategy"] == "summarize"]
        self.assertEqual(len(summaries), 1)
        self.assertTrue(summaries[0]["helper"])
        self.assertEqual(len(side), len(summaries))
        helper_request = self.server.requests[4]
        self.assertEqual(helper_request["messages"][0], self.server.requests[0]["messages"][0])  # same cached prefix
        self.assertIn("Do not call any tool", helper_request["messages"][-1]["content"])
        self.assertIn("CONTEXT SUMMARY (", session.history()[0]["content"])
        self.assertIn("GOAL\nread files", session.history()[0]["content"])
        self.assertEqual(history_from_events(events, self.slog.folder).messages, session.history())


if __name__ == "__main__":
    unittest.main()
