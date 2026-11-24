"""What redaction hides before text leaves this machine, and which files count as secret."""

from __future__ import annotations

import unittest
from pathlib import Path

from critique_bot.redaction import (
    REDACTED,
    redact_outgoing,
    redact_pii,
    redact_secrets,
    sensitive_file,
    set_pii_redaction,
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "critique_bot"


class RedactionTests(unittest.TestCase):
    def assertHidden(self, text: str, secret: str) -> None:
        out, count = redact_secrets(text)
        self.assertNotIn(secret, out, out)
        self.assertIn(REDACTED, out)
        self.assertGreaterEqual(count, 1)

    def assertKept(self, text: str) -> None:
        out, count = redact_secrets(text)
        self.assertEqual(out, text)
        self.assertEqual(count, 0)

    def test_unquoted_values_that_are_not_code(self) -> None:
        cases = [
            ("DB_PASSWORD=Xk9mP2qL7vR4\n", "Xk9mP2qL7vR4"),  # gitleaks:allow
            ("export API_TOKEN=abc123def456ghi\n", "abc123def456ghi"),  # gitleaks:allow
            ("storePassword=android123\n", "android123"),  # gitleaks:allow
            ("+storePassword=android123\n", "android123"),  # gitleaks:allow
            ("Server=db;User Id=sa;Password=Hunter2Hunter2;", "Hunter2Hunter2"),  # gitleaks:allow
            ("users:\n- name: me\n  user:\n    token: abcdef0123456789abcdef\n", "abcdef0123456789abcdef"),  # gitleaks:allow
            ("SECRET_KEY=hunterhunterhunter\n", "hunterhunterhunter"),  # gitleaks:allow
            ("  password: correcthorsebattery\n", "correcthorsebattery"),  # gitleaks:allow
        ]
        for text, secret in cases:
            with self.subTest(text=text):
                self.assertHidden(text, secret)

    def test_code_references_are_left_alone(self) -> None:
        for text in [
            "val key = BuildConfig.API_KEY\n",
            "val pw = getPassword()\n",
            "password = user.password\n",
            "token=${TOKEN}\n",
            'password=os.environ["X"]\n',
            "    password = user_password\n",
            "        token=resolved_token,\n",
            "client = Client(token=resolved_token)\n",
            "apiKey: process.env.API_KEY\n",
            "password: string;\n",
            'if line.startswith("-----BEGIN RSA PRIVATE KEY-----"):\n',
            "//registry.npmjs.org/:_authToken=${NPM_TOKEN}\n",
            'headers["Authorization"] = "Bearer " + token\n',
        ]:
            with self.subTest(text=text):
                self.assertKept(text)

    def test_json_and_call_arguments(self) -> None:
        cases = [
            ('{"password": "hunter2hunter2"}', "hunter2hunter2"),  # gitleaks:allow
            ('connect(password="hunter2hunter2")', "hunter2hunter2"),  # gitleaks:allow
            ("{password: 'hunter2hunter2'}", "hunter2hunter2"),  # gitleaks:allow
            ('{"api_key":"q8Zr3mT1vX0pL5kN"}', "q8Zr3mT1vX0pL5kN"),  # gitleaks:allow
        ]
        for text, secret in cases:
            with self.subTest(text=text):
                self.assertHidden(text, secret)

    def test_known_token_formats(self) -> None:
        cases = [
            ("Authorization: Bearer abcDEF123456ghiJKL789mno\n", "abcDEF123456ghiJKL789mno"),  # gitleaks:allow
            ('"Authorization": "Bearer abcDEF123456ghiJKL789mno"', "abcDEF123456ghiJKL789mno"),  # gitleaks:allow
            ("Authorization: Basic dXNlcjpodW50ZXIy\n", "dXNlcjpodW50ZXIy"),  # gitleaks:allow
            ("//registry.npmjs.org/:_authToken=a1b2c3d4-e5f6\n", "a1b2c3d4-e5f6"),  # gitleaks:allow
            ("t = npm_" + "A" * 36, "npm_" + "A" * 36),
            ("k = sk" "_live_4eC39HqLyjWDarjtT1zdp7dc", "4eC39HqLyjWDarjtT1zdp7dc"),  # gitleaks:allow
            ("k = rk" "_live_4eC39HqLyjWDarjtT1zdp7dc", "4eC39HqLyjWDarjtT1zdp7dc"),  # gitleaks:allow
            ("k = sk" "_test_4eC39HqLyjWDarjtT1zdp7dc", "4eC39HqLyjWDarjtT1zdp7dc"),  # gitleaks:allow
            ("k = SG" ".abcdefghijklmnopqrstuv.ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefg", "SG" ".abcdefghij"),  # gitleaks:allow
            ("k = hf" "_abcdefghijklmnopqrstuvwxyzABCDEFGH", "hf" "_abcdefghij"),  # gitleaks:allow
            ("k = py" "pi-AgEIcHlwaS5vcmcCJDAwMDAwMDAwLTAwMDAtMDAwMC0wMDAwLTAwMDAwMDAwMDAwMA", "py" "pi-AgEI"),  # gitleaks:allow
            ("k = gl" "rt-abcdefghijklmnopqrstuv", "gl" "rt-abcdef"),  # gitleaks:allow
            ("k = gl" "dt-abcdefghijklmnopqrstuv", "gl" "dt-abcdef"),  # gitleaks:allow
            ("k = gl" "cbt-64_abcdefghijklmnopqrstuv", "gl" "cbt-64_abcdef"),  # gitleaks:allow
            ("k = GO" "CSPX-abcdefghijklmnopqrstuvwx", "GO" "CSPX-abcdef"),  # gitleaks:allow
            ("k = ya" "29.a0AfH6SMBabcdefghijklmnopqrstuv", "ya" "29.a0AfH6"),  # gitleaks:allow
            ("https://ho" "oks.slack.com/services/T0000/B0000/XXXXXXXXXXXXXXXX", "T0000/B0000"),  # gitleaks:allow
            ("https://acct.blob.core.windows.net/c?sv=2020-08-04&sig=abcDEF123%2Bghi456jkl%3D", "abcDEF123"),  # gitleaks:allow
            ("DefaultEndpointsProtocol=https;AccountName=a;AccountKey=abc123def456ghi789==;", "abc123def456"),  # gitleaks:allow
            ("Endpoint=sb://x/;SharedAccessKeyName=r;SharedAccessKey=abcDEF123ghiJKL456=", "abcDEF123ghi"),  # gitleaks:allow
            ("url=jdbc:mysql://db:3306/app?user=app&password=secret", "secret"),  # gitleaks:allow
            ("jdbc:sqlserver://db;user=sa;password=letmein", "letmein"),  # gitleaks:allow
            ("REDIS_URL=redis://:hunter22@cache:6379/0", "hunter22"),  # gitleaks:allow
            ("    client-key-data: LS0tLS1CRUdJTiBSU0EgUFJJVkFURSBLRVktLS0tLQ==\n", "LS0tLS1CRUdJ"),  # gitleaks:allow
            ("    client-certificate-data: LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0t\n", "LS0tLS1CRUdJ"),  # gitleaks:allow
        ]
        for text, secret in cases:
            with self.subTest(text=text):
                self.assertHidden(text, secret)

    def test_private_keys(self) -> None:
        body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7"  # gitleaks:allow
        pgp = f"-----BEGIN PGP PRIVATE KEY BLOCK-----\n\nlQOYBF{body}\n-----END PGP PRIVATE KEY BLOCK-----\n"  # gitleaks:allow
        self.assertHidden(pgp, body)
        gcp = '{"type": "service_account", "private_key": "-----BEGIN PRIVATE KEY-----\\n' + body + '\\n-----END PRIVATE KEY-----\\n"}'  # gitleaks:allow
        self.assertHidden(gcp, body)
        cut_gcp = '{"private_key": "-----BEGIN PRIVATE KEY-----\\n' + body  # gitleaks:allow
        self.assertHidden(cut_gcp, body)
        cut = f"key:\n-----BEGIN RSA PRIVATE KEY-----\n{body}\n{body[::-1]}"  # gitleaks:allow
        out, _ = redact_secrets(cut)
        self.assertEqual(out, f"key:\n{REDACTED}private key]")

    def test_bare_key_bodies(self) -> None:
        body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7"  # gitleaks:allow
        self.assertHidden(f"x = 1\n+{body}\n+{body[::-1]}\ny = 2\n", body)
        # One line, or hex digests (single case), stay.
        self.assertKept(f"x = 1\n{body}\ny = 2\n")
        self.assertKept("a" * 3 + "0123456789abcdef" * 3 + "\n" + "0123456789abcdef" * 3 + "\n")

    def test_existing_source_is_not_touched_by_the_new_rules(self) -> None:
        for name in ("patch.py", "review_session.py", "workspace.py", "cli.py", "config.py"):
            with self.subTest(name=name):
                text = (_SRC / name).read_text(encoding="utf-8")
                self.assertEqual(redact_secrets(text)[0], text)

    def test_redact_outgoing(self) -> None:
        self.assertNotIn("Xk9mP2qL7vR4", redact_outgoing("DB_PASSWORD=Xk9mP2qL7vR4\n"))  # gitleaks:allow


class SensitiveFileTests(unittest.TestCase):
    def test_secret_files(self) -> None:
        for name in [
            "kubeconfig",
            "home/u/.kube/config",
            "infra/prod.tfvars",
            "infra/prod.tfvars.json",
            "terraform.tfstate",
            "terraform.tfstate.backup",
            "putty.ppk",
            "cert.p12",
            "cert.pfx",
            "serviceAccountKey.json",
            "firebase-adminsdk-abc12-0123456789.json",
            "client_secret_1234.apps.googleusercontent.com.json",
            "prod.env",
            "secrets.env",
            ".env.local",
            ".vault-token",
            ".dockercfg",
            "credentials",
            "credentials.json",
            "credentials.txt",
            "auth.json",
            r"C:\repo\.ENV",
            r"C:\repo\.env::$DATA",
            r"C:\repo\.env:stream",
            r"C:\repo\.env. . ",
            r"C:\Users\me\.SSH\id_rsa",
            "ID_RSA",
        ]:
            with self.subTest(name=name):
                self.assertTrue(sensitive_file(name))

    def test_ordinary_files(self) -> None:
        for name in [
            "src/environment.ts",
            "src/auth.py",
            "src/env.py",
            "app/credentials_test.kt",
            "README.md",
            r"C:\repo\main.py",
            "C:",
            "variables.tf",
        ]:
            with self.subTest(name=name):
                self.assertFalse(sensitive_file(name))


class PersonalDataTests(unittest.TestCase):
    def assertPii(self, text: str, kind: str) -> None:
        out, count = redact_pii(text)
        self.assertIn(f"{REDACTED}{kind}]", out, text)
        self.assertGreaterEqual(count, 1, text)

    def assertKept(self, text: str) -> None:
        self.assertEqual(redact_pii(text), (text, 0))

    def test_emails(self) -> None:
        self.assertEqual(redact_pii("mail jane.doe@acme-corp.com now")[0], "mail [REDACTED:email] now")  # gitleaks:allow
        self.assertPii("<j.smith+ci@mail.corp.co.uk>", "email")  # gitleaks:allow
        for text in (
            "user@example.com", "a@sub.example.org", "root@localhost", "git@github.com:org/repo.git",
            "noreply@github.com", "x = a@self.weight", "this@Outer.field", "https://u:p@docs.gradle.org/",
            "@Override", "npm i @types/node",
        ):
            with self.subTest(text=text):
                self.assertKept(text)

    def test_phones(self) -> None:
        for text in (
            "call +1 (415) 555-2671", "tel: +14155552671", "+44 20 7946 0958", "+91 98765 43210",  # gitleaks:allow
            "(415) 555-2671", "415-555-2671", "415.555.2671", "020 7946 0958",  # gitleaks:allow
        ):
            with self.subTest(text=text):
                self.assertPii(text, "phone")

    def test_numbers_that_are_not_phones(self) -> None:
        for text in (
            "192.168.100.200", "10.0.0.1:8080", "2024-01-15 10:30:00", "2024-01-15T10:30:00.123Z", "v1.22.333.4444",
            "1.2.3", "src/main.py:120:45", "0x7fff5fbff8c0", "+1234567890123\n", "x\n+12345678901\n", "a + 12345678",
            "1700000000000000000", "timeout=120000", "uuid 123e4567-e89b-12d3-a456-426614174000",
            "sha 4f9d2c1e8b7a6f5e4d3c2b1a0f9e8d7c6b5a4f3e", "version 2.40.1.windows.1", "port 65535 10000",
        ):
            with self.subTest(text=text):
                self.assertKept(text)

    def test_cards_need_luhn_and_an_issuer_prefix(self) -> None:
        for text in ("4111 1111 1111 1111", "4111-1111-1111-1111", "4111111111111111", "378282246310005", "5555 5555 5555 4444"):  # gitleaks:allow
            with self.subTest(text=text):
                self.assertPii(text, "card number")
        for text in ("4111 1111 1111 1112", "1234567890123456", "0000 0000 0000 0000", "4111 11 11 1111 1111"):
            with self.subTest(text=text):
                self.assertKept(text)

    def test_iban_needs_mod97(self) -> None:
        self.assertEqual(redact_pii("IBAN DE89 3704 0044 0532 0130 00 ok")[0], "IBAN [REDACTED:iban] ok")  # gitleaks:allow
        self.assertPii("GB82WEST12345698765432", "iban")  # gitleaks:allow
        self.assertKept("DE89370400440532013001")
        self.assertKept("AB12CDEFGHIJKLMNOP")

    def test_national_ids(self) -> None:
        self.assertPii("ssn 123-45-6789", "ssn")  # gitleaks:allow
        for text in ("666-12-3456", "000-12-3456", "900-12-3456", "123-00-6789"):
            with self.subTest(text=text):
                self.assertKept(text)
        self.assertPii("aadhaar 2341 2341 2346", "aadhaar")  # gitleaks:allow
        self.assertKept("2341 2341 2345")  # Verhoeff check fails
        self.assertPii("PAN ABCPE1234F", "pan")  # gitleaks:allow
        self.assertKept("ABCDE1234F")  # 4th letter is not a holder type
        self.assertPii("NI AB123456C", "ni number")  # gitleaks:allow
        self.assertPii("NI AB 12 34 56 C", "ni number")  # gitleaks:allow
        self.assertKept("QQ123456C")
        self.assertKept("GB123456A")

    def test_can_be_turned_off(self) -> None:
        set_pii_redaction(False)
        self.addCleanup(set_pii_redaction, True)
        self.assertEqual(redact_outgoing("jane@corp.io 415-555-2671"), "jane@corp.io 415-555-2671")  # gitleaks:allow

    def test_this_package_has_no_personal_data(self) -> None:
        for path in sorted(_SRC.rglob("*.py")):
            if path.name == "redaction.py":
                continue  # its comments show the phone formats
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertEqual(redact_pii(text)[1], 0)


if __name__ == "__main__":
    unittest.main()
