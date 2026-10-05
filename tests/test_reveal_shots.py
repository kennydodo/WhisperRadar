"""Reveal shots (one image, 2-4 items shown one at a time): the brief section
(channel opt-in), the objective checks on the plan, the pacing exemption, the
strict judge text and the channel setting.

Run: python -m unittest tests.test_reveal_shots
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import briefs, channel_io, db, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


def _ts(t):
    m, s = divmod(int(t), 60)
    return f"00:{m:02d}:{s:02d},000"


def _cues(count, secs):
    return [{"index": i + 1, "start": _ts(i * secs), "end": _ts((i + 1) * secs),
             "text": f"cue {i + 1}"} for i in range(count)]


def _shot(asset="S01_01_CMP_ST.png", cues="1-3", reveal=(1, 2, 3), motion="ST"):
    shot = {"cues": cues, "asset": asset, "scene": "S01", "motion": motion}
    if reveal is not None:
        shot["reveal"] = list(reveal)
    return shot


class BriefTests(unittest.TestCase):
    def setUp(self):
        self.template = briefs.read_template()

    def test_reveal_section_only_for_a_channel_that_enabled_it(self):
        off = briefs.render_brief(self.template, briefs.resolve_profile(None))
        on = briefs.render_brief(
            self.template, briefs.resolve_profile(None, reveal=1))
        self.assertNotIn("REVEAL SHOTS", off)
        self.assertIn("SECTION 7B", on)
        self.assertIn('"reveal": [c1, c2, c3]', on)
        # placed between the output document and the text-in-images section
        self.assertLess(on.index("SECTION 7B"), on.index("## SECTION 8 "))
        self.assertGreater(on.index("SECTION 7B"), on.index("## SECTION 7 "))

    def test_every_preset_still_renders_with_reveal_on(self):
        for key in briefs.MOTION_PRESETS:
            profile = briefs.resolve_profile(key, reveal=True)
            text = briefs.render_brief(self.template, profile, "Host opens.")
            self.assertNotIn("{{", text, key)
            self.assertIn("SECTION 7B", text, key)


class FaultTests(unittest.TestCase):
    def faults(self, **kw):
        return studio.shotlist_reveal_faults([_shot(**kw)])

    def test_a_valid_reveal_has_no_faults(self):
        self.assertEqual(self.faults(), [])
        self.assertEqual(studio.shot_reveal({"reveal": {"cues": [4, 5]}}), [4, 5])

    def test_shots_without_reveal_are_ignored(self):
        self.assertEqual(self.faults(reveal=None, motion="ZI"), [])

    def test_wrong_item_count_or_shape(self):
        self.assertTrue(self.faults(reveal=[1]))
        self.assertTrue(self.faults(reveal=[1, 2, 3, 4, 5], cues="1-5"))
        self.assertTrue(self.faults(reveal=[1, "x"]))

    def test_cues_must_increase_start_on_the_first_cue_and_stay_inside(self):
        self.assertTrue(self.faults(reveal=[1, 3, 2]))
        self.assertTrue(self.faults(reveal=[2, 3], cues="1-3"))
        self.assertTrue(self.faults(reveal=[1, 4], cues="1-3"))

    def test_a_reveal_shot_must_be_static(self):
        self.assertTrue(self.faults(motion="ZI", asset="S01_01_CMP_ZI.png"))
        self.assertTrue(self.faults(motion="ST", asset="S01_01_CMP_ZI.png"))

    def test_structural_faults_include_reveal_faults(self):
        data = {"shots": [_shot(reveal=[1, 1])],
                "images": [{"file": "S01_01_CMP_ST.png", "prompt": "p"}]}
        faults = studio.shotlist_structural_faults(data, 3)
        self.assertTrue(any("must increase" in f for f in faults))


class PacingTests(unittest.TestCase):
    def test_a_long_reveal_is_not_a_long_static_hold_or_st_share(self):
        cues = _cues(6, 5)          # two 15 s shots
        data = {"shots": [_shot("S01_01_CMP_ST.png", "1-3", (1, 2, 3)),
                          _shot("S01_02_CMP_ST.png", "4-6", (4, 5, 6))],
                "images": []}
        faults, _ = studio.shotlist_pacing(data, cues, 20.0)
        self.assertEqual(faults, [])

    def test_the_same_shots_without_reveal_are_rejected(self):
        cues = _cues(6, 5)
        data = {"shots": [_shot("S01_01_CMP_ST.png", "1-3", None),
                          _shot("S01_02_CMP_ST.png", "4-6", None)],
                "images": []}
        self.assertTrue(studio.shotlist_pacing(data, cues, 20.0)[0])

    def test_a_reveal_shot_still_obeys_the_max_hold(self):
        cues = _cues(6, 5)          # one 30 s shot
        data = {"shots": [_shot("S01_01_CMP_ST.png", "1-6", (1, 3, 5))],
                "images": []}
        faults, _ = studio.shotlist_pacing(data, cues, 12.0)
        self.assertTrue(any("hold longer" in f for f in faults))

    def test_a_static_only_channel_does_not_reject_the_reveal_marker(self):
        cues = _cues(6, 5)
        data = {"shots": [_shot("S01_01_CMP_ST.png", "1-3", (1, 2, 3)),
                          _shot("S01_02_CMP_ST.png", "4-6", None)],
                "images": []}
        faults, _ = studio.shotlist_pacing(data, cues, 20.0, briefs.STATIC)
        self.assertEqual(faults, [])


class JudgeTests(unittest.TestCase):
    cue_text = {1: "First cause.", 2: "Second cause.", 3: "Third cause."}

    def test_reveal_shots_get_the_item_lines_and_the_strict_rules(self):
        chunk = [{"asset": "S01_01_CMP_ST.png", "cues": "1-3", "reveal": [1, 2, 3],
                  "prompt": "three items"}]
        text = studio.alignment_prompt(chunk, self.cue_text)
        self.assertIn("REVEAL SHOT, 3 items", text)
        self.assertIn('item 2 (slice 2 of 3) at cue 2: "Second cause."', text)
        self.assertIn("REVEAL SHOTS. A shot marked REVEAL SHOT", text)
        self.assertIn("slice line", text)

    def test_plain_shots_get_no_reveal_text(self):
        chunk = [{"asset": "S01_01_SCN_ZI.png", "cues": "1-3", "prompt": "p"}]
        text = studio.alignment_prompt(chunk, self.cue_text)
        self.assertNotIn("REVEAL", text)

    def test_the_patch_prompt_keeps_the_layout(self):
        weak = [{"asset": "S01_01_CMP_ST.png", "missing": ["item 2 not in slice 2"],
                 "reason": "", "narration": "x", "reveal": [1, 2, 3]}]
        text = studio.shotlist_patch_prompt(weak)
        self.assertIn("REVEAL SHOT with 3 items", text)
        self.assertIn("nothing may cross a slice line", text)


class ChannelSettingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = load_config(None)
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        self.addCleanup(self.conn.close)
        self.oc = db.create_own_channel(self.conn, "Alpha")

    def test_the_brief_form_saves_and_clears_the_setting(self):
        app = create_app(self.cfg)
        client = app.test_client()
        client.post("/my-channels/brief",
                    data={"id": self.oc, "brief_reveal": "1"})
        row = db.get_own_channel(self.conn, self.oc)
        self.assertEqual(row["brief_reveal"], 1)
        client.post("/my-channels/brief",
                    data={"id": self.oc, "brief_reveal": ""})
        row = db.get_own_channel(self.conn, self.oc)
        self.assertIsNone(row["brief_reveal"])

    def test_the_setting_is_exported_and_imported(self):
        db.update_own_channel(self.conn, self.oc, brief_reveal=1)
        doc = json.loads(json.dumps(channel_io.export_channels(self.conn)))
        self.assertEqual(doc["channels"][0]["brief_reveal"], 1)
        db.remove_own_channel(self.conn, self.oc)
        channel_io.import_channels(self.conn, self.cfg, doc)
        self.assertEqual(db.get_own_channel(self.conn, "Alpha")["brief_reveal"], 1)


if __name__ == "__main__":
    unittest.main()
