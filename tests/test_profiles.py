"""Model profiles: the built-ins load and validate, names resolve (OpenRouter, Hugging Face,
Ollama, GGUF spellings), user overrides, local.profile, the doctor cache behind
``tool_mode: auto``, and how the local client and the engine apply a profile."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from critique_bot import model_api, profiles, run_stats
from critique_bot.agent import TOOL_FORMAT_NATIVE, TOOL_FORMAT_OPENAI, _tool_setup
from critique_bot.config import BotConfig, LocalConfig, load_config
from critique_bot.local_model import LocalModelClient, LocalModelSession, _reported_ctx

from test_native_tools import TOOLS, _call_chunks, _Server


def _config(url: str, model: str = "qwen3.6-27b", **local: object) -> BotConfig:
    return BotConfig(url=url, selectors=None, model=model, backend="local", local=LocalConfig(**local),
                     reply_start_ms=5_000, reply_stall_ms=5_000, reply_max_ms=20_000)


class _UserDir:
    """CRIT_HOME pointed at a temp folder with the given user profile files."""

    def __init__(self, files: dict[str, object] | None = None) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "profiles").mkdir()
        for name, data in (files or {}).items():
            text = data if isinstance(data, str) else json.dumps(data)
            (self.home / "profiles" / name).write_text(text, encoding="utf-8")

    def __enter__(self) -> Path:
        self.old = os.environ.get("CRIT_HOME")
        os.environ["CRIT_HOME"] = str(self.home)
        return self.home

    def __exit__(self, *exc: object) -> None:
        if self.old is None:
            os.environ.pop("CRIT_HOME", None)
        else:
            os.environ["CRIT_HOME"] = self.old
        self.tmp.cleanup()


class BuiltinProfileTests(unittest.TestCase):
    def test_builtins_load_validate_and_cover_every_family(self) -> None:
        with _UserDir():
            found = {p.id: p for p in profiles.load_profiles()}
        for family in ("qwen3", "qwen3-coder", "qwen3.5", "qwen3.6", "qwen3.6-27b", "qwen3.8", "deepseek-v3",
                       "deepseek-r1-distill", "glm-4.5", "glm-4.7", "devstral", "mistral-small", "gpt-oss",
                       "llama3", "llama4", "gemma3", "gemma4", "kimi-k2", "minimax-m2", "granite", "phi4", "generic"):
            self.assertIn(family, found)
        for profile in found.values():
            self.assertIn(profile.tool_mode, profiles.TOOL_MODES)
            self.assertTrue(profile.notes, f"{profile.id} has no notes")
            if profile.sampling:  # values from a model card must say which one
                self.assertTrue(profile.source or "card" in profile.notes.lower() or profile.id == "qwen3.6-27b",
                                f"{profile.id} sets sampling without a source")
        self.assertEqual(found["generic"].tool_mode, "auto")
        self.assertEqual(found["generic"].sampling, ())
        # a size override inherits its family's fields
        self.assertEqual(found["qwen3.6-27b"].sampling, found["qwen3.6"].sampling)
        self.assertTrue(found["qwen3.6-27b"].preserve_thinking)
        self.assertEqual(found["qwen3.6-27b"].effective_context, 32768)
        self.assertFalse(found["deepseek-r1-distill"].preserve_thinking)

    def test_names_resolve_in_every_spelling(self) -> None:
        cases = {
            "qwen/qwen3.6-27b": "qwen3.6-27b",
            "Qwen/Qwen3.6-27B": "qwen3.6-27b",
            "Qwen3.6-27B-Q4_K_M.gguf": "qwen3.6-27b",
            "qwen3.6:35b-a3b": "qwen3.6",
            "qwen3:8b": "qwen3",
            "qwen3-coder:30b": "qwen3-coder",
            "Qwen/Qwen3-Coder-480B-A35B-Instruct": "qwen3-coder",
            "deepseek-r1:14b": "deepseek-r1-distill",
            "z-ai/glm-4.6": "glm-4.5",
            "glm-4.7": "glm-4.7",
            "mistralai/devstral-small-2507": "devstral",
            "openai/gpt-oss-120b": "gpt-oss",
            "meta-llama/llama-3.3-70b-instruct": "llama3",
            "gemma3:12b": "gemma3",
            "moonshotai/kimi-k2-instruct": "kimi-k2",
            "minimax/minimax-m2": "minimax-m2",
            "something-else": "generic",
            "": "generic",
        }
        with _UserDir():
            for name, want in cases.items():
                self.assertEqual(profiles.resolve(name).id, want, name)

    def test_server_presets_load_and_fill_the_launch_line(self) -> None:
        presets = {p.id: p for p in profiles.server_presets()}
        self.assertEqual(set(presets) >= {"llama.cpp", "vllm", "sglang", "ollama", "lmstudio", "tgi", "mlx-lm",
                                          "openrouter"}, True)
        with _UserDir():
            qwen = profiles.resolve("qwen3.6-27b")
        line = presets["vllm"].launch_line("Qwen/Qwen3.6-27B", qwen, 32768)
        self.assertIn("--tool-call-parser qwen3_coder", line)
        self.assertIn("--reasoning-parser qwen3", line)
        self.assertIn("--max-model-len 32768", line)
        self.assertIn("--jinja", presets["llama.cpp"].launch_line("m.gguf", qwen))
        self.assertIn("max_tokens", json.dumps(presets["openrouter"].data))


class UserProfileTests(unittest.TestCase):
    def test_a_user_profile_overrides_fields_of_a_builtin(self) -> None:
        with _UserDir({"q.json": {"id": "qwen3.6", "sampling": {"temperature": 0.2}, "max_tokens": 4096}}):
            found = profiles.resolve("qwen3.6-35b")
        self.assertEqual(found.origin, "builtin+user")
        self.assertEqual(found.sampling_dict()["temperature"], 0.2)
        self.assertEqual(found.sampling_dict()["top_k"], 20)  # the rest stays
        self.assertEqual(found.max_tokens, 4096)

    def test_a_new_user_profile_wins_a_tie_and_local_profile_picks_by_id(self) -> None:
        mine = {"id": "my-qwen", "extends": "qwen3.6", "match": ["qwen3.6*"], "tool_mode": "prompted"}
        with _UserDir({"mine.json": mine}):
            self.assertEqual(profiles.resolve("qwen3.6-35b").id, "my-qwen")
            self.assertEqual(profiles.resolve("qwen3.6-35b").sampling_dict()["top_k"], 20)
            self.assertEqual(profiles.resolve("anything", "gemma3").id, "gemma3")
            with _quiet():
                self.assertEqual(profiles.resolve("qwen3:8b", "no-such").id, "qwen3")

    def test_a_broken_user_file_is_skipped(self) -> None:
        files = {"bad.json": "{not json", "worse.json": {"id": "x", "tool_mode": "sometimes"},
                 "ok.json": {"id": "ok", "match": ["okmodel*"]}}
        with _UserDir(files), _quiet():
            found = {p.id for p in profiles.load_profiles()}
        self.assertIn("ok", found)
        self.assertNotIn("x", found)

    def test_validation_rejects_bad_values(self) -> None:
        for bad in ({"id": "a", "sampling": {"temperature": "hot"}}, {"id": "a", "sampling": {"tempo": 1}},
                    {"id": "a", "effective_context": 10}, {"id": "a", "edit_format": "diff3"},
                    {"id": "a", "preserve_thinking": "yes"}, {"id": "a", "colour": "red"}, {"match": ["x"]},
                    {"id": "a", "sampling": {"top_k": 2.5}}):
            with self.assertRaises(profiles.ProfileError, msg=str(bad)):
                profiles._validate(bad, "test.json")

    def test_local_profile_is_read_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"backend": "local", "url": "http://127.0.0.1:8080", "model": "m",
                                        "local": {"profile": "gemma3"}}), encoding="utf-8")
            self.assertEqual(load_config(path).local.profile, "gemma3")


class _quiet:
    def __enter__(self) -> None:
        from critique_bot import log

        self.log = log
        self.saved = log.warn
        log.warn = lambda *a, **k: None

    def __exit__(self, *exc: object) -> None:
        self.log.warn = self.saved


class ToolModeTests(unittest.TestCase):
    def test_auto_trusts_the_doctor_cache(self) -> None:
        with _UserDir() as home:
            url, model = "http://127.0.0.1:9", "mystery-7b"
            config = _config(url, model)
            self.assertEqual(model_api.tool_mode(config), "native")  # no probe yet
            profiles.save_probe(url, model, {"recommend": "prompted"})
            self.assertTrue((home / "cache" / "doctor.json").is_file())
            self.assertEqual(model_api.tool_mode(config), "prompted")
            self.assertFalse(model_api.native_tools_ok(config))
            # another model on the same server is not affected
            self.assertEqual(model_api.tool_mode(replace(config, model="other")), "native")
            # a profile that says native ignores the cache
            profiles.save_probe(url, "qwen3.6-27b", {"recommend": "prompted"})
            self.assertEqual(model_api.tool_mode(replace(config, model="qwen3.6-27b")), "native")

    def test_prompted_profile_makes_the_engine_use_the_text_format(self) -> None:
        with _UserDir():
            config = _config("http://127.0.0.1:9", "gemma3:12b")
            self.assertFalse(model_api.native_tools_ok(config))
            setup = _tool_setup({}, helpers=0, web=False, native_ok=model_api.native_tools_ok(config))
            self.assertEqual(setup.tool_format, TOOL_FORMAT_OPENAI)
            native = _tool_setup({}, helpers=0, web=False,
                                 native_ok=model_api.native_tools_ok(replace(config, model="qwen3.6-27b")))
            self.assertEqual(native.tool_format, TOOL_FORMAT_NATIVE)


class ClientProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _Server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def setUp(self) -> None:
        self.server.replies, self.server.requests = [], []
        run_stats.reset()
        self.home = _UserDir()
        self.home.__enter__()

    def tearDown(self) -> None:
        self.home.__exit__()

    def _request(self, config: BotConfig, tools: list | None = None) -> dict:
        LocalModelClient(config).chat([{"role": "user", "content": "hi"}], tools=tools)
        return self.server.requests[-1]

    def test_sampling_and_max_tokens_come_from_the_profile_and_config_wins(self) -> None:
        body = self._request(_config(self.server.url, "qwen3.6-27b"))
        self.assertEqual((body["temperature"], body["top_p"], body["top_k"], body["presence_penalty"]),
                         (0.6, 0.95, 20, 0.0))
        self.assertNotIn("max_tokens", body)
        body = self._request(_config(self.server.url, "qwen3.6-27b", temperature=0.1, max_tokens=2048))
        self.assertEqual((body["temperature"], body["top_k"], body["max_tokens"]), (0.1, 20, 2048))
        body = self._request(_config(self.server.url, "unknown-model"))
        self.assertFalse({"temperature", "top_p", "top_k", "max_tokens"} & set(body))

    def test_profile_max_tokens_and_parallel_tool_calls(self) -> None:
        with _UserDir({"p.json": {"id": "capped", "match": ["capped*"], "max_tokens": 1234,
                                  "parallel_tool_calls": False}}):
            body = self._request(_config(self.server.url, "capped-7b"), tools=TOOLS)
            self.assertEqual((body["max_tokens"], body["parallel_tool_calls"]), (1234, False))
            body = self._request(_config(self.server.url, "capped-7b", max_tokens=99))
            self.assertEqual(body["max_tokens"], 99)
            self.assertNotIn("parallel_tool_calls", body)  # only with tools
        self.assertNotIn("parallel_tool_calls", self._request(_config(self.server.url, "qwen3.6-27b"), tools=TOOLS))

    def _two_turns(self, model: str) -> list[dict]:
        self.server.replies = [
            [{"choices": [{"index": 0, "delta": {"reasoning_content": "I should read a.py."}}]},
             *_call_chunks([("c1", "read_files", {"paths": ["a.py"]})])],
            [{"choices": [{"index": 0, "delta": {"content": "COMPLETED"}}]}],
        ]
        session = LocalModelSession(LocalModelClient(_config(self.server.url, model)))
        session.use_tools(TOOLS)
        session.send("Task: look")
        session.send(json.dumps({"input": [{"type": "function_call_output", "call_id": "c1",
                                            "output": {"ok": True, "output": "A"}}]}))
        return self.server.requests[1]["messages"]

    def test_preserve_thinking_keeps_reasoning_in_native_history(self) -> None:
        sent = self._two_turns("qwen3.6-27b")
        self.assertEqual(sent[1]["reasoning_content"], "I should read a.py.")

    def test_without_preserve_thinking_reasoning_is_never_echoed(self) -> None:
        sent = self._two_turns("deepseek-r1-distill-qwen-32b")
        self.assertNotIn("reasoning_content", sent[1])
        self.assertNotIn("I should read", json.dumps(sent))

    def test_effective_context_caps_the_server_window(self) -> None:
        _reported_ctx.clear()
        url = self.server.url
        _reported_ctx[url + "|qwen3.6-27b"] = 131072
        _reported_ctx[url + "|unknown-model"] = 131072
        try:
            self.assertEqual(model_api.context_tokens(_config(url, "qwen3.6-27b")), 32768)
            self.assertEqual(model_api.context_tokens(_config(url, "unknown-model")), 131072)
            # the person's own num_ctx wins
            self.assertEqual(model_api.context_tokens(_config(url, "qwen3.6-27b", num_ctx=65536)), 65536)
            _reported_ctx[url + "|qwen3.6-27b"] = 16384
            self.assertEqual(model_api.context_tokens(_config(url, "qwen3.6-27b")), 16384)
        finally:
            _reported_ctx.clear()


if __name__ == "__main__":
    unittest.main()
