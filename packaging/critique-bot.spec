# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for critique-bot (Windows and Linux).

Build from the repo root:

    python -m PyInstaller packaging/critique-bot.spec --noconfirm --clean
"""

from pathlib import Path

from PyInstaller.compat import is_win
from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs, copy_metadata

ROOT = Path(SPECPATH).resolve().parent

datas = []
binaries = []
hiddenimports = []

for package in ("playwright", "pyee"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

binaries += collect_dynamic_libs("greenlet")
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

prompts = ROOT / "prompts" / "review.txt"
if prompts.is_file():
    datas.append((str(prompts), "prompts"))
packaged_prompt = ROOT / "src" / "critique_bot" / "prompts" / "review.txt"
if packaged_prompt.is_file():
    datas.append((str(packaged_prompt), "critique_bot/prompts"))

a = Analysis(
    [str(ROOT / "src" / "critique_bot" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports
    + [
        "critique_bot",
        "critique_bot.browser",
        "critique_bot.chat_client",
        "critique_bot.cli",
        "critique_bot.config",
        "critique_bot.log",
        "critique_bot.output",
        "critique_bot.patch",
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
    name="critique-bot",
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
    name="critique-bot",
)
