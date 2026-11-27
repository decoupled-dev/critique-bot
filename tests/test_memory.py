# SPDX-License-Identifier: Apache-2.0
"""Stage 8b: the project memory package. Store and provenance, cards and invalidation, change
patterns, episodes, conventions (generic filter, limit, stale check), the learned glossary,
locate's memory retrievers, the memory tool, and the KV primer against a fake llama.cpp server.

Recall in the engine, the task-loop hooks and ``crit memory`` are in test_memory_engine.py."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from critique_bot import code_index
from critique_bot.indexing import engine as index_engine
from critique_bot.indexing import history, locate, store as index_store
from critique_bot.memory import cards, conventions, episodes, glossary, patterns, primer
from critique_bot.memory import tool as memory_tool
from critique_bot.memory.store import MemoryStore, MemoryStoreError, db_path, parse_id, simple_scores

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@localhost", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@localhost", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

HELPER = '''"""Converts cabin temperature values between units for the display."""


class TempFmt:
    def to_display(self, value, unit):
        print("unit not loaded yet")
        return value


def a1(x):
    return x * 9 / 5 + 32
'''
PANEL = '''from helper import TempFmt


class ClimatePanel:
    def render(self):
        return TempFmt().to_display(21, "C")
'''


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env=GIT_ENV).stdout


def make_project(root: Path) -> None:
    (root / ".crit").mkdir(parents=True, exist_ok=True)
    (root / "helper.py").write_text(HELPER, encoding="utf-8")
    (root / "panel.py").write_text(PANEL, encoding="utf-8")
    (root / "test_panel.py").write_text("import panel\n\ndef test_render():\n    assert panel.ClimatePanel().render()\n",
                                        encoding="utf-8")


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.crit_home = mock.patch.dict(os.environ, {"CRIT_HOME": str(self.root / "_crit_home")})
        self.crit_home.start()
        make_project(self.root)
        self.index = self.root / ".crit" / "cache" / "index.db"
        self.stores: list[MemoryStore] = []

    def tearDown(self) -> None:
        for store in self.stores:
            try:
                store.close()
            except Exception:
                pass
        index_engine.wait_all(10)
        self.crit_home.stop()
        self.tmp.cleanup()

    def store(self, **kwargs) -> MemoryStore:
        found = MemoryStore(db_path(self.root), **kwargs)
        self.stores.append(found)
        return found

    def build_index(self) -> None:
        self.index.parent.mkdir(parents=True, exist_ok=True)
        code_index.refresh_index(self.root, self.index)

    def conn(self):
        conn = index_store.connect(self.index)
        self.addCleanup(conn.close)
        return conn


class StoreTests(_Tmp):
    def test_every_row_needs_provenance(self) -> None:
        store = self.store()
        with self.assertRaises(MemoryStoreError):
            store.add_episode({"task": "x", "files_changed": ["a.py"]})
        with self.assertRaises(MemoryStoreError):
            store.put_card("file", "a.py", hash="h", text="t", provenance="")
        with self.assertRaises(MemoryStoreError):
            store.add_convention("Run `make test`", ["out"], provenance="somewhere")
        saved = store.add_episode({"task": "fix the unit", "session": "s1", "files_changed": ["a.py"]})
        self.assertEqual(saved.provenance, "session:s1")

    def test_ids_and_forget(self) -> None:
        store = self.store()
        self.assertEqual(parse_id("e12"), ("e", 12))
        self.assertEqual(parse_id(" N3 "), ("n", 3))
        self.assertIsNone(parse_id("x1"))
        saved = store.add_episode({"task": "t", "session": "s"})
        self.assertIs(type(store.get(saved.ref)), type(saved))
        self.assertTrue(store.forget(saved.ref))
        self.assertIsNone(store.get(saved.ref))
        with self.assertRaises(ValueError):
            store.forget("nope")

    def test_memory_db_is_in_crit_memory_and_gitignored(self) -> None:
        from critique_bot import project_home

        self.assertEqual(db_path(self.root), self.root / ".crit" / "memory" / "memory.db")
        self.assertIn(".crit/memory/", project_home._GITIGNORE_LINES)
        self.assertIn("memory/", project_home._LOCAL_GITIGNORE)

    def test_episode_search_with_and_without_fts(self) -> None:
        for fts in (None, False):
            with self.subTest(fts=fts):
                path = self.root / f"m{fts}.db"
                store = MemoryStore(path, fts=fts)
                self.stores.append(store)
                store.add_episode({"task": "temperature shows wrong unit after reboot", "session": "s1",
                                   "root_cause": "unit read before config load", "files_changed": ["helper.py"]})
                store.add_episode({"task": "crash when opening settings", "session": "s2", "files_changed": ["x.py"]})
                store.add_episode({"task": "temperature unit wrong in the tainted run", "session": "s3", "trusted": False})
                found = store.search_episodes(["temperature", "unit"])
                self.assertEqual([e.task for e, _s in found], ["temperature shows wrong unit after reboot"])
                self.assertEqual(len(store.search_episodes(["temperature"], include_untrusted=True)), 2)
                self.assertEqual(store.fts, fts is None)

    def test_text_is_redacted_on_write(self) -> None:
        store = self.store()
        secret = "sk-live-abcdefghijklmnopqrstuvwxyz0123456789"  # gitleaks:allow (fake token)
        saved = store.add_episode({"task": f"rotate key {secret}", "session": "s"})
        self.assertNotIn(secret, saved.task)

    def test_simple_scores(self) -> None:
        scores = simple_scores(["fan", "speed"], [(1, "fan speed too high"), (2, "fan off"), (3, "door lock")])
        self.assertEqual([item for item, _s in scores], [1, 2])
        self.assertEqual(scores[0][1], 1.0)


class CardTests(_Tmp):
    def test_extractive_card_names_real_symbols_and_doc(self) -> None:
        self.build_index()
        store = self.store()
        card, built = cards.ensure_file_card(store, self.root, "helper.py", self.conn())
        self.assertTrue(built)
        self.assertIn("Converts cabin temperature", card.text)
        self.assertIn("TempFmt", card.text)
        self.assertIn("unit not loaded yet", card.text)
        self.assertIn("panel.py", card.text)  # used by
        self.assertTrue(card.provenance.startswith("file:helper.py@sha256:"))
        self.assertLessEqual(len(card.text), cards.FILE_CARD_CHARS)

    def test_card_is_rebuilt_when_the_file_changes_and_dropped_when_gone(self) -> None:
        store = self.store()
        card, built = cards.ensure_file_card(store, self.root, "panel.py", None)
        again, rebuilt = cards.ensure_file_card(store, self.root, "panel.py", None)
        self.assertTrue(built)
        self.assertFalse(rebuilt)
        self.assertEqual(card.hash, again.hash)
        (self.root / "panel.py").write_text(PANEL + "\n# Draws the climate panel.\n", encoding="utf-8")
        self.assertEqual(cards.invalidate(store, self.root), ["panel.py"])
        self.assertIsNone(store.card("file", "panel.py"))
        cards.ensure_file_card(store, self.root, "panel.py", None)
        (self.root / "panel.py").unlink()
        gone, _ = cards.ensure_file_card(store, self.root, "panel.py", None)
        self.assertIsNone(gone)
        self.assertIsNone(store.card("file", "panel.py"))

    def test_model_builder_uses_a_good_reply_and_falls_back_on_a_bad_one(self) -> None:
        self.build_index()
        store = self.store()
        prompts: list[str] = []

        def good(prompt: str) -> str:
            prompts.append(prompt)
            return "Formats cabin temperatures for the climate display; TempFmt.to_display is the entry point."

        card, _ = cards.ensure_file_card(store, self.root, "helper.py", self.conn(), cards.ModelBuilder(good))
        self.assertEqual(card.builder, "model")
        self.assertIn("TempFmt.to_display", card.text)
        self.assertIn("TempFmt", prompts[0])
        bad = cards.ModelBuilder(lambda _p: "This file does many things.")
        card2, _ = cards.ensure_file_card(store, self.root, "panel.py", self.conn(), bad)
        self.assertEqual(card2.builder, "extractive")
        self.assertEqual(bad.used_fallback, 1)

    def test_doc_comment_skips_license_headers(self) -> None:
        text = "/*\n * Copyright (C) 2020 The Android Open Source Project\n * Licensed under Apache\n */\n" \
               "package a;\n/**\n * Shows the fan speed on the HVAC panel.\n */\nclass Fan {}\n"
        self.assertEqual(cards.doc_comment(text), "Shows the fan speed on the HVAC panel.")


class PatternTests(_Tmp):
    def commit(self, files: dict[str, str], message: str) -> None:
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", message)

    def test_recipes_and_hot_spots_are_mined_incrementally(self) -> None:
        git(self.root, "init", "-q", "-b", "main")
        self.commit({"x.txt": "0"}, "start")
        for n in range(3):
            self.commit({"types.hal": f"{n}", "PropIds.java": f"{n}", "PropService.java": f"{n}", f"note{n}.txt": "x"},
                        f"add vehicle property {n}")
        self.commit({"helper.py": HELPER + "#1\n"}, "fix crash in helper")
        self.commit({"helper.py": HELPER + "#2\n"}, "Fix wrong unit")
        self.build_index()
        conn = self.conn()
        history.update(conn, self.root)
        store = self.store()
        self.assertEqual(patterns.mine(store, conn, self.index), 6)
        recipes = store.recipes()
        self.assertTrue(recipes)
        self.assertEqual(recipes[0].files, ["PropIds.java", "PropService.java", "types.hal"])
        self.assertEqual(recipes[0].count, 3)
        self.assertEqual(patterns.recipe_title(recipes[0].subjects), "add vehicle property")
        self.assertTrue(recipes[0].provenance.startswith("commit:"))
        spots = store.hotspots()
        self.assertEqual(spots[0].path, "helper.py")
        self.assertEqual(spots[0].fixes, 2)
        self.assertEqual(patterns.mine(store, conn, self.index), 0)  # nothing new
        self.commit({"helper.py": HELPER + "#3\n"}, "bug: off by one")
        history.update(conn, self.root)
        self.assertEqual(patterns.mine(store, conn, self.index), 1)
        self.assertEqual(store.hotspots(paths=["helper.py"])[0].fixes, 3)

    def test_session_changes_count_as_change_sets(self) -> None:
        store = self.store()
        for session in ("s1", "s2"):
            patterns.add_session_change(store, ["a.py", "b.py"], session=session, task="t", ts=1)
        found = store.recipes(files=["a.py"])
        self.assertEqual(found[0].sessions, ["s2", "s1"])


def _events(task: str, final: str, *, code: str = "COMPLETED", edits: bool = True, test_exit: int | None = 0,
            mode: str = "auto") -> list[dict]:
    events = [{"type": "user_message", "text": "older task", "task": True},
              {"type": "task_finished", "code": "COMPLETED"},
              {"type": "user_message", "text": task, "task": True}]
    if edits:
        events += [
            {"type": "permission_decision", "kind": "edit", "allowed": True, "mode": mode},
            {"type": "tool_started", "tool": "edit_file", "call_id": "c1", "arguments": {"path": "helper.py"}},
            {"type": "tool_result", "tool": "edit_file", "call_id": "c1", "ok": True, "mutating": True, "summary": "edited"},
        ]
    if test_exit is not None:
        events += [
            {"type": "tool_started", "tool": "run_command", "call_id": "c2", "arguments": {"command": "python -m pytest -q"}},
            {"type": "tool_result", "tool": "run_command", "call_id": "c2", "ok": True, "summary": f"exit {test_exit} (1.0s)"},
        ]
    events += [{"type": "assistant_turn", "text": final},
               {"type": "task_finished", "code": code, "changed": ["helper.py"] if edits else None}]
    return events


class EpisodeTests(_Tmp):
    FINAL = ("Done.\nRoot cause: `TempFmt.to_display` read the unit before the config was loaded on cold boot.\n"
             "Fix: defer the unit lookup until onConfigLoaded.")

    def test_verified_episode_with_cause_and_fix(self) -> None:
        found = episodes.build(_events("Temperature shows wrong unit after reboot", self.FINAL), workspace=self.root,
                               session="s1", task="Temperature shows wrong unit after reboot", changed=["helper.py"])
        self.assertEqual(found["outcome"], "verified")
        self.assertEqual(found["kind"], "positive")
        self.assertEqual(found["class"], "bugfix")
        self.assertIn("before the config was loaded", found["root_cause"])
        self.assertIn("defer the unit lookup", found["fix_summary"])
        self.assertIn("TempFmt.to_display", found["symbols"])
        self.assertEqual(found["evidence"]["tests"], ["python -m pytest -q"])
        self.assertTrue(found["files_hash"]["helper.py"].startswith("sha256:"))
        self.assertTrue(found["trusted"])

    def test_outcomes(self) -> None:
        cases = [
            (dict(test_exit=1), "completed", "positive"),
            (dict(test_exit=None, mode="ask"), "accepted", "positive"),
            (dict(code="FAILED"), "failed", "negative"),
            (dict(code="INTERRUPTED"), "abandoned", "negative"),
        ]
        for kwargs, outcome, kind in cases:
            with self.subTest(outcome=outcome):
                found = episodes.build(_events("fix the panel crash", "It still crashes.", **kwargs), workspace=self.root,
                                       session="s", task="fix the panel crash", changed=["helper.py"])
                self.assertEqual((found["outcome"], found["kind"]), (outcome, kind))
        negative = episodes.build(_events("fix the panel crash", "I changed the cache size but it still crashes.",
                                          code="FAILED"), workspace=self.root, session="s", task="fix the panel crash",
                                  changed=["helper.py"])
        self.assertIn("cache size", negative["fix_summary"])

    def test_tainted_session_episode_is_untrusted(self) -> None:
        found = episodes.build(_events("t", self.FINAL), workspace=self.root, session="s", changed=["helper.py"],
                               tainted=True)
        self.assertFalse(found["trusted"])

    def test_question_without_edits_is_not_an_episode(self) -> None:
        found = episodes.build(_events("What does the panel do?", "It renders the climate panel.", edits=False,
                                       test_exit=None), workspace=self.root, session="s", changed=[])
        self.assertIsNone(found)

    def test_cause_and_fix_heuristics(self) -> None:
        self.assertEqual(episodes.root_cause("The problem was that the cache never expired."), "the cache never expired")
        self.assertEqual(episodes.fix_summary("I fixed it by clearing the cache on resume."), "clearing the cache on resume")
        self.assertEqual(episodes.root_cause("All good."), "")


class ConventionTests(_Tmp):
    def test_generic_advice_is_refused(self) -> None:
        for text in ("Write clean code.", "Always test your changes before you finish.", "Make sure to handle errors properly",
                     "short"):
            with self.subTest(text=text):
                self.assertTrue(conventions.generic_reason(text))
        for text in ("Tests in this repo run with `atest CarServiceUnitTest`, not Gradle.",
                     "Strings for the panel live in res/values/strings.xml; never hard-code them in ClimatePanel."):
            with self.subTest(text=text):
                self.assertEqual(conventions.generic_reason(text), "")

    def test_two_proposals_per_task_with_evidence(self) -> None:
        store = self.store()
        text = "Unit tests run with `python -m pytest -q` from the project root."
        made, why = conventions.propose(store, text, [], session="s", proposed_so_far=0)
        self.assertIsNone(made)
        self.assertIn("evidence", why)
        made, _ = conventions.propose(store, text, ["exit 0, 3 passed"], session="s", proposed_so_far=0)
        self.assertEqual(made.status, "proposed")
        self.assertIn("session s", made.evidence)
        dup, why = conventions.propose(store, text, ["again"], session="s", proposed_so_far=1)
        self.assertIsNone(dup)
        self.assertIn("already proposed", why)
        over, why = conventions.propose(store, "Build with `make -C native all`.", ["ok"], session="s", proposed_so_far=2)
        self.assertIsNone(over)
        self.assertIn("at most 2", why)

    def test_stale_check_flags_missing_files_and_symbols(self) -> None:
        store = self.store()
        keep = store.add_convention("Panel strings are built in panel.py by `ClimatePanel`.", ["x"], provenance="user:test",
                                    status="approved")
        gone = store.add_convention("Fan code lives in fan/speed.py.", ["x"], provenance="user:test", status="approved")
        renamed = store.add_convention("Units are converted by `TempFormatter` in helper.py.", ["x"], provenance="user:test",
                                       status="approved")
        flagged = conventions.stale_check(store, self.root, defined=lambda name: name in {"ClimatePanel", "TempFmt"})
        self.assertEqual(sorted(item.ref for item, _why in flagged), sorted([gone.ref, renamed.ref]))
        self.assertEqual(store.convention(keep.id).status, "approved")
        self.assertEqual(store.convention(gone.id).status, "stale")


class GlossaryTests(_Tmp):
    def test_descriptions_learn_badly_named_symbols(self) -> None:
        found = dict(glossary.descriptions("`a1` converts the cabin temperature to fahrenheit. "
                                           "convertTemperature converts temperature values."))
        self.assertEqual(found["a1"], ["cabin", "temperature", "fahrenheit"])
        self.assertNotIn("convertTemperature", found)

    def test_learned_terms_reach_the_index_and_locate(self) -> None:
        self.build_index()
        store = self.store()
        conn = self.conn()
        added = glossary.learn(store, ["`a1` converts the cabin temperature to fahrenheit."], provenance="session:s1",
                               defined=lambda name: name == "a1", index_conns=[conn])
        self.assertIn(("a1", "fahrenheit"), added)
        rows = conn.execute("SELECT term, expansion, source FROM glossary WHERE term = 'a1'").fetchall()
        self.assertIn(("a1", "fahrenheit", "learned"), [tuple(r) for r in rows])
        found = locate.locate([self.index], "fahrenheit value is off by a few degrees", workspace=str(self.root))
        self.assertIn("glossary", found.ran)
        self.assertEqual(found.candidates[0].path, "helper.py")
        self.assertIn("a1", found.candidates[0].symbols)


class LocateRetrieverTests(_Tmp):
    def test_cards_retriever_finds_code_by_its_card_text(self) -> None:
        self.build_index()
        store = self.store()
        store.put_card("file", "panel.py", hash="h", text="panel.py: draws the defroster indicator on the dashboard.",
                       provenance="file:panel.py@h")
        found = locate.locate([self.index], "defroster indicator missing", workspace=str(self.root), only=["cards"])
        self.assertEqual(found.candidates[0].path, "panel.py")
        self.assertIn("card", locate.hypothesis(found.candidates[0]))

    def test_no_memory_no_hits(self) -> None:
        self.build_index()
        found = locate.locate([self.index], "defroster indicator missing", workspace=str(self.root), only=["cards", "glossary"])
        self.assertEqual(found.candidates, [])


class ToolTests(_Tmp):
    def ctx(self, **settings):
        return SimpleNamespace(workspace=self.root, settings=settings, state=SimpleNamespace(), max_chars=6000,
                               index_path=self.index)

    def test_search_show_propose(self) -> None:
        store = self.store()
        saved = store.add_episode({"task": "fan speed too high on boot", "session": "s1", "outcome": "verified",
                                   "files_changed": ["helper.py"], "files_hash": {"helper.py": "sha256:old"}})
        ok, text = memory_tool.run({"action": "search", "query": "fan speed"}, self.ctx())
        self.assertTrue(ok)
        self.assertIn(saved.ref, text)
        ok, text = memory_tool.run({"action": "show", "id": saved.ref}, self.ctx())
        self.assertIn("code changed since: helper.py", text)
        self.assertIn("source: session:s1", text)
        ctx = self.ctx()
        memory_tool.set_session("s7")
        for n in range(2):
            ok, text = memory_tool.run({"action": "propose", "text": f"Fan limits live in `FAN_MAX_{n}` in helper.py.",
                                        "evidence": ["helper.py:3"]}, ctx)
            self.assertTrue(ok, text)
        ok, text = memory_tool.run({"action": "propose", "text": "Fan code uses `FanCtl` everywhere in helper.py.",
                                    "evidence": ["helper.py:3"]}, ctx)
        self.assertFalse(ok)
        self.assertIn("at most 2", text)
        ok, text = memory_tool.run({"action": "propose", "text": "Write clean code.", "evidence": ["x"]}, self.ctx())
        self.assertFalse(ok)
        self.assertEqual([c.status for c in store.conventions()], ["proposed", "proposed"])
        self.assertIn("session s7", store.conventions()[0].evidence)

    def test_modes(self) -> None:
        ok, text = memory_tool.run({"action": "search", "query": "x"}, self.ctx(memory="off"))
        self.assertIn("off", text)
        ok, text = memory_tool.run({"action": "propose", "text": "Use `make` in helper.py dir.", "evidence": ["x"]},
                                   self.ctx(memory="recall"))
        self.assertFalse(ok)
        self.assertEqual(memory_tool.mode({"memory": False}), "off")
        self.assertEqual(memory_tool.mode({}), "full")


class _SlotServer:
    def __init__(self, saved: set[str]) -> None:
        self.saved = saved
        self.calls: list[tuple[str, dict]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.calls.append((self.path, body))
                action = self.path.rpartition("action=")[2]
                name = body.get("filename", "")
                if action == "restore" and name not in outer.saved:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b'{"error": "failed to restore slot"}')
                    return
                if action == "save":
                    outer.saved.add(name)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"id_slot": 0, "filename": name}).encode())

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/v1"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class PrimerTests(unittest.TestCase):
    def test_key_changes_with_any_input(self) -> None:
        base = primer.primer_key("prefix", model="qwen", quant="q4", context=32768)
        self.assertEqual(base, primer.primer_key(b"prefix", model="qwen", quant="q4", context=32768))
        for other in (primer.primer_key("prefix!", model="qwen", quant="q4", context=32768),
                      primer.primer_key("prefix", model="qwen2", quant="q4", context=32768),
                      primer.primer_key("prefix", model="qwen", quant="q8", context=32768),
                      primer.primer_key("prefix", model="qwen", quant="q4", context=16384)):
            self.assertNotEqual(base, other)

    def test_build_then_restore_against_a_fake_server(self) -> None:
        server = _SlotServer(set())
        try:
            slots = primer.SlotPrimer(server.url, slot=1, timeout=5)
            key = primer.primer_key("system prompt and tools", model="m", context=8192)
            warmed: list[int] = []
            first = slots.ensure(key, lambda: warmed.append(1))
            self.assertEqual(first.action, "built")
            self.assertEqual(warmed, [1])
            second = slots.ensure(key, lambda: warmed.append(2))
            self.assertEqual(second.action, "restored")
            self.assertEqual(warmed, [1])
            paths = [path for path, _body in server.calls]
            self.assertEqual(paths, ["/slots/1?action=restore", "/slots/1?action=save", "/slots/1?action=restore"])
            self.assertEqual(server.calls[1][1]["filename"], primer.file_name(key))
        finally:
            server.stop()

    def test_unreachable_server_and_flags(self) -> None:
        slots = primer.SlotPrimer("http://127.0.0.1:9/v1", timeout=0.5)
        result = slots.ensure("k" * 64, lambda: None)
        self.assertEqual(result.action, "skipped")
        with self.assertRaises(ValueError):
            primer.server_root("file:///etc/passwd")
        self.assertFalse(primer.enabled({}))
        self.assertTrue(primer.enabled({}, {"kv_primer": True}))
        self.assertFalse(primer.enabled({"kv_primer": False}, {"kv_primer": True}))


if __name__ == "__main__":
    unittest.main()
