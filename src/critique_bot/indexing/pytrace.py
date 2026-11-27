# SPDX-License-Identifier: Apache-2.0
"""Run a Python program and record which lines of the workspace it executed.

Used by ``coverage_locate`` so it needs no coverage.py: ``python pytrace.py OUT ROOT -- ARGS``
runs ``python ARGS`` (``-m module ...`` or ``script.py ...``) and writes ``{"files": {path:
[lines]}, "exit": code}`` to OUT for files under ROOT. Standard library only; run as a
script, never imported by crit itself.
"""

from __future__ import annotations

import json
import os
import runpy
import sys
import threading


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[2] != "--":
        print("usage: pytrace.py OUT ROOT -- [-m module | script.py] args...", file=sys.stderr)
        return 2
    out, root, args = argv[0], os.path.realpath(argv[1]), argv[3:]
    seen: dict[str, set[int]] = {}
    own = os.path.realpath(__file__)
    cache: dict[str, bool] = {}

    def wanted(filename: str) -> bool:
        hit = cache.get(filename)
        if hit is None:
            if filename.startswith("<"):  # <frozen ...>, <string>
                cache[filename] = False
                return False
            real = os.path.realpath(filename)
            hit = real.startswith(root + os.sep) and real != own and "site-packages" not in real
            cache[filename] = hit
        return hit

    def tracer(frame, event, arg):
        filename = frame.f_code.co_filename
        if not wanted(filename):
            return None
        if event in {"line", "call"} and frame.f_lineno > 0:
            seen.setdefault(filename, set()).add(frame.f_lineno)
        return tracer

    # Python put this file's folder first on sys.path; its neighbours (tools.py, store.py, ...)
    # must not shadow the program's modules. Use what "python -m" / "python script.py" would.
    here = os.path.dirname(own)
    sys.path[:] = [item for item in sys.path if os.path.realpath(item or ".") != here]
    if args and args[0] == "-m":
        sys.path.insert(0, os.getcwd())
    code = 0
    sys.settrace(tracer)
    threading.settrace(tracer)
    try:
        if args and args[0] == "-m":
            sys.argv = args[1:]
            runpy.run_module(args[1], run_name="__main__", alter_sys=True)
        elif args:
            sys.argv = args
            sys.path.insert(0, os.path.dirname(os.path.abspath(args[0])))
            runpy.run_path(args[0], run_name="__main__")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except BaseException:  # the program's own failure: report it like python would
        import traceback

        traceback.print_exc()
        code = 1
    finally:
        sys.settrace(None)
        threading.settrace(None)
        files = {os.path.relpath(os.path.realpath(name), root).replace(os.sep, "/"): sorted(lines)
                 for name, lines in seen.items()}
        with open(out, "w", encoding="utf-8") as handle:
            json.dump({"files": files, "exit": code}, handle)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
