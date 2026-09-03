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
        self.assertEqual(line_code_for("src/pay.py", None, 12), f"{digest}_12_12")
        row = DiffLine("src/pay.py", "src/pay.py", None, 12, "add")
        span = line_range_for(row)
        self.assertEqual(span["start"]["line_code"], f"{digest}_12_12")
        self.assertEqual(span["start"]["type"], "new")
        self.assertEqual(span["start"]["new_line"], 12)
        refs = {"base_sha": "b", "start_sha": "s", "head_sha": "h"}
        pos = discussion_position(refs, row)
        self.assertEqual(
            pos,
            {
                "base_sha": "b",
                "start_sha": "s",
                "head_sha": "h",
                "old_path": "src/pay.py",
                "new_path": "src/pay.py",
                "position_type": "text",
                "new_line": 12,
            },
        )
        self.assertNotIn("line_range", pos)
        with_range = discussion_position(refs, row, with_line_range=True)
        self.assertIn("line_range", with_range)

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

    def test_nested_suggestion_fence_does_not_break_json(self) -> None:
        text = (
            "**Risk: Risky**\n1. Fix it.\n\n"
            "```json\n"
            '{"risk":"risky","comments":[{"path":"src/pay.py","line":12,"body":'
            '"**Must fix**\\n\\n```suggestion\\nfoo\\n```"}]}'
            "\n```\n"
        )
        comments = parse_inline_comments(text)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].path, "src/pay.py")
        stripped = strip_json_block(text)
        self.assertIn("**Risk: Risky**", stripped)
        self.assertNotIn("```json", stripped)
        self.assertNotIn('"comments"', stripped)
        self.assertNotIn("suggestion", stripped)

    def test_unfenced_json_is_stripped(self) -> None:
        text = (
            "**Risk: Risky**\n1. Restore identity.\n"
            '{"risk":"risky","comments":[{"path":"a.py","line":1,"body":"x"}]}'
        )
        self.assertEqual(len(parse_inline_comments(text)), 1)
        stripped = strip_json_block(text)
        self.assertEqual(stripped, "**Risk: Risky**\n1. Restore identity.")

    def test_comment_key_and_findings_alias(self) -> None:
        comments = parse_inline_comments(
            json.dumps(
                {
                    "findings": [
                        {"path": "a.py", "old_line": 4, "side": "old", "comment": "gone"}
                    ]
                }
            )
        )
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].body, "gone")
        self.assertEqual(comments[0].line, 4)
        self.assertEqual(comments[0].side, "old")

    def test_resolve_suffix_and_basename_path(self) -> None:
        from critique_bot.review_comments import InlineComment

        lines = parse_diff_lines(PATCH)
        row = resolve_comment(
            InlineComment(path="pay.py", line=12, side="new", body="x"),
            lines,
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row.path, "src/pay.py")
        nested = resolve_comment(
            InlineComment(path="src/pay.py", line=12, side="new", body="x"),
            lines,
        )
        self.assertEqual(nested.path, "src/pay.py")

    def test_trailing_comma_and_smart_quotes(self) -> None:
        text = (
            "**Risk: Risky**\n1. Fix it.\n\n"
            "```json\n"
            '{“risk”: “risky”, “comments”: [{'
            '“path”: “src/pay.py”, “line”: 12, “body”: “coupon is not defined”,'
            '}]}\n'
            "```\n"
        )
        comments = parse_inline_comments(text)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].path, "src/pay.py")
        self.assertEqual(comments[0].line, 12)

    def test_unescaped_newline_in_body(self) -> None:
        text = (
            '```json\n{"comments":[{"path":"src/pay.py","line":12,"body":"'
            "**Must fix**\n\nrestore identity"
            '"}]}\n```\n'
        )
        comments = parse_inline_comments(text)
        self.assertEqual(len(comments), 1)
        self.assertIn("restore identity", comments[0].body)

    def test_top_level_array_and_line_as_string(self) -> None:
        comments = parse_inline_comments(
            '[{"path":"src/pay.py","line":"L12","message":"x"}]'
        )
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].line, 12)
        self.assertEqual(comments[0].body, "x")

    def test_truncated_json_is_closed(self) -> None:
        comments = parse_inline_comments(
            '{"comments":[{"path":"src/pay.py","line":12,"body":"x"}]'
        )
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].path, "src/pay.py")

    def test_comments_object_instead_of_list(self) -> None:
        comments = parse_inline_comments(
            '{"comments":{"path":"src/pay.py","line":12,"body":"x"}}'
        )
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].body, "x")

    def test_normalize_summary_splits_paragraph(self) -> None:
        from critique_bot.review_comments import (
            format_gitlab_summary,
            normalize_summary_markdown,
        )

        paragraph = (
            "**Risk: Risky** **2 actions** 1. Restore Binder identity in Foo.java. "
            "2. Add a test that a 3p caller is rejected."
        )
        body = normalize_summary_markdown(paragraph)
        self.assertIn("**Risk: Risky**", body)
        self.assertIn("**2 actions**", body)
        self.assertIn("1. Restore Binder identity in Foo.java.", body)
        self.assertIn("2. Add a test that a 3p caller is rejected.", body)
        self.assertIn("\n\n1. ", "\n\n" + body)
        self.assertRegex(body, r"1\. Restore Binder identity.*\n2\. Add a test")
        formatted = format_gitlab_summary(paragraph, risk="risky")
        self.assertIn("\n1. Restore Binder identity in Foo.java.\n2. ", formatted)
        self.assertIn("### AAOS system-app review", formatted)

    def test_normalize_summary_adds_blank_line_before_list(self) -> None:
        from critique_bot.review_comments import format_gitlab_summary

        formatted = format_gitlab_summary(
            "**2 actions**\n1. Restore identity.\n2. Add a test.",
            risk="risky",
        )
        self.assertIn("**2 actions**\n\n1. Restore identity.\n2. Add a test.", formatted)

    def test_unfenced_pretty_json_after_markdown(self) -> None:
        text = (
            "**Risk: Risky**\n\n"
            "1. Restore Binder identity in Foo.java.\n\n"
            "{\n"
            '  "risk": "risky",\n'
            '  "comments": [\n'
            "    {\n"
            '      "path": "src/pay.py",\n'
            '      "line": 12,\n'
            '      "body": "**Security**\n\nrestore in finally."\n'
            "    }\n"
            "  ]\n"
            "}\n"
        )
        comments = parse_inline_comments(text)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].path, "src/pay.py")
        self.assertEqual(comments[0].line, 12)
        self.assertIn("restore in finally", comments[0].body)
        stripped = strip_json_block(text)
        self.assertIn("**Risk: Risky**", stripped)
        self.assertNotIn('"comments"', stripped)
        self.assertNotIn('"path"', stripped)

    def test_diff_fence_is_not_treated_as_json(self) -> None:
        text = (
            "**Risk: Risky**\n\n"
            "1. Fix coupon in `src/pay.py:12`.\n\n"
            "```diff\n"
            "+ return qty * price * coupon\n"
            "```\n"
        )
        self.assertEqual(parse_inline_comments(text), [])
        stripped = strip_json_block(text)
        self.assertIn("```diff", stripped)
        self.assertIn("coupon", stripped)

    def test_comments_as_json_string_and_index_map(self) -> None:
        as_string = parse_inline_comments(
            '{"comments": "[{\\"path\\": \\"src/pay.py\\", \\"line\\": 12, \\"body\\": \\"x\\"}]"}'
        )
        self.assertEqual(len(as_string), 1)
        self.assertEqual(as_string[0].path, "src/pay.py")
        as_map = parse_inline_comments(
            json.dumps(
                {
                    "comments": {
                        "0": {"path": "src/pay.py", "line": 12, "body": "first"},
                        "1": {"path": "src/pay.py", "line": 11, "body": "second"},
                    }
                }
            )
        )
        self.assertEqual([c.body for c in as_map], ["first", "second"])

    def test_path_keyed_comment_map(self) -> None:
        comments = parse_inline_comments(
            json.dumps(
                {
                    "comments": {
                        "src/pay.py": {"line": 12, "body": "coupon is not defined"}
                    }
                }
            )
        )
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].path, "src/pay.py")
        self.assertEqual(comments[0].line, 12)

    def test_broken_wrapper_recovers_comment_objects(self) -> None:
        text = (
            "**Risk: Risky**\n"
            'not json {"path":"src/pay.py","line":12,"body":"coupon is not defined"} '
            '{"path":"src/pay.py","line":11,"body":"trusted caller"}\n'
        )
        comments = parse_inline_comments(text)
        self.assertEqual(len(comments), 2)
        self.assertEqual(comments[0].line, 12)
        self.assertEqual(comments[1].line, 11)
        stripped = strip_json_block(text)
        self.assertNotIn('"path"', stripped)
        self.assertIn("**Risk: Risky**", stripped)

    def test_comments_from_summary_maps_path_line(self) -> None:
        from critique_bot.review_comments import comments_from_summary

        text = (
            "**Risk: Risky**\n\n"
            "**1 action**\n\n"
            "1. coupon is not defined in `src/pay.py:12`.\n"
        )
        comments = comments_from_summary(text, parse_diff_lines(PATCH))
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].path, "src/pay.py")
        self.assertEqual(comments[0].line, 12)
        self.assertIn("coupon is not defined", comments[0].body)

    def test_comments_from_summary_skips_safe_empty_review(self) -> None:
        from critique_bot.review_comments import comments_from_summary

        text = "**Risk: Safe**\n\nNo actionable findings.\n"
        self.assertEqual(comments_from_summary(text, parse_diff_lines(PATCH)), [])
        self.assertEqual(
            comments_from_summary(
                '**Risk: Safe**\n\nNo actionable findings.\n```json\n{"risk":"safe","comments":[]}\n```\n',
                parse_diff_lines(PATCH),
            ),
            [],
        )

    def test_path_line_suffix_on_json_path(self) -> None:
        comments = parse_inline_comments(
            '{"comments":[{"path":"src/pay.py:12","body":"x"}]}'
        )
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].path, "src/pay.py")
        self.assertEqual(comments[0].line, 12)


if __name__ == "__main__":
    unittest.main()
