# SPDX-License-Identifier: Apache-2.0
"""Company, customer, and project names from ``redact_terms`` never reach the chat."""

from __future__ import annotations

import threading
import unittest

from critique_bot.redaction import (
    NAME_MARKER,
    REDACTED,
    clean_terms,
    custom_terms,
    redact_outgoing,
    redact_terms,
    set_custom_terms,
)


class TermTests(unittest.TestCase):
    def setUp(self) -> None:
        set_custom_terms(["Acme Corp", "Acme", "Globex"])
        self.addCleanup(set_custom_terms, ())

    def assertHidden(self, text: str, expected: str) -> None:
        self.assertEqual(redact_terms(text)[0], expected, text)

    def test_marker_format_matches_the_edit_guard(self) -> None:
        self.assertEqual(NAME_MARKER, "[REDACTED:name]")
        self.assertTrue(NAME_MARKER.startswith(REDACTED))

    def test_separator_and_case_variants_of_a_multi_word_term(self) -> None:
        for text in ("Acme Corp", "acme-corp", "acme_corp", "AcmeCorp", "acme.corp", "ACME CORP", "acme  corp"):
            with self.subTest(text=text):
                self.assertHidden(text, "[REDACTED:name]")

    def test_identifier_boundaries(self) -> None:
        cases = {
            "AcmeClient": "[REDACTED:name]Client",
            "com.acme.app": "com.[REDACTED:name].app",
            "ACME_API_URL": "[REDACTED:name]_API_URL",
            "acme_db": "[REDACTED:name]_db",
            "myAcmeThing": "my[REDACTED:name]Thing",
            "XMLAcme": "XML[REDACTED:name]",
            "ACMEClient": "[REDACTED:name]Client",
            "Acme's": "[REDACTED:name]'s",
            "acme2": "[REDACTED:name]2",
            "https://globex.com/x": "https://[REDACTED:name].com/x",
            "src/acme/Main.kt": "src/[REDACTED:name]/Main.kt",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertHidden(text, expected)

    def test_letters_continuing_the_word_keep_it(self) -> None:
        for text in ("Acmeville", "acmeville", "globexian", "Superacme", "ACMEVILLE"):
            with self.subTest(text=text):
                self.assertEqual(redact_terms(text), (text, 0))

    def test_longest_term_wins(self) -> None:
        self.assertEqual(redact_terms("Acme Corp and Acme"), ("[REDACTED:name] and [REDACTED:name]", 2))

    def test_redact_outgoing_applies_terms_after_pii(self) -> None:
        out = redact_outgoing("ask jane.doe@acme.com about AcmeCorpService")  # gitleaks:allow
        self.assertEqual(out, "ask [REDACTED:email] about [REDACTED:name]Service")

    def test_terms_inside_a_secret_key_name_are_hidden_too(self) -> None:
        out = redact_outgoing("ACME_API_TOKEN=Xk9mP2qL7vR4\n")  # gitleaks:allow
        self.assertNotIn("Xk9mP2qL7vR4", out)
        self.assertNotIn("acme", out.lower())

    def test_off_when_empty(self) -> None:
        set_custom_terms(())
        self.assertEqual(custom_terms(), ())
        self.assertEqual(redact_terms("Acme Corp"), ("Acme Corp", 0))

    def test_regex_characters_in_terms_are_literal(self) -> None:
        set_custom_terms(["AT&T Labs", "Foo+Bar", "a.b.c"])
        self.assertEqual(redact_terms("at&t-labs and foo+bar and abc")[0], "[REDACTED:name] and [REDACTED:name] and [REDACTED:name]")
        self.assertEqual(redact_terms("fooXbar")[1], 0)

    def test_concurrent_readers_see_a_whole_setting(self) -> None:
        errors: list[BaseException] = []

        def reader() -> None:
            try:
                for _ in range(500):
                    redact_terms("Acme Corp Globex")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for thread in threads:
            thread.start()
        for index in range(200):
            set_custom_terms(["Acme Corp"] if index % 2 else ["Globex", "Initech"])
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])


class CleanTermsTests(unittest.TestCase):
    def test_strips_collapses_and_dedupes(self) -> None:
        self.assertEqual(clean_terms([" Acme   Corp ", "acme corp", "Globex"]), ("Acme Corp", "Globex"))

    def test_rejects_bad_entries(self) -> None:
        for bad in (["ab"], ["a b"], [""], ["   "], [None], [7], ["x" * 201], ["--"]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    clean_terms(bad)

    def test_limits(self) -> None:
        self.assertEqual(clean_terms(["abc", "y" * 200]), ("abc", "y" * 200))


if __name__ == "__main__":
    unittest.main()
