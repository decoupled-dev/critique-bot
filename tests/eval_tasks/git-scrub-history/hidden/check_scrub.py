"""Pass when feature/telemetry's own commits no longer contain the dump or the credentials file
(in any branch or tag), everything else in those commits is unchanged, and main is untouched."""

import json
import subprocess
import sys

BAD = ("assets/sample_dump.bin", "config/credentials.env")


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout


problems = []
expect = json.load(open(git("rev-parse", "--git-dir").strip() + "/eval-expect.json"))
if git("rev-parse", "main").strip() != expect["main"]:
    problems.append("main was changed")
if git("rev-parse", "--verify", "-q", "feature/telemetry").strip() == "":
    problems.append("branch feature/telemetry is gone")
else:
    if git("merge-base", "main", "feature/telemetry").strip() != expect["main"]:
        problems.append("feature/telemetry no longer branches from main")
    shas = git("rev-list", "--reverse", "main..feature/telemetry").split()
    if len(shas) != len(expect["commits"]):
        problems.append(f"feature/telemetry has {len(shas)} own commits, expected {len(expect['commits'])}")
    for sha, want in zip(shas, expect["commits"]):
        subject = git("log", "-1", "--format=%s", sha).strip()
        if subject != want["subject"]:
            problems.append(f"commit {sha[:8]} subject {subject!r}, expected {want['subject']!r}")
        files = {}
        for line in git("ls-tree", "-r", sha).splitlines():
            meta, path = line.split("\t", 1)
            if path != ".gitignore":
                files[path] = meta.split()[2]
        if files != {p: b for p, b in want["files"].items() if p != ".gitignore"}:
            diff = sorted(set(files.items()) ^ set(want["files"].items()))
            problems.append(f"commit {subject!r} content differs: {diff[:4]}")
for ref in git("for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags").split():
    for path in BAD:
        if git("log", "--format=%h", "-1", ref, "--", path).strip():
            problems.append(f"{ref} history still contains {path}")
if git("status", "--porcelain", "--untracked-files=no").strip():
    problems.append("uncommitted changes to tracked files")
print("\n".join(dict.fromkeys(problems)) or "ok")
sys.exit(1 if problems else 0)
