"""The style/bible web-chat asks pass completeness checks, so a stuck
'generating' flag cannot cost the 240 s dead wait (data/logs evidence:
channel_look was the only stage hitting it)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from whisperradar.video import parsing  # noqa: E402
from whisperradar.video.explainer import themes  # noqa: E402
from whisperradar.video.explainer.layout import layout_scene  # noqa: E402
from whisperradar.video.explainer.validate import ExplainerError  # noqa: E402
from whisperradar.video.explainer.validate import load as load_doc  # noqa: E402
from whisperradar import channel_look  # noqa: E402


class StyleReadyTests(unittest.TestCase):
    def test_complete_guide_passes(self):
        guide = ("## Voice & Tone\nConfident, direct.\n\n## Pacing & Rhythm\n"
                 "Fast.\n\n## Things to Avoid\nNo sponsor reads. "
                 + "Substantive body text. " * 120)
        self.assertTrue(channel_look.style_guide_ready(guide))

    def test_truncated_guide_fails(self):
        # stopped right after the first section: keep waiting
        self.assertFalse(channel_look.style_guide_ready(
            "## Voice & Tone\nConfident.\n\n## Pacing & Rhythm\nFast."
            + "padding " * 100))

    def test_case_insensitive_and_length_floor(self):
        self.assertTrue(channel_look.style_guide_ready(
            "voice & tone\n" + "x" * 2500 + "\nthings to avoid"))
        self.assertFalse(channel_look.style_guide_ready(
            "voice & tone\nthings to avoid"))   # too short to be a guide

    def test_empty_and_none_fail(self):
        self.assertFalse(channel_look.style_guide_ready(""))
        self.assertFalse(channel_look.style_guide_ready(None))


class BibleReadyTests(unittest.TestCase):
    def test_both_markers_and_bible_body(self):
        text = (channel_look.STYLE_MARK + "\nA visual style.\n"
                + channel_look.BIBLE_MARK + "\nThe recurring character is a "
                "detective in a grey coat, three-day stubble and a worn "
                "leather jacket; the city at night, rain, neon reflections. "
                "The main environment is a small apartment with warm lamps, "
                "a cluttered desk and a wall of case notes. Recurring props: "
                "a magnifier, a pocket watch, an old radio.")
        self.assertTrue(channel_look._bible_ready(text))

    def test_missing_bible_marker_fails(self):
        self.assertFalse(channel_look._bible_ready(
            channel_look.STYLE_MARK + "\nA visual style only."))

    def test_thin_bible_fails(self):
        self.assertFalse(channel_look._bible_ready(
            channel_look.STYLE_MARK + "\nA visual style.\n"
            + channel_look.BIBLE_MARK + "\nhmm"))


class AskWiringTests(unittest.TestCase):
    """The ask helpers must pass the ready checks to the transport."""

    def test_ask_writing_passes_style_ready(self):
        seen = {}

        class FakeTransport:
            def ask(self, site, prompt, files=(), new_chat=True, ready=None):
                seen["ready"] = ready
                # a complete guide straight away
                return ("## Voice & Tone\nDirect, no filler.\n\n"
                        "## Things to Avoid\nNo ads. " * 40)

        import tempfile
        from whisperradar import db, studio
        cfg = studio.__dict__ and None
        cfg = type("Cfg", (), {"db_path": os.path.join(
            tempfile.mkdtemp(), "wr.db")})()
        videos = [{"title": "T1", "transcript_path": os.path.join(
            tempfile.mkdtemp(), "tr.txt")}]
        with open(videos[0]["transcript_path"], "w", encoding="utf-8") as fh:
            fh.write("word " * 60)
        reply = channel_look._ask_writing(cfg, FakeTransport(), "chatgpt",
                                          "My Channel", "general", videos,
                                          "", lambda m: None)
        self.assertTrue(seen["ready"] is channel_look.style_guide_ready)
        self.assertIn("Direct", reply)

    def test_ask_passes_bible_ready(self):
        seen = {}

        class FakeTransport:
            def ask(self, site, prompt, files=(), new_chat=True, ready=None):
                seen["ready"] = ready
                return (channel_look.STYLE_MARK + "\nA look.\n"
                        + channel_look.BIBLE_MARK + "\nA long enough bible "
                        "body for the check to pass.")

        style, bible = channel_look._ask(
            FakeTransport(), "chatgpt", "My Channel", [{"title": "T1"}],
            None, [], "", lambda m: None)
        self.assertTrue(seen["ready"] is channel_look._bible_ready)
        self.assertEqual(bible.strip().startswith("A long enough"), True)


if __name__ == "__main__":
    unittest.main()
