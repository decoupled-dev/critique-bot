"""The reply is read with its whitespace intact (innerText collapses indentation), and XML edits are checked."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from critique_bot import agent_edit, agent_tools, chat_client


def _chrome() -> str:
    for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return ""


CHROME = os.environ.get("CRIT_TEST_CHROME") or _chrome()

# The two layouts seen on chatgpt.com: plain text with literal newlines (signed out),
# and rendered markdown (signed in), each inside a turn <li> that the selector also matches.
_PLAIN = """<ol><li data-message-role="assistant"><h4 data-message-attribution>ChatGPT said:</h4>
<div data-assistant-markdown><section><p style="white-space: pre-line">&lt;tool_call&gt;
{"tool": "edit_file", "arguments": {"old_string": "    &lt;item /&gt;\\n", "note": "a\tb"}}
&lt;/tool_call&gt;</p></section></div></li></ol>"""
_MARKDOWN = """<ol><li data-message-role="assistant"><h4 data-message-attribution>ChatGPT said:</h4>
<div data-assistant-markdown><p>Here:</p><pre><div><span>xml</span><button>Copy code</button></div><code class="language-xml">&lt;a&gt;
    &lt;b/&gt;
&lt;/a&gt;
</code></pre><p>Use <code>adb logcat</code> and <em>this</em>.</p><ol><li>one</li><li>two</li></ol></div></li></ol>"""
SELECTOR = "[data-assistant-markdown], [data-message-role='assistant']"


@unittest.skipUnless(CHROME, "needs Chrome or Edge (set CRIT_TEST_CHROME)")
class RealBrowserTextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from playwright.sync_api import sync_playwright

        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=CHROME, headless=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def _page(self, html: str):
        page = self.browser.new_page()
        page.set_content(html)
        self.addCleanup(page.close)
        return page

    def test_plain_reply_keeps_spaces_and_tabs(self) -> None:
        page = self._page(_PLAIN)
        state = chat_client._reply_state(page, SELECTOR, with_text=True)
        self.assertIsNotNone(state)
        self.assertEqual(state["count"], 1)  # the turn and its markdown are one reply
        self.assertEqual(
            state["text"],
            '<tool_call>\n{"tool": "edit_file", "arguments": {"old_string": "    <item />\\n", "note": "a\tb"}}\n</tool_call>',
        )
        self.assertNotIn("ChatGPT said", state["text"])
        inner = page.evaluate("() => document.querySelector('[data-assistant-markdown]').innerText")
        self.assertNotIn('"    <item', inner)  # what the old reader saw

    def test_rendered_markdown_is_rebuilt(self) -> None:
        page = self._page(_MARKDOWN)
        text = chat_client._reply_state(page, SELECTOR, with_text=True)["text"]
        self.assertEqual(
            text,
            "Here:\n```xml\n<a>\n    <b/>\n</a>\n```\nUse `adb logcat` and *this*.\n1. one\n2. two",
        )

    def test_turn_without_content_reads_empty(self) -> None:
        page = self._page('<ol><li data-message-role="assistant"><h4 data-message-attribution>ChatGPT said:</h4></li></ol>')
        state = chat_client._reply_state(page, SELECTOR, with_text=True)
        self.assertEqual((state["count"], state["length"], state["text"]), (1, 0, ""))

    def test_element_text(self) -> None:
        page = self._page(_PLAIN)
        self.assertIn('"    <item />', chat_client._element_text(page.locator("[data-assistant-markdown]")))


class ScriptTextTests(unittest.TestCase):
    def test_reader_script_is_raw(self) -> None:
        # A non-raw Python string turned "\n" in the regexes into real newlines and broke the script.
        self.assertIn(r"\n", chat_client._REPLY_TEXT_FN)
        self.assertNotIn("\n{3,}", chat_client._REPLY_TEXT_FN.replace(r"\n{3,}", ""))


class XmlCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "res").mkdir()
        self.strings = self.root / "res" / "strings.xml"
        self.strings.write_bytes(b"<resources>\r\n    <string name=\"a\">A</string>\r\n</resources>\r\n")
        self.ctx = agent_tools.ToolContext(workspace=self.root, state=agent_tools.TaskState(task="t"))
        agent_tools.execute("read_files", {"path": "res/strings.xml"}, self.ctx)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_messages(self) -> None:
        self.assertEqual(agent_edit.syntax_check("a.xml", "<a><b/></a>"), "ok")
        self.assertIn("&amp;", agent_edit.syntax_check("s.xml", "<r><s>Tom & Jerry</s></r>"))
        self.assertIn("mismatched tag", agent_edit.syntax_check("l.xml", "<a>\n<b></a>"))
        self.assertIn("xmlns:android", agent_edit.syntax_check("l.xml", '<View android:id="x"/>'))

    def test_breaking_edit_is_refused_and_good_edit_keeps_crlf(self) -> None:
        bad = agent_tools.execute(
            "edit_file",
            {"path": "res/strings.xml", "old_string": '    <string name="a">A</string>', "new_string": '    <string name="a">A & B</string>'},
            self.ctx,
        )
        self.assertFalse(bad["ok"])
        self.assertIn("&amp;", bad["error"])
        good = agent_tools.execute(
            "edit_file",
            {"path": "res/strings.xml", "old_string": '    <string name="a">A</string>\n',
             "new_string": '    <string name="a">A</string>\n    <string name="b">A &amp; B</string>\n'},
            self.ctx,
        )
        self.assertTrue(good["ok"], good)
        self.assertEqual(
            self.strings.read_bytes(),
            b'<resources>\r\n    <string name="a">A</string>\r\n    <string name="b">A &amp; B</string>\r\n</resources>\r\n',
        )

    def test_rewrite_that_breaks_xml_is_refused(self) -> None:
        result = agent_tools.execute(
            "write_files", {"path": "res/strings.xml", "contents": "<resources>\n<string>x</resources>\n"}, self.ctx
        )
        self.assertFalse(result["ok"])
        self.assertIn("not replaced", result["error"])
        self.assertIn(b'name="a"', self.strings.read_bytes())


if __name__ == "__main__":
    unittest.main()
