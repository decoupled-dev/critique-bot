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
        self.assertEqual(lines, [])

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


if __name__ == "__main__":
    unittest.main()
