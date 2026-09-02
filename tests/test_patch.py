from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from critique_bot.patch import (
    ABSOLUTE_MAX_FILE_CHARS,
    ABSOLUTE_MAX_FILES,
    ABSOLUTE_MAX_PROMPT_CHARS,
    ABSOLUTE_MAX_READ_BYTES,
    DEFAULT_MAX_FILE_CHARS,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_PROMPT_CHARS,
    DEFAULT_MAX_READ_BYTES,
    InputError,
    InputLimits,
    SanitizeStats,
    cap_text,
    changed_file_paths,
    context_file_priority,
    finalize_prompt,
    format_sanitize_note,
    load_path,
    load_stdin,
    looks_binary_bytes,
    looks_binary_path,
    looks_binary_text,
    looks_like_diff,
    sanitize_attachments,
    sanitize_one,
    strip_unsafe_controls,
)
from critique_bot.patch import (
    _binary_stub,
    _decode_bytes,
    _iter_sections,
    _path_from_section,
    _read_capped,
    _section_is_binary,
    _split_positions,
    _truncate_section,
)


GIT_DIFF = """\
diff --git a/src/app.py b/src/app.py
index 111..222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
 def main():
-    return 1
+    return 2
+    # extra
"""

UNIFIED_DIFF = """\
--- a/readme.txt
+++ b/readme.txt
@@ -1 +1 @@
-old
+new
"""

INDEX_DIFF = """\
Index: src/Foo.java
===================================================================
--- src/Foo.java
+++ src/Foo.java
@@ -1 +1 @@
-a
+b
"""


class ChangedFilePathsTests(unittest.TestCase):
    def test_git_diff_path(self) -> None:
        self.assertEqual(changed_file_paths(GIT_DIFF), ["src/app.py"])

    def test_skips_binary_and_lockfile(self) -> None:
        patch = (
            "diff --git a/photo.png b/photo.png\n"
            "Binary files a/photo.png and b/photo.png differ\n"
            "diff --git a/package-lock.json b/package-lock.json\n"
            "--- a/package-lock.json\n"
            "+++ b/package-lock.json\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
        )
        self.assertEqual(changed_file_paths(patch), ["src/app.py"])

    def test_skips_deleted_file(self) -> None:
        patch = (
            "diff --git a/gone.py b/gone.py\n"
            "deleted file mode 100644\n"
            "--- a/gone.py\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-old\n"
        )
        self.assertEqual(changed_file_paths(patch), [])

    def test_java_before_xml(self) -> None:
        self.assertLess(
            context_file_priority("a/Foo.java")[0],
            context_file_priority("res/values/strings.xml")[0],
        )


class StripControlsTests(unittest.TestCase):
    def test_empty_unchanged(self) -> None:
        self.assertEqual(strip_unsafe_controls(""), "")

    def test_keeps_tab_newline_cr(self) -> None:
        text = "a\tb\nc\rd"
        self.assertEqual(strip_unsafe_controls(text), text)

    def test_drops_nul_and_other_c0(self) -> None:
        text = "ok\x00bad\x07more\x1fend\x7f"
        self.assertEqual(strip_unsafe_controls(text), "okbadmoreend")

    def test_no_regex_hit_returns_same_object_path(self) -> None:
        text = "plain ascii"
        self.assertEqual(strip_unsafe_controls(text), text)


class LooksBinaryTests(unittest.TestCase):
    def test_path_by_extension(self) -> None:
        self.assertTrue(looks_binary_path("photo.PNG"))
        self.assertTrue(looks_binary_path('weird/"archive.zip"'))
        self.assertTrue(looks_binary_path("model.pt\tmode"))
        self.assertFalse(looks_binary_path("src/app.py"))
        self.assertFalse(looks_binary_path("README"))

    def test_bytes_empty_is_not_binary(self) -> None:
        self.assertFalse(looks_binary_bytes(b""))

    def test_bytes_nul_is_binary(self) -> None:
        self.assertTrue(looks_binary_bytes(b"hello\x00world"))

    def test_bytes_high_control_ratio(self) -> None:
        data = bytes(range(1, 32)) * 300
        self.assertTrue(looks_binary_bytes(data))

    def test_bytes_git_diff_not_binary(self) -> None:
        self.assertFalse(looks_binary_bytes(b"diff --git a/a b/a\n+ok\n"))
        self.assertFalse(looks_binary_bytes(b"--- a/x\n+++ b/x\n"))
        self.assertFalse(looks_binary_bytes(b"Index: foo\n"))
        self.assertFalse(looks_binary_bytes(b"context\n@@ -1 +1 @@\n"))

    def test_text_empty_and_nul(self) -> None:
        self.assertFalse(looks_binary_text(""))
        self.assertTrue(looks_binary_text("x\x00y"))

    def test_text_replacement_ratio(self) -> None:
        sample = "\ufffd" * 20 + "a" * 80
        self.assertTrue(looks_binary_text(sample))
        self.assertFalse(looks_binary_text("normal source code"))


class LooksLikeDiffTests(unittest.TestCase):
    def test_git_header(self) -> None:
        self.assertTrue(looks_like_diff(GIT_DIFF))

    def test_hunk_marker(self) -> None:
        self.assertTrue(looks_like_diff("@@ -1,2 +1,2 @@\n"))
        self.assertTrue(looks_like_diff("line\n@@ -1 +1 @@\n"))

    def test_unified_headers(self) -> None:
        self.assertTrue(looks_like_diff(UNIFIED_DIFF))
        self.assertTrue(looks_like_diff("--- a/x\n+++ b/x\n"))

    def test_binary_files_line(self) -> None:
        self.assertTrue(looks_like_diff("Binary files a/x and b/x differ\n"))
        self.assertTrue(looks_like_diff("GIT binary patch\n"))

    def test_plain_text_is_not_diff(self) -> None:
        self.assertFalse(looks_like_diff("hello world\nno headers here"))


class DecodeAndReadTests(unittest.TestCase):
    def test_utf8_bom_stripped(self) -> None:
        self.assertEqual(_decode_bytes(b"\xef\xbb\xbfhello"), "hello")

    def test_utf16_le_bom(self) -> None:
        self.assertEqual(_decode_bytes("hi".encode("utf-16")), "hi")

    def test_invalid_utf8_replaced(self) -> None:
        text = _decode_bytes(b"ok\x80no")
        self.assertIn("ok", text)
        self.assertIn("\ufffd", text)

    def test_read_capped_truncates(self) -> None:
        handle = io.BytesIO(b"abcdefghij")
        data, truncated = _read_capped(handle, 4)
        self.assertEqual(data, b"abcd")
        self.assertTrue(truncated)

    def test_read_capped_fits(self) -> None:
        handle = io.BytesIO(b"abc")
        data, truncated = _read_capped(handle, 10)
        self.assertEqual(data, b"abc")
        self.assertFalse(truncated)

    def test_binary_stub(self) -> None:
        self.assertEqual(
            _binary_stub("photo.png", 12),
            "[binary file omitted: photo.png (12 bytes)]\n",
        )


class CapTextTests(unittest.TestCase):
    def test_zero_or_negative_returns_empty(self) -> None:
        self.assertEqual(cap_text("hello", 0, what="x"), "")
        self.assertEqual(cap_text("hello", -5, what="x"), "")

    def test_under_cap_unchanged(self) -> None:
        self.assertEqual(cap_text("hello", 10, what="x"), "hello")

    def test_over_cap_adds_marker(self) -> None:
        text = "abcdefghij" * 20
        out = cap_text(text, 80, what="body")
        self.assertLessEqual(len(out), 80)
        self.assertIn("[truncated:", out)
        self.assertIn("chars omitted", out)


class DiffSectionTests(unittest.TestCase):
    def test_split_git_positions(self) -> None:
        two = GIT_DIFF + "diff --git a/b.py b/b.py\n"
        positions = _split_positions(two)
        self.assertEqual(len(positions), 2)
        self.assertEqual(positions[0], 0)

    def test_split_index_and_unified(self) -> None:
        self.assertTrue(_split_positions(INDEX_DIFF))
        self.assertTrue(_split_positions(UNIFIED_DIFF))
        self.assertEqual(_split_positions("no headers"), [])

    def test_iter_sections_with_preamble(self) -> None:
        text = "commit abc\nAuthor: x\n\n" + GIT_DIFF
        sections = _iter_sections(text)
        self.assertEqual(sections[0][0], "preamble")
        self.assertEqual(sections[1][0], "file")

    def test_iter_sections_blob_when_no_headers(self) -> None:
        self.assertEqual(_iter_sections("plain"), [("blob", "plain")])

    def test_path_from_git_and_plus_plus(self) -> None:
        self.assertEqual(_path_from_section(GIT_DIFF), "src/app.py")
        self.assertEqual(
            _path_from_section("--- a/old.py\n+++ b/new.py\n@@ -1 +1 @@\n"),
            "new.py",
        )
        self.assertEqual(_path_from_section("Index: pkg/A.java\n"), "pkg/A.java")
        self.assertEqual(_path_from_section("???"), "(unknown)")

    def test_path_from_quoted_and_tab(self) -> None:
        section = 'diff --git a/"weird name.py" b/"weird name.py"\n'
        self.assertEqual(_path_from_section(section), "weird name.py")
        plus = "+++ b/foo.py\t2020-01-01\n"
        self.assertEqual(_path_from_section(plus), "foo.py")

    def test_section_binary_markers(self) -> None:
        self.assertTrue(
            _section_is_binary("Binary files a/x.png and b/x.png differ\n", "x.png")
        )
        self.assertTrue(_section_is_binary("hello\x00world", "a.txt"))
        self.assertTrue(_section_is_binary("no hunk here", "photo.png"))
        self.assertFalse(_section_is_binary(GIT_DIFF, "src/app.py"))

    def test_truncate_section_breaks_on_newline(self) -> None:
        section = "line1\n" + ("x" * 200) + "\nline3\n"
        out = _truncate_section(section, 40)
        self.assertIn("truncated", out)
        self.assertLess(len(out), len(section))


class SanitizeOneTests(unittest.TestCase):
    def test_plain_text_included(self) -> None:
        limits = InputLimits(max_file_chars=1000, max_prompt_chars=5000)
        text, stats = sanitize_one("notes.txt", "hello", limits, remaining_chars=5000, remaining_files=10)
        self.assertEqual(text, "hello")
        self.assertEqual(stats.files_included, 1)
        self.assertFalse(stats.did_sanitize)

    def test_plain_text_truncated(self) -> None:
        limits = InputLimits(max_file_chars=50, max_prompt_chars=5000)
        body = "word " * 80
        text, stats = sanitize_one(
            "notes.txt", body, limits, remaining_chars=5000, remaining_files=10
        )
        self.assertTrue(stats.files_truncated)
        self.assertIn("[truncated:", text)

    def test_binary_stub_passthrough(self) -> None:
        stub = _binary_stub("a.bin", 9)
        limits = InputLimits()
        text, stats = sanitize_one(
            "a.bin", stub, limits, remaining_chars=5000, remaining_files=10
        )
        self.assertEqual(text, stub)
        self.assertEqual(stats.binaries_omitted, 1)

    def test_binary_path_becomes_stub(self) -> None:
        limits = InputLimits()
        text, stats = sanitize_one(
            "pic.png", "not really an image", limits, remaining_chars=5000, remaining_files=10
        )
        self.assertTrue(text.startswith("[binary file omitted:"))
        self.assertEqual(stats.binaries_omitted, 1)

    def test_git_diff_keeps_text_file(self) -> None:
        limits = InputLimits(max_file_chars=50_000)
        text, stats = sanitize_one(
            "diff.patch", GIT_DIFF, limits, remaining_chars=50_000, remaining_files=10
        )
        self.assertIn("src/app.py", text)
        self.assertEqual(stats.files_included, 1)
        self.assertEqual(stats.files_seen, 1)

    def test_git_diff_omits_binary_file(self) -> None:
        patch = (
            GIT_DIFF
            + "diff --git a/logo.png b/logo.png\n"
            + "Binary files a/logo.png and b/logo.png differ\n"
        )
        limits = InputLimits()
        text, stats = sanitize_one(
            "diff.patch", patch, limits, remaining_chars=50_000, remaining_files=10
        )
        self.assertIn("src/app.py", text)
        self.assertNotIn("GIT binary", text)
        self.assertEqual(stats.binaries_omitted, 1)
        self.assertIn("logo.png", stats.omitted_paths)

    def test_git_diff_file_cap(self) -> None:
        two = GIT_DIFF + "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-a\n+b\n"
        limits = InputLimits(max_file_chars=50_000)
        text, stats = sanitize_one(
            "diff.patch", two, limits, remaining_chars=50_000, remaining_files=1
        )
        self.assertEqual(stats.files_included, 1)
        self.assertEqual(stats.files_over_cap, 1)

    def test_empty_diff_after_dropping_binaries(self) -> None:
        patch = "diff --git a/a.png b/a.png\nBinary files a/a.png and b/a.png differ\n"
        limits = InputLimits()
        text, stats = sanitize_one(
            "diff.patch", patch, limits, remaining_chars=50_000, remaining_files=10
        )
        self.assertIn("no reviewable text", text)
        self.assertEqual(stats.binaries_omitted, 1)

    def test_preamble_capped(self) -> None:
        preamble = "note " * 2000
        text = preamble + "\n" + GIT_DIFF
        limits = InputLimits()
        out, _stats = sanitize_one(
            "diff.patch", text, limits, remaining_chars=50_000, remaining_files=10
        )
        self.assertIn("src/app.py", out)
        self.assertLess(len(out), len(text))


class SanitizeAttachmentsTests(unittest.TestCase):
    def test_keeps_text_files(self) -> None:
        limits = InputLimits()
        out, stats = sanitize_attachments(
            [("a.txt", "hello"), ("b.txt", "world")], limits
        )
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0][1], "hello")
        self.assertFalse(stats.did_sanitize)

    def test_skips_binary_attachments(self) -> None:
        limits = InputLimits()
        out, stats = sanitize_attachments(
            [("pic.png", "xxx"), ("ok.txt", "text")], limits
        )
        names = [name for name, _ in out]
        self.assertIn("ok.txt", names)
        self.assertNotIn("pic.png", names)
        self.assertEqual(stats.binaries_omitted, 1)

    def test_skips_when_budget_exhausted(self) -> None:
        limits = InputLimits(max_prompt_chars=2500, max_files=80)
        attachments = [(f"f{i}.txt", "x" * 400) for i in range(20)]
        _out, stats = sanitize_attachments(attachments, limits, extra_overhead=0)
        self.assertGreater(stats.skipped_attachments, 0)

    def test_max_files_skips_rest(self) -> None:
        limits = InputLimits(max_files=1, max_prompt_chars=50_000)
        out, stats = sanitize_attachments(
            [("a.txt", "one"), ("b.txt", "two")], limits
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(stats.skipped_attachments, 1)

    def test_all_binary_placeholder(self) -> None:
        limits = InputLimits()
        out, stats = sanitize_attachments([("a.png", "xx"), ("b.jpg", "yy")], limits)
        self.assertEqual(len(out), 1)
        self.assertIn("no reviewable text", out[0][1])
        self.assertGreaterEqual(stats.binaries_omitted, 1)

    def test_extra_overhead_shrinks_budget(self) -> None:
        limits = InputLimits(max_prompt_chars=3000)
        out, _stats = sanitize_attachments(
            [("a.txt", "hello")], limits, extra_overhead=500
        )
        self.assertEqual(out[0][1], "hello")


class SanitizeNoteTests(unittest.TestCase):
    def test_empty_when_clean(self) -> None:
        self.assertEqual(format_sanitize_note(SanitizeStats()), "")

    def test_note_lists_details_and_sample(self) -> None:
        stats = SanitizeStats(
            original_chars=1000,
            output_chars=200,
            files_seen=5,
            files_included=2,
            binaries_omitted=1,
            files_truncated=1,
            files_over_cap=1,
            skipped_attachments=1,
            truncated_read=True,
            omitted_paths=[f"f{i}.bin" for i in range(30)],
        )
        note = format_sanitize_note(stats)
        self.assertIn("NOTE:", note)
        self.assertIn("binary omitted", note)
        self.assertIn("truncated", note)
        self.assertIn("over file cap", note)
        self.assertIn("attachment(s) skipped", note)
        self.assertIn("source read was capped", note)
        self.assertIn("+6 more", note)

    def test_finalize_prepends_note_and_hard_caps(self) -> None:
        stats = SanitizeStats(binaries_omitted=1, original_chars=10, output_chars=5)
        limits = InputLimits(max_prompt_chars=20_000)
        out = finalize_prompt("PROMPT", limits, stats)
        self.assertTrue(out.startswith("NOTE:"))
        self.assertIn("PROMPT", out)

    def test_finalize_hard_cap(self) -> None:
        stats = SanitizeStats()
        huge = "x" * 500
        limits = InputLimits(max_prompt_chars=100)
        out = finalize_prompt(huge, limits, stats)
        self.assertLessEqual(len(out), 100)
        self.assertIn("[truncated:", out)


class SanitizeStatsTests(unittest.TestCase):
    def test_merge_and_did_sanitize(self) -> None:
        a = SanitizeStats(original_chars=10, files_seen=1)
        b = SanitizeStats(
            original_chars=5,
            binaries_omitted=1,
            omitted_paths=["x.bin"],
            truncated_read=True,
        )
        a.merge(b)
        self.assertEqual(a.original_chars, 15)
        self.assertEqual(a.binaries_omitted, 1)
        self.assertEqual(a.omitted_paths, ["x.bin"])
        self.assertTrue(a.truncated_read)
        self.assertTrue(a.did_sanitize)
        self.assertFalse(SanitizeStats().did_sanitize)


class LoadPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.limits = InputLimits(max_read_bytes=64)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_file(self) -> None:
        with self.assertRaises(InputError) as ctx:
            load_path(self.folder / "missing.txt", self.limits)
        self.assertIn("not found", str(ctx.exception))

    def test_reads_utf8(self) -> None:
        path = self.folder / "a.txt"
        path.write_text("hello\n", encoding="utf-8")
        loaded = load_path(path, self.limits)
        self.assertEqual(loaded.text, "hello\n")
        self.assertFalse(loaded.binary)
        self.assertEqual(loaded.name, str(path))

    def test_custom_name(self) -> None:
        path = self.folder / "a.txt"
        path.write_text("x", encoding="utf-8")
        loaded = load_path(path, self.limits, name="alias")
        self.assertEqual(loaded.name, "alias")

    def test_skips_binary_extension(self) -> None:
        path = self.folder / "pic.png"
        path.write_bytes(b"not really png")
        loaded = load_path(path, self.limits)
        self.assertTrue(loaded.binary)
        self.assertIn("binary file omitted", loaded.text)

    def test_skips_nul_content(self) -> None:
        path = self.folder / "data.dat"
        path.write_bytes(b"abc\x00def")
        loaded = load_path(path, self.limits)
        self.assertTrue(loaded.binary)

    def test_truncated_read(self) -> None:
        path = self.folder / "big.txt"
        path.write_text("a" * 200, encoding="utf-8")
        loaded = load_path(path, InputLimits(max_read_bytes=20))
        self.assertTrue(loaded.truncated_read)
        self.assertEqual(len(loaded.text), 20)

    def test_strips_controls(self) -> None:
        path = self.folder / "ctrl.txt"
        path.write_bytes(b"ok\x00still")
        # NUL makes looks_binary_bytes True, so this is omitted as binary
        loaded = load_path(path, self.limits)
        self.assertTrue(loaded.binary)


class LoadStdinTests(unittest.TestCase):
    def test_reads_text(self) -> None:
        buf = io.BytesIO(b"patch here")
        fake = type("S", (), {"buffer": buf})()
        with patch("critique_bot.patch.sys.stdin", fake):
            loaded = load_stdin(InputLimits())
        self.assertEqual(loaded.text, "patch here")
        self.assertEqual(loaded.name, "stdin")

    def test_binary_stdin(self) -> None:
        buf = io.BytesIO(b"\x00\x01\x02" * 100)
        fake = type("S", (), {"buffer": buf})()
        with patch("critique_bot.patch.sys.stdin", fake):
            loaded = load_stdin(InputLimits())
        self.assertTrue(loaded.binary)
        self.assertIn("binary file omitted", loaded.text)

    def test_truncated_stdin(self) -> None:
        buf = io.BytesIO(b"x" * 50)
        fake = type("S", (), {"buffer": buf})()
        with patch("critique_bot.patch.sys.stdin", fake):
            loaded = load_stdin(InputLimits(max_read_bytes=10))
        self.assertTrue(loaded.truncated_read)
        self.assertEqual(loaded.text, "x" * 10)


class DefaultLimitsTests(unittest.TestCase):
    def test_defaults_and_absolutes(self) -> None:
        self.assertEqual(DEFAULT_MAX_PROMPT_CHARS, 120_000)
        self.assertEqual(DEFAULT_MAX_FILE_CHARS, 32_000)
        self.assertEqual(DEFAULT_MAX_FILES, 80)
        self.assertEqual(DEFAULT_MAX_READ_BYTES, 16_000_000)
        self.assertGreater(ABSOLUTE_MAX_PROMPT_CHARS, DEFAULT_MAX_PROMPT_CHARS)
        self.assertGreater(ABSOLUTE_MAX_FILE_CHARS, DEFAULT_MAX_FILE_CHARS)
        self.assertGreater(ABSOLUTE_MAX_FILES, DEFAULT_MAX_FILES)
        self.assertGreater(ABSOLUTE_MAX_READ_BYTES, DEFAULT_MAX_READ_BYTES)


if __name__ == "__main__":
    unittest.main()
