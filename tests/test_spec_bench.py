# SPDX-License-Identifier: Apache-2.0
"""scripts/eval/spec_bench.py: the tokens-per-second math, the on/off verdict, and a run
against a fake OpenAI-compatible server with a scripted clock."""

from __future__ import annotations

import argparse
import importlib.util
import itertools
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("spec_bench", ROOT / "scripts" / "eval" / "spec_bench.py")
spec_bench = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("spec_bench", spec_bench)  # dataclasses look their module up by name
_SPEC.loader.exec_module(spec_bench)


def _sse(events: list[dict]) -> list[bytes]:
    return [b"data: " + json.dumps(e).encode() + b"\n" for e in events] + [b"data: [DONE]\n"]


def _counter(step: float = 1.0):
    ticks = itertools.count()
    return lambda: next(ticks) * step


class MathTests(unittest.TestCase):
    def test_decode_speed_excludes_the_first_token(self) -> None:
        sample = spec_bench.Sample(first_token_s=2.0, total_s=12.0, completion_tokens=501)
        self.assertEqual(sample.decode_tps, 50.0)
        self.assertAlmostEqual(sample.overall_tps, 41.75)
        self.assertEqual(spec_bench.Sample(1.0, 1.0, 10).decode_tps, 0.0)
        self.assertEqual(spec_bench.Sample(0.0, 0.0, 0).overall_tps, 0.0)

    def test_measure_uses_usage_then_timings_then_deltas(self) -> None:
        events = [{"choices": [{"delta": {"content": "a"}}]}, {"choices": [{"delta": {"content": "b"}}]},
                  {"choices": [], "usage": {"completion_tokens": 101}}]
        # clock: start 0, first token 1, end 2
        sample = spec_bench.measure(_sse(events), _counter())
        self.assertEqual((sample.first_token_s, sample.total_s, sample.completion_tokens), (1.0, 2.0, 101))
        self.assertEqual(sample.decode_tps, 100.0)
        timed = [{"choices": [{"delta": {"content": "a"}}]},
                 {"choices": [], "timings": {"predicted_n": 41, "predicted_per_second": 37.5}}]
        sample = spec_bench.measure(_sse(timed), _counter(0.5))
        self.assertEqual((sample.completion_tokens, sample.server_tps, sample.decode_tps), (41, 37.5, 80.0))
        bare = [{"choices": [{"delta": {"content": "x"}}]}] * 3
        self.assertEqual(spec_bench.measure(_sse(bare), _counter()).completion_tokens, 3)

    def test_compare_verdict(self) -> None:
        def result(rewrite: float, normal: float) -> dict:
            return {"summary": {"rewrite": {"decode_tps": rewrite}, "normal": {"decode_tps": normal}}}

        keep = spec_bench.compare(result(20, 30), result(40, 29))
        self.assertEqual(keep, {"rewrite_speedup": 2.0, "normal_slowdown": 0.0333, "keep": True})
        self.assertFalse(spec_bench.compare(result(20, 30), result(25, 30))["keep"])  # 1.25x only
        self.assertFalse(spec_bench.compare(result(20, 30), result(40, 27))["keep"])  # normal 10% slower

    def test_synthetic_file_has_the_asked_size(self) -> None:
        text = spec_bench.synthetic_file(600)
        self.assertEqual(text.count("\n"), 600)
        self.assertIn("def compute_1(", text)


class FakeServerTests(unittest.TestCase):
    def test_run_against_a_fake_server(self) -> None:
        requests: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                tokens = 400 if "Rewrite" in body["messages"][-1]["content"] else 40
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for line in _sse([{"choices": [{"delta": {"content": "x"}}]},
                                  {"choices": [], "usage": {"completion_tokens": tokens + 1}}]):
                    self.wfile.write(line + b"\n")

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{httpd.server_address[1]}"
            client = spec_bench.Client(url, "m")
            original = client.run
            client.run = lambda messages, max_tokens: original(messages, max_tokens, clock=_counter())
            with tempfile.TemporaryDirectory() as tmp:
                args = argparse.Namespace(url=url, model="m", api_key="", file="", lines=50, runs=2,
                                          rewrite_max_tokens=5000, normal_max_tokens=100, label="off",
                                          out=str(Path(tmp) / "off.json"))
                result = spec_bench.run(args, client)
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual(len(requests), 4)
        self.assertEqual({r["max_tokens"] for r in requests}, {5000, 100})
        self.assertTrue(all(r["stream"] and r["temperature"] == 0 for r in requests))
        # scripted clock: first token after 1s, end after 2s -> tokens per second = tokens
        self.assertEqual(result["summary"]["rewrite"]["decode_tps"], 400.0)
        self.assertEqual(result["summary"]["normal"]["decode_tps"], 40.0)
        self.assertEqual(result["summary"]["rewrite"]["runs"], 2)
        self.assertEqual(result["source_lines"], 50)


if __name__ == "__main__":
    unittest.main()
