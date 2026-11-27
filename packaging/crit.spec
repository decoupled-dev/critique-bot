# -*- mode: python ; coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
"""PyInstaller spec for crit (Windows and Linux).

Build from the repo root:

    python -m PyInstaller packaging/crit.spec --noconfirm --clean
"""

import sys
from pathlib import Path

from PyInstaller.compat import is_win
from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parent
# collect_submodules("critique_bot") below imports the package; work from a plain checkout too.
sys.path.insert(0, str(ROOT / "src"))

datas = []
binaries = []
hiddenimports = []

for package in ("playwright", "pyee"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

binaries += collect_dynamic_libs("greenlet")
# rich loads unicode tables and pygments lexers/styles by name at runtime.
for package in ("rich", "pygments", "prompt_toolkit"):
    hiddenimports += collect_submodules(package)
datas += copy_metadata("playwright")
datas += copy_metadata("greenlet")

try:
    import playwright

    driver_dir = Path(playwright.__file__).resolve().parent / "driver"
    node_name = "node.exe" if is_win else "node"
    node_path = driver_dir / node_name
    if node_path.is_file():
        binaries.append((str(node_path), "playwright/driver"))
    if driver_dir.is_dir():
        datas.append((str(driver_dir), "playwright/driver"))
except ImportError:
    pass

# Every prompt crit can load (agent.txt, agent_openai.txt, review.txt, ...): crit reads
# prompts only from its own bundle, so a missing one is a broken release.
for packaged_prompt in sorted((ROOT / "src" / "critique_bot" / "prompts").glob("*.txt")):
    datas.append((str(packaged_prompt), "critique_bot/prompts"))

# This product's license and the notices of the third-party code it bundles.
for notice in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
    if (ROOT / notice).is_file():
        datas.append((str(ROOT / notice), "."))

# Built-in skill packs (android, aosp, aaos, kotlin, aspice, ...): agent_tools.builtin_skills_dir().
for skill_md in sorted((ROOT / "src" / "critique_bot" / "skills").glob("*/SKILL.md")):
    datas.append((str(skill_md), f"critique_bot/skills/{skill_md.parent.name}"))

# Built-in model profiles and server presets: profiles.builtin_dir().
for profile_json in sorted((ROOT / "src" / "critique_bot" / "profiles").rglob("*.json")):
    relative = profile_json.parent.relative_to(ROOT / "src")
    datas.append((str(profile_json), relative.as_posix()))
# Built-in agents (one per former role): extensions.agent_files.builtin_dir().
for agent_md in sorted((ROOT / "src" / "critique_bot" / "agents").glob("*.md")):
    datas.append((str(agent_md), "critique_bot/agents"))
# Domain packs (aosp, ...): their manifest tools and agents (packs.folder()); the pack modules are
# imported by name (packs.load), so PyInstaller has to be told about them.
for pack_file in sorted((ROOT / "src" / "critique_bot" / "packs").glob("*/tools/*.json")) + sorted(
        (ROOT / "src" / "critique_bot" / "packs").glob("*/agents/*.md")):
    datas.append((str(pack_file), pack_file.parent.relative_to(ROOT / "src").as_posix()))
hiddenimports += collect_submodules("critique_bot.packs")

a = Analysis(
    [str(ROOT / "src" / "critique_bot" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports
    # Every critique_bot module: several are imported lazily, and a new one must not be missed.
    + collect_submodules("critique_bot")
    + [
        "playwright.sync_api",
        "greenlet",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="crit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="crit",
)
