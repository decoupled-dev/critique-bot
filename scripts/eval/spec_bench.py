#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Speculative-decoding benchmark: decode speed for a whole-file rewrite vs a normal short turn.

Whole-file rewrites mostly copy text already in the prompt, which n-gram / prompt-lookup
speculation (or a draft model) can speed up; a normal agent turn should not get slower.
Run it once with speculation off and once with it on (same server, model and settings),
then compare (plan/01-model-protocol.md, work item 12; keep speculation in a preset only
when rewrites get >= 1.5x faster and normal turns slow down by <= 5%):

    python scripts/eval/spec_bench.py run --url http://127.0.0.1:8080 --model M --label off --out off.json
    # restart the server with speculation on
    python scripts/eval/spec_bench.py run --url http://127.0.0.1:8080 --model M --label on --out on.json
    python scripts/eval/spec_bench.py compare off.json on.json

Speeds are completion tokens per second after the first token (decode speed), from the
server's usage counts (or llama.cpp's timings), taking the median over ``--runs``. Only the
standard library is used; the API key comes from ``--api-key`` or ``CRIT_API_KEY``.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

#: Rewrites must get at least this much faster, and normal turns may lose at most this much.
MIN_REWRITE_SPEEDUP = 1.5
MAX_NORMAL_SLOWDOWN = 0.05


@dataclass
class Sample:
    """One streamed reply: seconds to the first token, seconds in all, completion tokens."""

    first_token_s: float
    total_s: float
    completion_tokens: int
    #: The server's own decode speed when it reports one (llama.cpp ``timings.predicted_per_second``).
    server_tps: float | None = None

    @property
    def decode_tps(self) -> float:
        """Tokens per second after the first token (0 when it cannot be computed)."""
        span = self.total_s - self.first_token_s
        if self.completion_tokens <= 1 or span <= 0:
            return 0.0
        return (self.completion_tokens - 1) / span

    @property
    def overall_tps(self) -> float:
        return self.completion_tokens / self.total_s if self.total_s > 0 else 0.0


def synthetic_file(lines: int = 600) -> str:
    """A Python file of about ``lines`` lines with repeated but not identical functions."""
    out = ['"""A synthetic module for the rewrite benchmark."""', "", "import math", ""]
    index = 0
    while len(out) < lines:
        index += 1
        out += [
            "",
            f"def compute_{index}(values, scale={index}):",
            f'    """Scale, square and sum values (variant {index})."""',
            "    total = 0",
            "    for value in values:",
            f"        total += math.pow(value * scale, 2) + {index}",
            "    return total",
        ]
    return "\n".join(out[:lines]) + "\n"


def rewrite_messages(source: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "You are a careful coding assistant. Output only code."},
        {"role": "user", "content": (
            "Rewrite this whole file with every function name changed from compute_N to calculate_N. "
            "Change nothing else. Output the complete file and nothing else.\n\n```python\n" + source + "```")},
    ]


def normal_messages() -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "You are a coding assistant."},
        {"role": "user", "content": (
            "In three sentences: what is the difference between a list and a tuple in Python, "
            "and when would you pick each one?")},
    ]


def _events(lines: Iterable[bytes]) -> Iterable[dict[str, Any]]:
    for raw in lines:
        text = raw.decode("utf-8", "replace").strip()
        if not text.startswith("data:"):
            continue
        text = text[5:].strip()
        if text == "[DONE]":
            return
        try:
            event = json.loads(text)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def measure(lines: Iterable[bytes], clock: Callable[[], float] = time.monotonic, started: float | None = None) -> Sample:
    """Time one server-sent-event stream: when it started (``started``, else now), its first
    content token, its end."""
    started = clock() if started is None else started
    first: float | None = None
    deltas = 0
    usage_tokens: int | None = None
    server_tps: float | None = None
    for event in _events(lines):
        for choice in event.get("choices") or []:
            delta = choice.get("delta") if isinstance(choice, dict) else None
            if isinstance(delta, dict) and (delta.get("content") or delta.get("reasoning_content")
                                            or delta.get("reasoning")):
                deltas += 1
                if first is None:
                    first = clock()
        usage = event.get("usage")
        if isinstance(usage, dict) and isinstance(usage.get("completion_tokens"), int):
            usage_tokens = usage["completion_tokens"]
        timings = event.get("timings")
        if isinstance(timings, dict):
            if usage_tokens is None and isinstance(timings.get("predicted_n"), int):
                usage_tokens = timings["predicted_n"]
            if isinstance(timings.get("predicted_per_second"), (int, float)):
                server_tps = float(timings["predicted_per_second"])
    ended = clock()
    first_s = (first if first is not None else ended) - started
    return Sample(first_token_s=first_s, total_s=ended - started,
                  completion_tokens=usage_tokens if usage_tokens is not None else deltas, server_tps=server_tps)


class Client:
    """``POST /v1/chat/completions`` streamed, with http.client (never through a proxy)."""

    def __init__(self, url: str, model: str, api_key: str = "", timeout: float = 600.0) -> None:
        parts = urlsplit(url.rstrip("/"))
        self.https = parts.scheme == "https"
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or (443 if self.https else 80)
        self.prefix = parts.path.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def run(self, messages: list[dict[str, str]], max_tokens: int, clock: Callable[[], float] = time.monotonic) -> Sample:
        body = {"model": self.model, "messages": messages, "stream": True, "max_tokens": max_tokens,
                "temperature": 0, "stream_options": {"include_usage": True}}
        cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
        conn = cls(self.host, self.port, timeout=self.timeout)
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            started = clock()  # before the request: the first-token time includes prompt processing
            conn.request("POST", self.prefix + "/v1/chat/completions", body=json.dumps(body).encode(), headers=headers)
            response = conn.getresponse()
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}: {response.read()[:300]!r}")
            return measure(iter(response.readline, b""), clock, started)
        finally:
            conn.close()


def median_sample(samples: list[Sample]) -> dict[str, float]:
    return {
        "decode_tps": statistics.median(s.decode_tps for s in samples),
        "overall_tps": statistics.median(s.overall_tps for s in samples),
        "first_token_s": statistics.median(s.first_token_s for s in samples),
        "completion_tokens": statistics.median(s.completion_tokens for s in samples),
        "runs": len(samples),
    }


def compare(off: dict[str, Any], on: dict[str, Any]) -> dict[str, Any]:
    """Speculation on vs off: the rewrite speedup, the normal-turn change, and the verdict."""
    def tps(result: dict[str, Any], kind: str) -> float:
        return float(result["summary"][kind]["decode_tps"])

    rewrite_off, rewrite_on = tps(off, "rewrite"), tps(on, "rewrite")
    normal_off, normal_on = tps(off, "normal"), tps(on, "normal")
    speedup = rewrite_on / rewrite_off if rewrite_off > 0 else 0.0
    slowdown = 1.0 - normal_on / normal_off if normal_off > 0 else 1.0
    return {
        "rewrite_speedup": round(speedup, 3),
        "normal_slowdown": round(slowdown, 4),
        "keep": speedup >= MIN_REWRITE_SPEEDUP and slowdown <= MAX_NORMAL_SLOWDOWN,
    }


def run(args: argparse.Namespace, client: Client | None = None) -> dict[str, Any]:
    client = client or Client(args.url, args.model, args.api_key or os.environ.get("CRIT_API_KEY", ""))
    source = Path(args.file).read_text(encoding="utf-8") if args.file else synthetic_file(args.lines)
    kinds = {"rewrite": (rewrite_messages(source), args.rewrite_max_tokens),
             "normal": (normal_messages(), args.normal_max_tokens)}
    samples: dict[str, list[Sample]] = {kind: [] for kind in kinds}
    for _ in range(args.runs):
        for kind, (messages, max_tokens) in kinds.items():
            samples[kind].append(client.run(messages, max_tokens))
    return {
        "label": args.label, "url": args.url, "model": args.model, "source_lines": source.count("\n"),
        "samples": {k: [asdict(s) | {"decode_tps": s.decode_tps} for s in v] for k, v in samples.items()},
        "summary": {k: median_sample(v) for k, v in samples.items()},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("run", help="measure one server setting")
    one.add_argument("--url", required=True, help="server base URL, e.g. http://127.0.0.1:8080")
    one.add_argument("--model", required=True)
    one.add_argument("--api-key", default="")
    one.add_argument("--file", default="", help="file to rewrite (default: a synthetic file of --lines lines)")
    one.add_argument("--lines", type=int, default=600)
    one.add_argument("--runs", type=int, default=3)
    one.add_argument("--rewrite-max-tokens", type=int, default=12000)
    one.add_argument("--normal-max-tokens", type=int, default=300)
    one.add_argument("--label", default="")
    one.add_argument("--out", default="", help="write the result JSON here")
    two = sub.add_parser("compare", help="speculation off vs on")
    two.add_argument("off")
    two.add_argument("on")
    args = parser.parse_args(argv)
    if args.command == "compare":
        off = json.loads(Path(args.off).read_text(encoding="utf-8"))
        on = json.loads(Path(args.on).read_text(encoding="utf-8"))
        verdict = compare(off, on)
        print(json.dumps(verdict, indent=2))
        return 0 if verdict["keep"] else 1
    result = run(args)
    text = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    for kind, summary in result["summary"].items():
        print(f"{kind:8} {summary['decode_tps']:8.1f} tok/s decode, first token {summary['first_token_s']:.2f}s, "
              f"{summary['completion_tokens']:.0f} tokens (median of {summary['runs']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
