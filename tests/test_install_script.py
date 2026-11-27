# SPDX-License-Identifier: Apache-2.0
"""scripts/install.sh checks build provenance (SF-12), run offline against a fake release.

``curl`` copies files from a local release folder, ``gh`` is a stand-in whose sign-in and
``attestation verify`` results each test sets, and the "crit" in the zip only writes a marker
file. PATH holds just these and the few system tools the script uses, so a real ``gh`` on
this machine is never called.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install.sh"
TOOLS = ("sh", "grep", "sed", "head", "find", "mktemp", "rm", "chmod", "unzip", "sha256sum", "cat", "uname", "cp")
VERSION = "9.9.9"

FAKE_CURL = """#!/bin/sh
out=""
url=""
while [ $# -gt 0 ]; do
    case "$1" in
        -o) out="$2"; shift 2 ;;
        -*) shift ;;
        *) url="$1"; shift ;;
    esac
done
name="${url##*/}"
[ -f "$FAKE_RELEASE/$name" ] || exit 22
cp "$FAKE_RELEASE/$name" "$out"
"""

FAKE_GH = """#!/bin/sh
echo "$*" >> "$FAKE_GH_LOG"
case "$1 $2" in
    "auth status") exit "${FAKE_GH_AUTH:-0}" ;;
    "attestation verify") exit "${FAKE_GH_VERIFY:-0}" ;;
esac
exit 2
"""


def _machine() -> str:
    return "arm64" if os.uname().machine in {"aarch64", "arm64"} else "x64"


@unittest.skipUnless(sys.platform.startswith("linux") and all(shutil.which(tool) for tool in TOOLS),
                     "needs Linux with sh, unzip and sha256sum")
class InstallScriptProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="crit-install-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.release = self.base / "release"
        self.release.mkdir()
        self.marker = self.base / "installed"
        crit = f"#!/bin/sh\necho \"$@\" > '{self.marker}'\n"
        zip_name = f"crit-{VERSION}-linux-{_machine()}.zip"
        with zipfile.ZipFile(self.release / zip_name, "w") as archive:
            info = zipfile.ZipInfo("crit/crit")
            info.external_attr = 0o755 << 16
            archive.writestr(info, crit)
        digest = hashlib.sha256((self.release / zip_name).read_bytes()).hexdigest()
        (self.release / "SHA256SUMS.txt").write_text(f"{digest}  {zip_name}\n", encoding="utf-8")
        self.bin = self.base / "bin"
        self.bin.mkdir()
        for tool in TOOLS:
            (self.bin / tool).symlink_to(shutil.which(tool))
        (self.bin / "curl").write_text(FAKE_CURL, encoding="utf-8")
        (self.bin / "curl").chmod(0o755)
        self.gh_log = self.base / "gh.log"

    def run_script(self, *, gh: bool, auth: int = 0, verify: int = 0, **env: str) -> subprocess.CompletedProcess:
        if gh:
            (self.bin / "gh").write_text(FAKE_GH, encoding="utf-8")
            (self.bin / "gh").chmod(0o755)
        environ = {"PATH": str(self.bin), "HOME": str(self.base), "TMPDIR": str(self.base), "CRIT_VERSION": VERSION,
                   "FAKE_RELEASE": str(self.release), "FAKE_GH_LOG": str(self.gh_log), "FAKE_GH_AUTH": str(auth),
                   "FAKE_GH_VERIFY": str(verify), **env}
        return subprocess.run(["sh", str(SCRIPT)], env=environ, capture_output=True, text=True, timeout=60, check=False)

    def test_a_verified_build_installs(self) -> None:
        done = self.run_script(gh=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(self.marker.exists())
        self.assertIn("Build provenance OK", done.stdout)
        calls = self.gh_log.read_text(encoding="utf-8")
        self.assertIn("attestation verify", calls)
        self.assertIn("--repo decoupled-dev/critique-bot", calls)

    def test_a_failed_check_installs_nothing(self) -> None:
        done = self.run_script(gh=True, verify=1)
        self.assertEqual(done.returncode, 1)
        self.assertFalse(self.marker.exists())
        self.assertIn("FAILED", done.stderr)
        self.assertIn("CRIT_SKIP_ATTESTATION=1", done.stderr)

    def test_the_override_is_explicit_and_said(self) -> None:
        done = self.run_script(gh=True, verify=1, CRIT_SKIP_ATTESTATION="1")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(self.marker.exists())
        self.assertIn("NOT verified", done.stderr)

    def test_without_gh_it_says_so_and_can_be_required(self) -> None:
        done = self.run_script(gh=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("not verified", done.stderr)
        self.assertIn("gh attestation verify", done.stderr)
        self.marker.unlink()
        strict = self.run_script(gh=False, CRIT_REQUIRE_ATTESTATION="1")
        self.assertEqual(strict.returncode, 1)
        self.assertFalse(self.marker.exists())

    def test_gh_that_is_not_signed_in_counts_as_missing(self) -> None:
        done = self.run_script(gh=True, auth=1)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("attestation verify", self.gh_log.read_text(encoding="utf-8"))

    def test_a_checksum_mismatch_still_stops_first(self) -> None:
        (self.release / "SHA256SUMS.txt").write_text(f"{'0' * 64}  crit-{VERSION}-linux-{_machine()}.zip\n",
                                                     encoding="utf-8")
        done = self.run_script(gh=True)
        self.assertEqual(done.returncode, 1)
        self.assertIn("Checksum mismatch", done.stderr)
        self.assertFalse(self.gh_log.exists() and "attestation" in self.gh_log.read_text(encoding="utf-8"))


class WorkflowAttestsTheChecksumsTest(unittest.TestCase):
    def test_sha256sums_is_an_attestation_subject(self) -> None:
        workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
        subjects = workflow.split("subject-path:", 1)[1].split("- name:", 1)[0]
        self.assertIn("SHA256SUMS.txt", subjects)

    def test_the_windows_installer_checks_provenance_too(self) -> None:
        text = (SCRIPT.parent / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("gh attestation verify", text)
        self.assertIn("CRIT_SKIP_ATTESTATION", text)
        self.assertIn("CRIT_REQUIRE_ATTESTATION", text)


class SupplyChainPinsTest(unittest.TestCase):
    """SF-13: CI tools install from hash-pinned files, and every container image is pinned by digest."""

    ROOT = Path(__file__).resolve().parents[1]

    def test_ci_tool_installs_require_hashes(self) -> None:
        import re

        workflow = (self.ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
        for line in workflow.splitlines():
            if "pip install" in line and "-e " not in line and "-e\"" not in line:
                self.assertIn("--require-hashes", line, line.strip())
        for path in sorted((self.ROOT / ".github" / "requirements").glob("*.txt")):
            text = path.read_text(encoding="utf-8")
            pins = re.findall(r"^[A-Za-z0-9_.-]+==\S+ \\$", text, re.MULTILINE)
            hashed = re.findall(r"^[A-Za-z0-9_.-]+==\S+ \\\n(?:    --hash=sha256:[0-9a-f]{64}(?: \\)?\n)+", text,
                                re.MULTILINE)
            self.assertTrue(pins, path.name)
            self.assertEqual(len(pins), len(hashed), f"{path.name}: every pin has its hashes")

    def test_container_images_are_pinned_by_digest(self) -> None:
        import re

        workflow = (self.ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
        images = re.findall(r"(?:ubuntu|python|debian):[\w.-]+(?:@sha256:[0-9a-f]{64})?", workflow)
        self.assertTrue(images)
        for image in images:
            self.assertIn("@sha256:", image, image)

if __name__ == "__main__":
    unittest.main()
