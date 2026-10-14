"""The 'time beat' style (default) vs the 'classic' style kept as it was."""
import unittest

from whisperradar import studio

GUIDE = """## Voice
x
## Structure (time beats: roles in order, timing as % of runtime)
1. **Hook** (0-5%) - cold open
2. **Reframe** (5-12%) - the claim
3. **Mechanism** (12-35%) - how it works
4. **Payoff** (35-100%) - land it
## CTA
y"""

OLD_GUIDE = """## Structure (beats in order, with rough timing)
1. **Cold open** (0:00-0:45) - a scene
2. **Thesis** (0:45-1:30) - the 20 habits
3. **Habits** (1:30-40:00) - one by one
## CTA
y"""


class TimeBeatTests(unittest.TestCase):
    def test_default_style_analysis_asks_for_role_beats_and_percentages(self):
        p = studio.style_prompt("T", "g", "a transcript", word_count=3000)
        self.assertIn("time beats: roles in order, timing as % of runtime", p)
        self.assertIn("TIME BEATS. In the Structure section", p)
        self.assertIn("never minutes or seconds", p)
        self.assertIn("No beat may be a sponsor", p)
        self.assertNotIn("rough timing", p)
        self.assertNotIn("reflect this in the Structure section", p)

    def test_from_scratch_default_is_time_beat_too(self):
        self.assertIn("TIME BEATS. In the Structure section",
                      studio.style_prompt_from_scratch("T", "g"))

    def test_classic_is_kept_as_it_was(self):
        p = studio.style_prompt("T", "g", "a transcript", word_count=3000,
                                variant="classic")
        self.assertIn("beats in order, with rough timing", p)
        self.assertIn("reflect this in the Structure section", p)
        self.assertNotIn("TIME BEATS. In the Structure", p)
        s = studio.style_prompt_from_scratch("T", "g", variant="classic")
        self.assertIn("rough timing", s)

    def test_word_table_turns_percentages_into_words(self):
        t = studio.beat_word_table(GUIDE, 1200)
        self.assertIn("- 0-5% = about words 0-60: Hook: cold open", t)
        self.assertIn("- 35-100% = about words 420-1200: Payoff: land it", t)
        self.assertNotIn("()", t)

    def test_old_format_guides_give_no_table(self):
        self.assertEqual(studio.beat_word_table(OLD_GUIDE, 1200), "")
        self.assertEqual(studio.beat_word_table("", 1200), "")

    def test_writer_prompt_has_no_structure_contradiction(self):
        p = studio.script_prompt("T", "g", "facts", GUIDE, target_words=1200)
        self.assertIn("TIME BEATS FOR THIS SCRIPT (about 1200 words", p)
        flat = " ".join(p.split())
        self.assertIn("a rhythm to follow, never the story", flat)
        self.assertIn("fill every beat with YOUR own story", flat)
        self.assertNotIn("structure, and CTA style all come from it", p)
        c = studio.script_prompt("T", "g", "facts", GUIDE, target_words=1200,
                                 style_variant="classic")
        self.assertIn("structure, and CTA style all come from it", c)
        self.assertNotIn("TIME BEATS FOR THIS SCRIPT", c)


if __name__ == "__main__":
    unittest.main()


class StyleVariantSettingTests(unittest.TestCase):
    def test_default_and_saved_choice(self):
        import tempfile
        from pathlib import Path
        from whisperradar import db, studio

        class Cfg:
            pass
        with tempfile.TemporaryDirectory() as d:
            cfg = Cfg()
            cfg.db_path = Path(d) / "t.db"
            self.assertEqual(studio.style_variant(cfg), "time-beat")
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            db.set_setting(conn, "style_variant", "classic")
            self.assertEqual(studio.style_variant(conn=conn), "classic")
            db.set_setting(conn, "style_variant", "bogus")
            self.assertEqual(studio.style_variant(conn=conn), "time-beat")
            conn.close()
