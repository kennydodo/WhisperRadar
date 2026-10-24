"""Qwen's thinking mode (Auto / Thinking / Fast) is a menu opened by the
.qwen-thinking-selector control; checked live on chat.qwen.ai 2026-10-24.
(The "Select Mode" button on that page is the + tools menu, NOT thinking.)"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whisperradar import webchat  # noqa: E402

OPEN = ".qwen-thinking-selector .qwen-chat-v2-dropdown-menu-select"
QWEN = {
    "key": "qwen", "name": "Qwen", "url": "https://chat.qwen.ai/",
    "level_label": "Thinking mode", "paid": False, "models": [],
    "levels": [{"id": m.lower(), "label": m, "open": OPEN, "item": m,
                "current": "Auto"} for m in ("Thinking", "Fast", "Auto")],
    "toggles": [], "port": 9235,
}


class Page:
    def __init__(self):
        self.calls = []

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, js, arg=None):
        if js is webchat._CLICK_TEXT_JS:
            self.calls.append(("open", arg["open"]))
            return "opened"
        if js is webchat._PICK_ITEM_JS:
            self.calls.append(("pick", arg))
            # the menu is closed until it has been opened
            return "clicked" if any(c[0] == "open" for c in self.calls) else "missing"
        return None


class QwenLevelTests(unittest.TestCase):
    def test_the_levels_survive_sanitising(self):
        clean, problems = webchat.sanitize_sites([QWEN])
        self.assertEqual(problems, [])
        self.assertEqual([x["label"] for x in clean[0]["levels"]],
                         ["Thinking", "Fast", "Auto"])
        self.assertEqual(clean[0]["levels"][0]["open"], OPEN)

    def test_thinking_is_opened_from_the_thinking_selector_and_picked(self):
        clean, _ = webchat.sanitize_sites([QWEN])
        page = Page()
        note = webchat._generic_prepare(clean[0])(page, level="thinking")
        self.assertIn(("open", OPEN), page.calls)
        self.assertIn(("pick", "Thinking"), page.calls)
        self.assertIn("Thinking", note)

    def test_no_level_asked_leaves_the_page_alone(self):
        clean, _ = webchat.sanitize_sites([QWEN])
        page = Page()
        webchat._generic_prepare(clean[0])(page)
        self.assertEqual(page.calls, [])


if __name__ == "__main__":
    unittest.main()
