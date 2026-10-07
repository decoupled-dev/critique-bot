from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from critique_bot.agent_tools import canonical_tool
from critique_bot.code_graph import (
    _command,
    _extract,
    _verify_sha256,
    archive_name,
    detect,
    hint,
    install_bundle,
    launcher,
    platform_target,
    prepare,
    query,
    release_url,
    sync_after_edit,
    vendored_archive,
)


class _Proc:
    def __init__(self, text: str, code: int = 0) -> None:
        self.returncode = code
        self.stdout = text.encode("utf-8")
        self.stderr = b""


class CodeGraphTests(unittest.TestCase):
    def test_hint_names_the_installed_backend(self) -> None:
        def find(name: str) -> str | None:
            return "/bin/codegraph" if name == "codegraph" else None

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIn("built on the first task", hint(root, which=find))
            (root / ".codegraph").mkdir()
            self.assertIn("ready", hint(root, which=find))

        def only_graphify(name: str) -> str | None:
            return "/bin/graphify" if name == "graphify" else None

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIn("no graphify-out", hint(root, which=only_graphify))
            graph = root / "graphify-out" / "graph.json"
            graph.parent.mkdir()
            graph.write_text("{}", encoding="utf-8")
            self.assertIn("graphify-out/graph.json", hint(root, which=only_graphify))

        self.assertIn("not installed", hint(Path("."), which=lambda _name: None))

    def test_prepare_inits_once_then_syncs(self) -> None:
        seen: list[list[str]] = []

        def runner(argv, **kwargs):
            del kwargs
            seen.append(argv)
            if argv[1] == "init":
                (Path(argv[2]) / ".codegraph").mkdir()
            return _Proc("ok")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            find = lambda name: "/bin/codegraph" if name == "codegraph" else None  # noqa: E731
            self.assertIn("built", prepare(root, runner=runner, which=find))
            self.assertEqual(seen[0][:2], ["/bin/codegraph", "init"])
            self.assertIn("synced", prepare(root, runner=runner, which=find))
            self.assertEqual(seen[1][:2], ["/bin/codegraph", "sync"])

    def test_explore_returns_the_graph_text(self) -> None:
        def runner(argv, **kwargs):
            del kwargs
            self.assertEqual(argv[:2], ["/bin/codegraph", "explore"])
            self.assertIn("OrderService", argv[2])
            return _Proc("OrderService calls place\nsrc/Order.java:40")

        with tempfile.TemporaryDirectory() as tmp:
            code, text = query(
                Path(tmp),
                "how does OrderService place an order",
                runner=runner,
                which=lambda name: "/bin/codegraph" if name == "codegraph" else None,
            )
        self.assertEqual(code, 0)
        self.assertIn("src/Order.java:40", text)
        self.assertEqual(canonical_tool("codegraph_explore"), "code_graph")
        self.assertEqual(canonical_tool("graphify"), "code_graph")

    def test_missing_cli_points_back_at_search(self) -> None:
        code, text = query(Path("."), "OrderService", which=lambda _name: None)
        self.assertEqual(code, 127)
        self.assertIn("search_code", text)

    def test_edit_syncs_only_after_the_graph_exists(self) -> None:
        seen: list[list[str]] = []

        def runner(argv, **kwargs):
            del kwargs
            seen.append(argv)
            return _Proc("ok")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sync_after_edit(root, runner=runner, which=lambda name: "/bin/codegraph" if name == "codegraph" else None)
            self.assertEqual(seen, [])
            (root / ".codegraph").mkdir()
            sync_after_edit(root, runner=runner, which=lambda name: "/bin/codegraph" if name == "codegraph" else None)
            self.assertEqual(seen[0][:2], ["/bin/codegraph", "sync"])

    def test_graphify_path_uses_both_names(self) -> None:
        def runner(argv, **kwargs):
            del kwargs
            self.assertEqual(argv, ["graphify", "path", "FastAPI", "ModelField"])
            return _Proc("FastAPI --uses--> ModelField")

        code, text = query(
            Path("."),
            "FastAPI",
            action="path",
            target="ModelField",
            runner=runner,
            which=lambda name: "/bin/graphify" if name == "graphify" else None,
        )
        self.assertEqual(code, 0)
        self.assertIn("ModelField", text)

    def test_the_app_includes_codegraph_without_a_separate_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("included with this app", hint(Path(tmp)))
        self.assertEqual(detect(), "codegraph")

    def test_release_names_match_the_official_installer(self) -> None:
        self.assertEqual(archive_name("win32-x64"), "codegraph-win32-x64.zip")
        self.assertEqual(archive_name("win32-arm64"), "codegraph-win32-arm64.zip")
        self.assertEqual(archive_name("linux-x64"), "codegraph-linux-x64.tar.gz")
        self.assertEqual(
            release_url("1.6.2", "darwin-arm64"),
            "https://github.com/colbymchenry/codegraph/releases/download/v1.6.2/codegraph-darwin-arm64.tar.gz",
        )
        with self.assertRaises(OSError):
            release_url("../secret", "linux-x64")

    def test_extract_hoists_the_archive_and_drops_escape_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "codegraph-linux-x64.tar.gz"
            payload = b"#!/bin/sh\necho ok\n"
            with tarfile.open(archive, "w:gz") as bundle:
                info = tarfile.TarInfo("codegraph-linux-x64/bin/codegraph")
                info.size = len(payload)
                info.mode = 0o755
                bundle.addfile(info, io.BytesIO(payload))
                escaped = tarfile.TarInfo("../outside.txt")
                escaped.size = 4
                bundle.addfile(escaped, io.BytesIO(b"nope"))
            dest = root / "current"
            _extract(archive, dest)
            found = launcher(dest)
            self.assertIsNotNone(found)
            assert found is not None
            self.assertEqual(found.read_bytes(), payload)
            self.assertFalse((root / "outside.txt").exists())
            self.assertFalse((dest / "outside.txt").exists())

    def test_install_uses_the_archive_shipped_in_the_repo(self) -> None:
        archive = vendored_archive(platform_target())
        self.assertTrue(archive.is_file())
        self.assertIn("vendor/codegraph/v1.6.2", archive.as_posix())
        with tempfile.TemporaryDirectory() as tmp:
            found = install_bundle(Path(tmp) / "current")
            self.assertTrue(Path(found).is_file())
            self.assertEqual(Path(found).name, "codegraph")
        with self.assertRaises(OSError) as missing:
            vendored_archive(platform_target(), "9.9.9")
        self.assertIn("not downloaded", str(missing.exception))

    def test_checksum_rejects_a_changed_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "codegraph-linux-x64.tar.gz"
            archive.write_bytes(b"not the release")
            (root / "SHA256SUMS").write_text("abc  codegraph-linux-x64.tar.gz\n", encoding="utf-8")
            with self.assertRaises(OSError) as bad:
                _verify_sha256(archive)
            self.assertIn("failed its checksum", str(bad.exception))

    def test_windows_launcher_goes_through_cmd(self) -> None:
        self.assertEqual(
            _command(r"C:\app\bin\codegraph.cmd", ["explore", "main"]),
            ["cmd.exe", "/d", "/s", "/c", r"C:\app\bin\codegraph.cmd", "explore", "main"],
        )
