import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whisperradar import elements  # noqa: E402

REPLY = ("json\nCopy\n```json\n" + json.dumps({"elements": [
    {"name": "Marcus", "kind": "Character", "description": "A tall man in a grey coat."},
    {"name": "marcus", "kind": "character", "description": "duplicate name"},
    {"name": "The bank", "kind": "place", "description": "A glass-fronted branch."},
    {"name": "Idea", "kind": "concept", "description": "not drawable"},
    {"name": "", "kind": "object", "description": "no name"},
    {"name": "Coin", "kind": "object", "description": "A thick gold coin."},
]}) + "\n```")


class ParseTests(unittest.TestCase):
    def test_keeps_valid_drops_bad_and_duplicate(self):
        found = elements.parse(REPLY)
        self.assertEqual([e["name"] for e in found], ["Marcus", "The bank", "Coin"])
        self.assertEqual(found[0]["kind"], "character")

    def test_nothing_usable_gives_an_empty_list(self):
        self.assertEqual(elements.parse("sorry, I cannot do that"), [])
        self.assertEqual(elements.parse('{"elements": "none"}'), [])

    def test_a_bare_list_is_accepted(self):
        self.assertEqual(len(elements.parse(
            '[{"name": "A", "kind": "object", "description": "d"}]')), 1)

    def test_long_descriptions_are_trimmed_and_the_list_is_capped(self):
        many = [{"name": f"n{i}", "kind": "object", "description": "x" * 900}
                for i in range(50)]
        found = elements.parse(json.dumps({"elements": many}))
        self.assertEqual(len(found), elements.MAX_ELEMENTS)
        self.assertEqual(len(found[0]["description"]), elements.MAX_DESCRIPTION)


class FilesAndPromptTests(unittest.TestCase):
    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(elements.load_text(d), "")
            elements.save(Path(d), elements.parse(REPLY))
            text = elements.load_text(d)
            self.assertIn("## Characters", text)
            self.assertIn("- **Marcus**: A tall man in a grey coat.", text)
            self.assertIn("## Places", text)
            self.assertTrue((Path(d) / elements.JSON_FILE).exists())

    def test_planner_block_is_empty_without_elements(self):
        self.assertEqual(elements.planner_block(""), "")
        self.assertIn("INPUT 4 - ELEMENTS", elements.planner_block("- **A**: x"))
        self.assertIn("- **A**: x", elements.planner_block("- **A**: x"))

    def test_prompt_carries_the_script_and_the_style(self):
        text = elements.prompt("Marcus walked into the bank.", "Flat 2D doodles")
        self.assertIn("Marcus walked into the bank.", text)
        self.assertIn("Flat 2D doodles", text)
        self.assertIn('"elements"', text)


if __name__ == "__main__":
    unittest.main()


class WebChatJobTests(unittest.TestCase):
    def test_elements_follow_recorded_narration_and_save_provenance(self):
        ws, pdir, asked, t = self._run([REPLY], script="A completely different newer script.")
        (pdir / "subtitles.srt").write_text(
            "1\n00:00:00,000 --> 00:00:04,000\nMarcus walked into the recorded bank.\n", encoding="utf-8")
        messages = []
        ws.run_elements(None, 1, t, "zai", log=messages.append)
        self.assertIn("Marcus walked into the recorded bank.", asked[0][2])
        self.assertNotIn("different newer script", asked[0][2])
        self.assertTrue(any("mismatch" in m for m in messages))
        self.assertEqual(json.loads((pdir / "elements_source.json").read_text())["source"], "subtitles")
    """run_elements with a fake chat transport: saves the list, retries once."""

    def _run(self, replies, script="Marcus walks into the bank."):
        from unittest import mock
        from whisperradar import webstages as ws
        d = tempfile.mkdtemp()
        pdir = Path(d)
        (pdir / "script.md").write_text(script, encoding="utf-8")
        asked = []

        class T:
            def ask(self, site, text, files, new_chat=True, ready=None):
                asked.append((site, new_chat, text))
                return replies.pop(0)

        ctx = {"pdir": pdir, "visual_style": "Flat doodles"}
        patcher = mock.patch.object(ws.ep, "_context", return_value=ctx)
        patcher.start()
        self.addCleanup(patcher.stop)
        dump = mock.patch.object(ws, "_dump")
        dump.start()
        self.addCleanup(dump.stop)
        return ws, pdir, asked, T()

    def test_saves_the_list(self):
        ws, pdir, asked, t = self._run([REPLY])
        found = ws.run_elements(None, 1, t, "zai", log=lambda m: None)
        self.assertEqual(len(found), 3)
        self.assertIn("Marcus", (pdir / "elements.md").read_text(encoding="utf-8"))
        self.assertEqual(len(asked), 1)

    def test_one_retry_then_success(self):
        ws, pdir, asked, t = self._run(["I cannot open files", REPLY])
        found = ws.run_elements(None, 1, t, "zai", log=lambda m: None)
        self.assertEqual(len(found), 3)
        self.assertEqual(len(asked), 2)
        self.assertFalse(asked[1][1])            # the retry stays in the same chat

    def test_gives_up_after_the_retry(self):
        ws, pdir, asked, t = self._run(["no", "still no"])
        with self.assertRaises(ws.StageFailed):
            ws.run_elements(None, 1, t, "zai", log=lambda m: None)
        self.assertFalse((pdir / "elements.md").exists())

    def test_no_script_fails_early(self):
        ws, pdir, asked, t = self._run([REPLY], script="")
        with self.assertRaises(ws.StageFailed):
            ws.run_elements(None, 1, t, "zai", log=lambda m: None)
        self.assertEqual(asked, [])
