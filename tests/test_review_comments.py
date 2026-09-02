from __future__ import annotations

import json
import unittest

from critique_bot.review_comments import (
    parse_diff_lines,
    parse_inline_comments,
    position_for,
    resolve_comment,
    strip_json_block,
)

PATCH = """\
diff --git a/src/pay.py b/src/pay.py
index 111..222 100644
--- a/src/pay.py
+++ b/src/pay.py
@@ -8,7 +8,9 @@ def total(qty, price):
     if qty < 0:
         return 0
-    return qty * price
+    # trusted caller
+    return qty * price * coupon
+
"""

REVIEW = """\
Coupon is unbounded.

```json
{"comments":[{"path":"src/pay.py","line":12,"side":"new","body":"coupon is not defined"}]}
```
"""


class ReviewCommentTests(unittest.TestCase):
    def test_parse_json_fence(self) -> None:
        comments = parse_inline_comments(REVIEW)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].path, "src/pay.py")
        self.assertEqual(comments[0].line, 12)
        self.assertEqual(comments[0].side, "new")
        self.assertIn("coupon", comments[0].body)

    def test_strip_json_leaves_prose(self) -> None:
        self.assertEqual(strip_json_block(REVIEW), "Coupon is unbounded.")

    def test_maps_added_line(self) -> None:
        comments = parse_inline_comments(REVIEW)
        row = resolve_comment(comments[0], parse_diff_lines(PATCH))
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row.kind, "add")
        self.assertEqual(row.new_line, 12)
        self.assertEqual(position_for(row), {"new_line": 12})

    def test_skips_unknown_file(self) -> None:
        comments = parse_inline_comments(
            '{"comments":[{"path":"nope.c","line":1,"side":"new","body":"x"}]}'
        )
        self.assertIsNone(resolve_comment(comments[0], parse_diff_lines(PATCH)))

    def test_no_json_returns_empty(self) -> None:
        self.assertEqual(parse_inline_comments("just prose"), [])
        self.assertEqual(strip_json_block("just prose"), "just prose")

    def test_side_aliases_and_file_message_keys(self) -> None:
        comments = parse_inline_comments(
            json.dumps(
                {
                    "comments": [
                        {"file": "src/pay.py", "new_line": 11, "side": "right", "message": "a"},
                        {"path": "src/pay.py", "line": 10, "side": "+", "body": "b"},
                        {"path": "src/pay.py", "line": 10, "side": "deleted", "body": "c"},
                        {"path": "src/pay.py", "line": 10, "side": "-", "body": "d"},
                        {"path": "src/pay.py", "line": 10, "side": "left", "body": "e"},
                        {"path": "src/pay.py", "line": 10, "side": "weird", "body": "f"},
                    ]
                }
            )
        )
        self.assertEqual(comments[0].side, "new")
        self.assertEqual(comments[0].path, "src/pay.py")
        self.assertEqual(comments[1].side, "new")
        self.assertEqual(comments[2].side, "old")
        self.assertEqual(comments[3].side, "old")
        self.assertEqual(comments[4].side, "old")
        self.assertEqual(comments[5].side, "new")

    def test_skips_invalid_items(self) -> None:
        comments = parse_inline_comments(
            json.dumps(
                {
                    "comments": [
                        "nope",
                        {"path": "a.py", "line": "x", "body": "z"},
                        {"path": "", "line": 1, "body": "z"},
                        {"path": "a.py", "line": 0, "body": "z"},
                        {"path": "a.py", "line": 1, "body": ""},
                        {"path": "a.py", "line": 2, "body": "ok"},
                    ]
                }
            )
        )
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].body, "ok")

    def test_max_inline_comments(self) -> None:
        from critique_bot.review_comments import MAX_INLINE_COMMENTS

        raw = {
            "comments": [
                {"path": "a.py", "line": i + 1, "body": f"c{i}"} for i in range(20)
            ]
        }
        comments = parse_inline_comments(json.dumps(raw))
        self.assertEqual(len(comments), MAX_INLINE_COMMENTS)

    def test_comments_not_a_list(self) -> None:
        self.assertEqual(parse_inline_comments('{"comments": {}}'), [])

    def test_parse_diff_deleted_and_context(self) -> None:
        lines = parse_diff_lines(PATCH)
        kinds = [row.kind for row in lines]
        self.assertIn("del", kinds)
        self.assertIn("add", kinds)
        self.assertIn("context", kinds)
        deleted = next(row for row in lines if row.kind == "del")
        self.assertEqual(position_for(deleted), {"old_line": deleted.old_line})
        context = next(row for row in lines if row.kind == "context")
        pos = position_for(context)
        self.assertIn("new_line", pos)
        self.assertIn("old_line", pos)

    def test_resolve_old_side_exact_and_nearest(self) -> None:
        from critique_bot.review_comments import InlineComment

        lines = parse_diff_lines(PATCH)
        deleted = next(row for row in lines if row.kind == "del")
        exact = resolve_comment(
            InlineComment(path="src/pay.py", line=deleted.old_line or 1, side="old", body="x"),
            lines,
        )
        self.assertIsNotNone(exact)
        assert exact is not None
        self.assertEqual(exact.kind, "del")
        near = resolve_comment(
            InlineComment(path="src/pay.py", line=999, side="old", body="x"),
            lines,
        )
        self.assertIsNotNone(near)
        assert near is not None
        self.assertEqual(near.kind, "del")

    def test_resolve_prefers_added_line(self) -> None:
        from critique_bot.review_comments import InlineComment

        lines = parse_diff_lines(PATCH)
        added = next(row for row in lines if row.kind == "add")
        row = resolve_comment(
            InlineComment(path="src/pay.py", line=added.new_line or 1, side="new", body="x"),
            lines,
        )
        self.assertEqual(row.kind, "add")

    def test_resolve_nearest_added(self) -> None:
        from critique_bot.review_comments import InlineComment

        lines = parse_diff_lines(PATCH)
        row = resolve_comment(
            InlineComment(path="src/pay.py", line=1, side="new", body="x"),
            lines,
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row.kind, "add")

    def test_clean_paths_in_diff(self) -> None:
        patch = (
            "diff --git a/old.py b/old.py\n"
            "--- a/gone.py\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-x\n"
        )
        lines = parse_diff_lines(patch)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].path, "gone.py")
        self.assertEqual(lines[0].kind, "del")
        self.assertEqual(lines[0].old_line, 1)

    def test_json_fence_case_insensitive(self) -> None:
        text = 'hi\n```JSON\n{"comments":[{"path":"a.py","line":1,"body":"x"}]}\n```\n'
        comments = parse_inline_comments(text)
        self.assertEqual(len(comments), 1)
        self.assertEqual(strip_json_block(text), "hi")

    def test_raw_object_without_fence(self) -> None:
        comments = parse_inline_comments(
            'prefix {"comments":[{"path":"a.py","line":3,"body":"x"}]}'
        )
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].line, 3)

    def test_position_for_old_line_only(self) -> None:
        from critique_bot.review_comments import DiffLine, position_for

        row = DiffLine("a.py", "a.py", 4, None, "other")
        self.assertEqual(position_for(row), {"old_line": 4})
        empty = DiffLine("a.py", "a.py", None, None, "other")
        self.assertEqual(position_for(empty), {})

    def test_skips_nit_severity(self) -> None:
        comments = parse_inline_comments(
            json.dumps(
                {
                    "comments": [
                        {"path": "a.py", "line": 1, "severity": "nit", "body": "rename"},
                        {
                            "path": "a.py",
                            "line": 2,
                            "severity": "must-fix",
                            "body": "restore Binder identity",
                        },
                    ]
                }
            )
        )
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].severity, "must-fix")

    def test_prefers_last_comments_json_fence(self) -> None:
        text = (
            "ignore\n"
            '```json\n{"comments":[{"path":"old.py","line":1,"body":"stale"}]}\n```\n'
            "real\n"
            '```json\n{"comments":[{"path":"a.py","line":3,"body":"keep"}]}\n```\n'
        )
        comments = parse_inline_comments(text)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].path, "a.py")
        self.assertEqual(strip_json_block(text), "ignore\n\nreal")

    def test_line_code_and_range(self) -> None:
        import hashlib

        from critique_bot.review_comments import (
            DiffLine,
            discussion_position,
            line_code_for,
            line_range_for,
        )

        digest = hashlib.sha1(b"src/pay.py").hexdigest()
        self.assertEqual(line_code_for("src/pay.py", None, 12), f"{digest}_0_12")
        row = DiffLine("src/pay.py", "src/pay.py", None, 12, "add")
        span = line_range_for(row)
        self.assertEqual(span["start"]["line_code"], f"{digest}_0_12")
        self.assertEqual(span["start"]["type"], "new")
        self.assertEqual(span["start"]["new_line"], 12)
        self.assertNotIn("old_line", span["start"])
        refs = {"base_sha": "b", "start_sha": "s", "head_sha": "h"}
        pos = discussion_position(refs, row)
        self.assertEqual(pos["new_line"], 12)
        self.assertNotIn("old_line", pos)
        self.assertIn("line_range", pos)
        bare = discussion_position(refs, row, with_line_range=False)
        self.assertNotIn("line_range", bare)

    def test_format_gitlab_comment_and_summary(self) -> None:
        from critique_bot.review_comments import (
            InlineComment,
            format_gitlab_comment,
            format_gitlab_summary,
            truncate_markdown,
        )

        titled = InlineComment(
            path="Foo.java",
            line=8,
            side="new",
            body="**Security**\n\nRestore identity.",
            severity="security",
        )
        self.assertEqual(
            format_gitlab_comment(titled), "**Security**\n\nRestore identity."
        )
        untitled = InlineComment(
            path="Foo.java", line=8, side="new", body="Restore identity.", severity="test"
        )
        self.assertIn("**Missing test**", format_gitlab_comment(untitled))
        located = format_gitlab_comment(untitled, include_location=True)
        self.assertIn("`Foo.java:8`", located)
        summary = format_gitlab_summary(
            "1. Restore Binder identity.", inline_count=1, overview_count=1
        )
        self.assertIn("### AAOS system-app review", summary)
        self.assertIn("Changes", summary)
        self.assertIn("overview thread", summary)
        self.assertTrue(truncate_markdown("short", 10) == "short")
        self.assertTrue(truncate_markdown("a" * 50, 10).endswith("…"))

    def test_parse_and_format_risk(self) -> None:
        from critique_bot.review_comments import (
            format_gitlab_summary,
            infer_risk_from_comments,
            parse_review_risk,
        )

        text = (
            "**Risk: Risky**\n1. Restore identity.\n"
            '```json\n{"risk":"risky","comments":['
            '{"path":"a.py","line":1,"severity":"security","body":"x"}]}\n```\n'
        )
        self.assertEqual(parse_review_risk(text), "risky")
        self.assertEqual(parse_review_risk('{"risk":"Blocker","comments":[]}'), "blocker")
        self.assertEqual(parse_review_risk("**Risk: Moderate risk**\nNo JSON"), "moderate")
        self.assertEqual(parse_review_risk("just prose"), "safe")
        self.assertEqual(
            infer_risk_from_comments(parse_inline_comments(
                '{"comments":[{"path":"a.py","line":1,"severity":"test","body":"x"}]}'
            )),
            "moderate",
        )
        formatted = format_gitlab_summary(
            "**Risk: Risky**\n1. Restore identity.", risk="risky"
        )
        self.assertIn("**Risk: Risky**", formatted)
        self.assertIn(":red_circle:", formatted)
        self.assertIn("1. Restore identity.", formatted)
        self.assertEqual(formatted.count("Risk: Risky"), 1)


if __name__ == "__main__":
    unittest.main()
