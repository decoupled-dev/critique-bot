"""Build main plus a 5-commit branch feature/telemetry; its 2nd commit wrongly adds a 3 MB
binary dump and a credentials file next to real changes, and its 4th commit edits both.

Dates and identities are fixed so hashes are stable. The expected result (the file list
of every branch commit without the two files) is written to .git/eval-expect.json for
the check; trees are compared by path and blob id, so a .gitignore change is tolerated.
"""

import json
import os
import random
import subprocess

BASE = 1_761_000_000
ENV = {**os.environ, "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
       "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com"}
BAD = ("assets/sample_dump.bin", "config/credentials.env")


def git(*args, n=0):
    env = {**ENV, "GIT_AUTHOR_DATE": f"{BASE + n * 3600} +0000", "GIT_COMMITTER_DATE": f"{BASE + n * 3600} +0000"}
    return subprocess.run(["git", *args], check=True, env=env, capture_output=True, text=True).stdout


def write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    mode = "wb" if isinstance(text, bytes) else "w"
    with open(path, mode) as f:
        f.write(text)


def commit(n, subject):
    git("add", "-A")
    git("commit", "-qm", subject, n=n)


write("telemetry/__init__.py", "")
write("README.md", "# vehicle telemetry\n\nUploads trip summaries.\n")
commit(1, "repo: skeleton")
write("telemetry/trip.py", "def summarize(points):\n    return {\"n\": len(points)}\n")
commit(2, "trip: summary of a list of points")

git("checkout", "-qb", "feature/telemetry")
write("telemetry/upload.py", "import json\n\n\ndef payload(summary):\n    return json.dumps(summary)\n")
commit(3, "telemetry: upload payload")
write("telemetry/upload.py", "import json\n\nENDPOINT = \"https://telemetry.example.invalid/v1/trips\"\n\n\n"
      "def payload(summary):\n    return json.dumps(summary, sort_keys=True)\n")
write("config/telemetry.yaml", "endpoint: https://telemetry.example.invalid/v1/trips\nbatch: 20\n")
write("assets/sample_dump.bin", random.Random(99).randbytes(3 * 1024 * 1024))
write("config/credentials.env", "API_TOKEN=FAKE_TOKEN_123\nUPLOAD_USER=test-rig\n")
commit(4, "telemetry: config and sample data")
write("telemetry/upload.py", "import json\n\nENDPOINT = \"https://telemetry.example.invalid/v1/trips\"\n\n\n"
      "def payload(summary):\n    return json.dumps(summary, sort_keys=True)\n\n\n"
      "def batches(items, size=20):\n    return [items[i:i + size] for i in range(0, len(items), size)]\n")
commit(5, "telemetry: batch uploads")
write("config/telemetry.yaml", "endpoint: https://telemetry.example.invalid/v1/trips\nbatch: 50\nretries: 3\n")
write("config/credentials.env", "API_TOKEN=FAKE_TOKEN_123\nUPLOAD_USER=test-rig\nUPLOAD_SECRET=FAKE_SECRET_456\n")
commit(6, "telemetry: retries and bigger batches")
write("tests/test_upload.py", "import unittest\n\nfrom telemetry.upload import batches\n\n\n"
      "class T(unittest.TestCase):\n    def test_batches(self):\n        self.assertEqual(len(batches(list(range(45)))), 3)\n")
commit(7, "tests: batches")

main = git("rev-parse", "main").strip()
expect = {"main": main, "commits": []}
for sha in git("rev-list", "--reverse", "main..feature/telemetry").split():
    files = {}
    for line in git("ls-tree", "-r", sha).splitlines():
        meta, path = line.split("\t", 1)
        if path not in BAD:
            files[path] = meta.split()[2]
    expect["commits"].append({"subject": git("log", "-1", "--format=%s", sha).strip(), "files": files})
git_dir = git("rev-parse", "--git-dir").strip()
with open(os.path.join(git_dir, "eval-expect.json"), "w", encoding="utf-8") as f:
    json.dump(expect, f, indent=1)
