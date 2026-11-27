# SPDX-License-Identifier: Apache-2.0
"""The strings index never keeps or returns secret literals (SF-6). Fake secrets only."""

from __future__ import annotations

import unittest

from critique_bot.indexing import facts, locate

PASSWORD = "Qz7vLp2Xk9mR4wT8"
AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"


class StringsIndexSecretTests(unittest.TestCase):
    def values(self, text: str, language: str, rel: str) -> list[str]:
        return [item[2] for item in facts.parse_text(text, language, rel).strings]

    def test_a_literal_assigned_to_a_secret_name_is_not_indexed(self) -> None:
        text = (f'DB_PASSWORD = "{PASSWORD}"\n'
                'GREETING = "hello from the settings module"\n')
        found = self.values(text, "python", "app/settings.py")
        self.assertNotIn(PASSWORD, found)
        self.assertIn("hello from the settings module", found)

    def test_a_literal_with_a_secret_format_is_not_indexed(self) -> None:
        found = self.values(f'key = "{AWS_KEY}"\nmsg = "connecting to the bucket now"\n', "python", "a.py")
        self.assertNotIn(AWS_KEY, found)
        self.assertIn("connecting to the bucket now", found)

    def test_secret_files_are_not_indexed(self) -> None:
        self.assertEqual(facts.scrub_strings([(1, "literal", "some long value here", "", "")],
                                             "x = 'some long value here'\n", "config/local.properties"), [])

    def test_hits_from_an_older_index_are_filtered(self) -> None:
        hits = [locate.StringHit("a.py", 1, "literal", AWS_KEY, "", "", 1.0),
                locate.StringHit(".env", 1, "literal", "plain words here", "", "", 1.0),
                locate.StringHit("b.py", 2, "log", "plain words here", "", "", 1.0)]
        self.assertEqual([hit.path for hit in hits if not locate._secret_hit(hit)], ["b.py"])


if __name__ == "__main__":
    unittest.main()
