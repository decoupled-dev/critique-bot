import io
import tempfile
import unittest
from pathlib import Path

from taskq import TaskStore
from taskq.cli import main


class StoreTest(unittest.TestCase):
    def test_add_and_list(self):
        s = TaskStore()
        s.add("  Buy   milk ")
        s.add("Call Bob")
        self.assertEqual([t.title for t in s.list()], ["Buy milk", "Call Bob"])

    def test_done_hidden(self):
        s = TaskStore()
        a = s.add("a")
        s.add("b")
        s.complete(a.id)
        self.assertEqual([t.title for t in s.list()], ["b"])
        self.assertEqual(len(s.list(include_done=True)), 2)

    def test_save_load(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "todo.json"
            s = TaskStore()
            s.add("x")
            s.save(p)
            self.assertEqual(TaskStore.load(p).list()[0].title, "x")

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            p = str(Path(d) / "todo.json")
            main(["--file", p, "add", "Water plants"], out=io.StringIO())
            buf = io.StringIO()
            main(["--file", p, "list"], out=buf)
            self.assertIn("Water plants", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
