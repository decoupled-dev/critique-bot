#!/usr/bin/env python3
"""Package a Linux bundle from scripts/build.py as a .deb (amd64 or arm64).

Run on Linux after scripts/build.py, with dpkg-deb installed (Debian and Ubuntu have it):

    python scripts/build.py
    python scripts/build_deb.py

Install with ``sudo apt install ./crit_<version>_<arch>.deb``. The package puts
the bundle in /opt/crit, adds ``crit`` to /usr/bin, and ships the worker service
``crit-worker.service`` (not enabled, runs as the system user ``crit``) with its config at
/etc/crit/config.json. /etc/crit/crit.json is where IT may put the shared config file.
The package replaces the old ``critique-bot`` package (Replaces/Conflicts/Provides).
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
sys.path.insert(0, str(ROOT / "scripts"))

from build import _platform_tag, _version  # noqa: E402

_DEB_ARCH = {"x64": "amd64", "arm64": "arm64"}

_CONTROL = """Package: crit
Version: {version}
Section: devel
Priority: optional
Architecture: {arch}
Maintainer: crit maintainers <noreply@invalid>
Depends: libc6 (>= {glibc})
Replaces: critique-bot
Conflicts: critique-bot
Provides: critique-bot
Recommends: microsoft-edge-stable | google-chrome-stable | chromium | chromium-browser
Description: Code review and coding agent that drives an AI service's web page
 crit sends only the conversation to the url in config.json (strict egress by
 default), redacts secrets, personal data and configured company names, and keeps
 every browser and tool telemetry switch off.
"""

_WRAPPER = """#!/bin/sh
exec /opt/crit/crit "$@"
"""

_POSTINST = """#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    if ! getent passwd crit >/dev/null; then
        useradd --system --home-dir /var/lib/crit --create-home \\
            --shell /usr/sbin/nologin crit
    fi
    install -d -o crit -g crit -m 0750 /var/lib/crit
    # 0755: every user's crit reads the config file /etc/crit/crit.json (0644) here;
    # the worker's config.json stays 0640.
    install -d -o root -g crit -m 0755 /etc/crit
    if [ ! -e /etc/crit/config.json ]; then
        if [ -f /etc/critique-bot/config.json ]; then
            # Upgrading from the critique-bot package: keep the worker's settings.
            install -o root -g crit -m 0640 /etc/critique-bot/config.json /etc/crit/config.json
        else
            install -o root -g crit -m 0640 /usr/share/crit/examples/config.example.json /etc/crit/config.json
        fi
    fi
    if command -v systemctl >/dev/null 2>&1; then
        systemctl daemon-reload || true
    fi
fi
exit 0
"""

_PRERM = """#!/bin/sh
set -e
if [ "$1" = "remove" ] && command -v systemctl >/dev/null 2>&1; then
    systemctl stop crit-worker.service 2>/dev/null || true
    systemctl disable crit-worker.service 2>/dev/null || true
fi
exit 0
"""


def _build_glibc() -> str:
    """The glibc of the machine that built the bundle: the oldest the program can run on.

    PyInstaller bundles carry the build machine's libpython, which needs that glibc or newer,
    so the package must not promise less (release builds use Ubuntu 22.04: glibc 2.35).
    """
    import platform

    name, version = platform.libc_ver()
    if name != "glibc" or not version:
        raise SystemExit("could not tell the glibc version of this machine")
    return version


def _commit_time() -> str:
    """The last commit's time, so file dates in the package are reproducible and real."""
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%ct"], cwd=ROOT, capture_output=True, text=True, check=True)
        return out.stdout.strip() or "0"
    except (OSError, subprocess.CalledProcessError):
        return "0"


def _write(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(mode)


def _normalize_modes(root: Path) -> None:
    """Folders 0755, files 0644 unless executable (then 0755): no group or world write in the package."""
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        if path.is_dir():
            path.chmod(0o755)
        else:
            executable = path.stat().st_mode & stat.S_IXUSR
            path.chmod(0o755 if executable else 0o644)


def build_deb(bundle: Path, version: str, arch: str, out_dir: Path) -> Path:
    if shutil.which("dpkg-deb") is None:
        raise SystemExit("dpkg-deb is required (Debian/Ubuntu: it ships with dpkg)")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "pkg"
        shutil.copytree(bundle, root / "opt" / "crit", symlinks=True)
        unit = (ROOT / "packaging" / "crit-worker.service").read_text(encoding="utf-8")
        unit = unit.replace("--config /opt/crit/config.json", "--config /etc/crit/config.json")
        _write(root / "lib" / "systemd" / "system" / "crit-worker.service", unit, 0o644)
        (root / "opt" / "crit" / "crit-worker.service").unlink(missing_ok=True)
        _write(root / "usr" / "bin" / "crit", _WRAPPER, 0o755)
        doc = root / "usr" / "share" / "doc" / "crit"
        doc.mkdir(parents=True)
        # Not under /usr/share/doc: minimal images (Docker, cloud) never install that folder,
        # and postinst copies the worker's starter config from here.
        examples = root / "usr" / "share" / "crit" / "examples"
        examples.mkdir(parents=True)
        for name in ("config.example.json", "config-file.example.json"):
            shutil.copy2(ROOT / name, examples / name)
        for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
            if (ROOT / name).is_file():
                shutil.copy2(ROOT / name, doc / ("copyright" if name == "LICENSE" else name))
        _normalize_modes(root)
        control = root / "DEBIAN"
        control.mkdir()
        _write(control / "control", _CONTROL.format(version=version, arch=arch, glibc=_build_glibc()), 0o644)
        _write(control / "conffiles", "/lib/systemd/system/crit-worker.service\n", 0o644)
        _write(control / "postinst", _POSTINST, 0o755)
        _write(control / "prerm", _PRERM, 0o755)
        out_dir.mkdir(parents=True, exist_ok=True)
        deb = out_dir / f"crit_{version}_{arch}.deb"
        env = {**os.environ, "SOURCE_DATE_EPOCH": os.environ.get("SOURCE_DATE_EPOCH") or _commit_time()}
        subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", str(root), str(deb)], check=True, env=env)
    print(f"wrote {deb} ({deb.stat().st_size} bytes)")
    return deb


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", help="the unzipped bundle folder (default: dist/crit-<version>-<tag>)")
    args = parser.parse_args(argv)
    if not sys.platform.startswith("linux"):
        raise SystemExit("build the .deb on Linux")
    version = _version()
    tag = _platform_tag()
    arch = _DEB_ARCH.get(tag.split("-", 1)[1])
    if arch is None:
        raise SystemExit(f"no Debian architecture for {tag}")
    bundle = Path(args.bundle) if args.bundle else DIST / f"crit-{version}-{tag}"
    if not (bundle / "crit").is_file():
        raise SystemExit(f"{bundle} has no crit binary; run scripts/build.py first")
    build_deb(bundle, version, arch, DIST)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
