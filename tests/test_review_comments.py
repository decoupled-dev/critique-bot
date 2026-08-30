from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
