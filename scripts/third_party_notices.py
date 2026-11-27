#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Write THIRD_PARTY_NOTICES.md: the license of every third-party package crit ships.

Offline: it reads the metadata of the packages installed in the Python environment that
runs it (run it with crit's own environment). Run it again after changing constraints.txt,
and commit the result.

    .venv/bin/python scripts/third_party_notices.py
"""

from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "THIRD_PARTY_NOTICES.md"
LICENSE_NAMES = re.compile(r"(?i)^(licen[cs]e|copying|notice)(\.[a-z]+)?$|^licen[cs]e[-_.]")

# Pinned in constraints.txt but never shipped: the installer's pip and the build tools.
BUILD_ONLY = frozenset({
    "pip", "pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "packaging", "setuptools",
    "pefile", "pywin32-ctypes", "build", "pyproject_hooks", "macholib", "wheel",
})
# The [index] extra: installed from PyPI by `pip install "critique-bot[index]"`, not in the
# release bundles. License per package (checked against each project's LICENSE file); the
# installed metadata wins when the package is present.
OPTIONAL_LICENSES = {
    "tree-sitter": ("MIT", "https://github.com/tree-sitter/py-tree-sitter"),
    "tree-sitter-python": ("MIT", "https://github.com/tree-sitter/tree-sitter-python"),
    "tree-sitter-java": ("MIT", "https://github.com/tree-sitter/tree-sitter-java"),
    "tree-sitter-kotlin": ("MIT", "https://github.com/fwcd/tree-sitter-kotlin"),
    "tree-sitter-go": ("MIT", "https://github.com/tree-sitter/tree-sitter-go"),
    "tree-sitter-rust": ("MIT", "https://github.com/tree-sitter/tree-sitter-rust"),
    "tree-sitter-c": ("MIT", "https://github.com/tree-sitter/tree-sitter-c"),
    "tree-sitter-cpp": ("MIT", "https://github.com/tree-sitter/tree-sitter-cpp"),
    "tree-sitter-c-sharp": ("MIT", "https://github.com/tree-sitter/tree-sitter-c-sharp"),
    "tree-sitter-javascript": ("MIT", "https://github.com/tree-sitter/tree-sitter-javascript"),
    "tree-sitter-typescript": ("MIT", "https://github.com/tree-sitter/tree-sitter-typescript"),
}


def pins() -> list[tuple[str, str]]:
    found = []
    for line in (REPO / "constraints.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "==" in line:
            name, version = line.split("==", 1)
            found.append((name.strip(), version.strip()))
    return found


def pinned_packages() -> list[str]:
    """The pinned packages crit ships: not build-only, not an optional extra."""
    return [name for name, _ in pins()
            if name.lower() not in BUILD_ONLY and name.lower() not in OPTIONAL_LICENSES]


def license_of(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    expr = meta.get("License-Expression")
    if expr:
        return expr
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    if classifiers:
        return ", ".join(classifiers)
    text = (meta.get("License") or "").strip()
    return text if text and len(text) < 80 else "see license text"


def license_texts(dist: metadata.Distribution) -> list[str]:
    texts = []
    for file in dist.files or []:
        if LICENSE_NAMES.match(Path(str(file)).name):
            try:
                texts.append(Path(dist.locate_file(file)).read_text(encoding="utf-8", errors="replace").strip())
            except OSError:
                continue
    return texts


def python_section() -> list[str]:
    lines = ["## Python packages", "", "Pinned in `constraints.txt` and installed with crit.", ""]
    missing = []
    for name in pinned_packages():
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            missing.append(name)
            continue
        url = dist.metadata.get("Home-page") or next(
            (u.split(",", 1)[-1].strip() for u in dist.metadata.get_all("Project-URL") or []), ""
        )
        lines += [f"### {dist.metadata['Name']} {dist.version}", "", f"License: {license_of(dist)}  "]
        if url:
            lines.append(f"Source: {url}")
        lines.append("")
        for text in license_texts(dist):
            lines += ["```", text, "```", ""]
    if missing:
        sys.exit(f"not installed in this environment: {', '.join(missing)}; run with crit's own Python")
    return lines


def optional_section() -> list[str]:
    lines = [
        "## Optional extras (not in the release bundles)",
        "",
        "The `[index]` extra (tree-sitter and its grammars) is installed from PyPI only when",
        "asked for; each package carries its own license text. Pinned in `constraints.txt`.",
        "",
        "| Package | Version | License | Source |",
        "| --- | --- | --- | --- |",
    ]
    for name, version in pins():
        if name.lower() not in OPTIONAL_LICENSES:
            continue
        license, url = OPTIONAL_LICENSES[name.lower()]
        try:
            license = license_of(metadata.distribution(name))
        except metadata.PackageNotFoundError:
            pass
        lines.append(f"| {name} | {version} | {license} | {url} |")
    missing = sorted(set(OPTIONAL_LICENSES) - {name.lower() for name, _ in pins()})
    if missing:
        sys.exit(f"OPTIONAL_LICENSES names packages constraints.txt does not pin: {', '.join(missing)}")
    return lines + [""]


def bundled_section() -> list[str]:
    return [
        "## In the release bundles only (zip, .deb)",
        "",
        "The PyInstaller bundles also carry:",
        "",
        "- **CPython**, the Python runtime: PSF-2.0 (https://docs.python.org/3/license.html).",
        "- **The PyInstaller bootloader**: GPL-2.0-or-later WITH the PyInstaller bootloader",
        "  exception, which allows shipping it with programs under any license",
        "  (https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt).",
        "- **Node.js**, inside Playwright's driver: MIT and the licenses of its bundled",
        "  components. Its full text is the Node.js part of Playwright's license above, and the",
        "  bundle keeps it as `playwright/driver/LICENSE`, with Playwright's own notices in",
        "  `playwright/driver/package/ThirdPartyNotices.txt`.",
        "",
    ]


def main() -> int:
    parts = [
        "# Third-party notices",
        "",
        "crit (critique-bot) is licensed under the Apache License 2.0 (see LICENSE and NOTICE).",
        "It ships the third-party software below. Generated by `scripts/third_party_notices.py`;",
        "do not edit by hand.",
        "",
        "Build and test tools (PyInstaller, the scanners) are not shipped and are not listed.",
        "",
    ]
    parts += python_section()
    parts += optional_section()
    parts += bundled_section()
    OUT.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
