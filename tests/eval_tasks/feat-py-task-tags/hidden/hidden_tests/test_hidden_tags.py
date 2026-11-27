import io
import json
import tempfile
import unittest
from pathlib import Path

from taskq import Task, TaskStore
from taskq.cli import main


class HiddenTagsTest(unittest.TestCase):
    def test_tags_parsed_from_title(self):
        s = TaskStore()
        t = s.add("Buy milk #home #Urgent")
        self.assertEqual(t.title, "Buy milk")
        self.assertEqual(t.tags, ["home", "urgent"])

    def test_tags_in_middle_and_duplicates(self):
        s = TaskStore()
        t = s.add("Fix  #work the   #Work printer #ops-team now #a_b")
        self.assertEqual(t.title, "Fix the printer now")
        self.assertEqual(t.tags, ["work", "ops-team", "a_b"])

    def test_hash_not_a_tag(self):
        s = TaskStore()
        t = s.add("Learn C# basics # 5")
        self.assertEqual(t.title, "Learn C# basics # 5")
        self.assertEqual(t.tags, [])

    def test_no_tags_default(self):
        self.assertEqual(TaskStore().add("plain").tags, [])
        self.assertEqual(Task(id=1, title="x").tags, [])
        a, b = Task(id=1, title="a"), Task(id=2, title="b")
        a.tags.append("q")
        self.assertEqual(b.tags, [])

    def test_list_by_tag(self):
        s = TaskStore()
        s.add("a #home")
        b = s.add("b #work #home")
        s.add("c #work")
        s.add("d")
        self.assertEqual([t.title for t in s.list(tag="home")], ["a", "b"])
        self.assertEqual([t.title for t in s.list(tag="WORK")], ["b", "c"])
        self.assertEqual([t.title for t in s.list(tag="#work")], ["b", "c"])
        self.assertEqual(s.list(tag="nope"), [])
        s.complete(b.id)
        self.assertEqual([t.title for t in s.list(tag="home")], ["a"])
        self.assertEqual([t.title for t in s.list(tag="home", include_done=True)], ["a", "b"])
        self.assertEqual(len(s.list()), 3)

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "todo.json"
            s = TaskStore()
            s.add("Pay rent #home #money")
            s.save(p)
            t = TaskStore.load(p).list()[0]
            self.assertEqual(t.title, "Pay rent")
            self.assertEqual(t.tags, ["home", "money"])

    def test_old_file_loads(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "todo.json"
            p.write_text(json.dumps({"version": 1, "tasks": [
                {"id": 1, "title": "old task", "done": False},
                {"id": 2, "title": "old done", "done": True}]}), encoding="utf-8")
            s = TaskStore.load(p)
            self.assertEqual([t.tags for t in s.list(include_done=True)], [[], []])
            self.assertEqual(s.add("new #x").id, 3)

    def test_cli_tag_filter_and_display(self):
        with tempfile.TemporaryDirectory() as d:
            p = str(Path(d) / "todo.json")
            main(["--file", p, "add", "Water plants #home #garden"], out=io.StringIO())
            main(["--file", p, "add", "Send report #work"], out=io.StringIO())
            buf = io.StringIO()
            main(["--file", p, "list", "--tag", "home"], out=buf)
            out = buf.getvalue()
            self.assertIn("Water plants [home, garden]", out)
            self.assertNotIn("Send report", out)
            buf = io.StringIO()
            main(["--file", p, "list"], out=buf)
            self.assertIn("Send report [work]", buf.getvalue())
            self.assertIn("Water plants", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
