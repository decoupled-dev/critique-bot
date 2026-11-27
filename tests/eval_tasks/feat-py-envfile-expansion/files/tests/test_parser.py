import unittest

from envfile import EnvFileError, parse


class ParserTest(unittest.TestCase):
    def test_basic(self):
        text = "# db\nDB_HOST=localhost\nexport DB_PORT = 5432\n\nNAME='my app'  # comment\n"
        self.assertEqual(parse(text, {}), {"DB_HOST": "localhost", "DB_PORT": "5432", "NAME": "my app"})

    def test_double_quotes_escapes(self):
        self.assertEqual(parse('MSG="a\\tb\\n\\"c\\""', {}), {"MSG": 'a\tb\n"c"'})

    def test_inline_comment_needs_space(self):
        self.assertEqual(parse("COLOR=#fff\nX=1 # one", {}), {"COLOR": "#fff", "X": "1"})

    def test_errors(self):
        with self.assertRaises(EnvFileError):
            parse("NOEQUALS", {})
        with self.assertRaises(EnvFileError):
            parse("1BAD=x", {})
        with self.assertRaisesRegex(EnvFileError, "line 2"):
            parse("A=1\nB='open", {})


if __name__ == "__main__":
    unittest.main()
