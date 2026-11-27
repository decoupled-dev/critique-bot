import os
import unittest
from unittest import mock

from envfile import EnvFileError, parse


class HiddenExpansionTest(unittest.TestCase):
    def test_reference_earlier_key(self):
        text = "HOST=db.local\nPORT=5432\nURL=postgres://${HOST}:${PORT}/app\n"
        self.assertEqual(parse(text, {})["URL"], "postgres://db.local:5432/app")

    def test_environ_lookup_and_file_wins(self):
        env = {"USER": "alice", "HOME": "/home/alice"}
        out = parse("HOME=/srv\nA=${USER}@${HOME}\n", env)
        self.assertEqual(out["A"], "alice@/srv")

    def test_later_key_not_visible(self):
        with self.assertRaises(EnvFileError):
            parse("A=${B}\nB=1\n", {})

    def test_default_os_environ(self):
        with mock.patch.dict(os.environ, {"ENVFILE_HIDDEN_X": "from-os"}):
            self.assertEqual(parse("V=${ENVFILE_HIDDEN_X}")["V"], "from-os")

    def test_fallback(self):
        out = parse("A=${MISSING:-guest}\nE=\nB=${E:-empty used}\nC=${SET:-no}\n", {"SET": "yes"})
        self.assertEqual(out["A"], "guest")
        self.assertEqual(out["B"], "empty used")
        self.assertEqual(out["C"], "yes")
        self.assertEqual(parse("D=${NOPE:-}x", {})["D"], "x")

    def test_missing_raises_with_line_and_name(self):
        with self.assertRaises(EnvFileError) as cm:
            parse("# c\nA=1\nB=x${NOT_THERE}y\n", {})
        msg = str(cm.exception)
        self.assertIn("line 3", msg)
        self.assertIn("NOT_THERE", msg)

    def test_dollar_escapes(self):
        out = parse("P=pa$$word\nQ=cost $5\nR=$HOME\nS=$${X}\n", {"HOME": "/h", "X": "x"})
        self.assertEqual(out["P"], "pa$word")
        self.assertEqual(out["Q"], "cost $5")
        self.assertEqual(out["R"], "$HOME")
        self.assertEqual(out["S"], "${X}")

    def test_quotes(self):
        env = {"WHO": "bob"}
        out = parse("A='hi ${WHO} $$'\nB=\"hi ${WHO}\\t$$\"\nC=hi ${WHO} # c\n", env)
        self.assertEqual(out["A"], "hi ${WHO} $$")
        self.assertEqual(out["B"], "hi bob\t$")
        self.assertEqual(out["C"], "hi bob")

    def test_existing_behaviour(self):
        text = "# db\nDB_HOST=localhost\nexport DB_PORT = 5432\n\nNAME='my app'  # comment\nCOLOR=#fff\n"
        self.assertEqual(parse(text, {}),
                         {"DB_HOST": "localhost", "DB_PORT": "5432", "NAME": "my app", "COLOR": "#fff"})
        with self.assertRaisesRegex(EnvFileError, "line 2"):
            parse("A=1\nB='open", {})

    def test_expanded_text_not_expanded_again(self):
        out = parse("A=$${B}\nC=${A}\n", {"B": "b"})
        self.assertEqual(out["A"], "${B}")
        self.assertEqual(out["C"], "${B}")


if __name__ == "__main__":
    unittest.main()
