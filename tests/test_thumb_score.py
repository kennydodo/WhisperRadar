"""thumb_score: reviewer verdict + phone-size metrics as one 0-100."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_external_prompts import Base       # noqa: E402
from tests.test_thumbnails import three, png       # noqa: E402
from whisperradar import thumbnails as th          # noqa: E402

GOOD = {"contrast": 55.0, "saturation": 70.0, "brightness": 120.0,
        "warnings": []}
FLAT = {"contrast": 20.0, "saturation": 40.0, "brightness": 30.0,
        "warnings": ["flat at phone size (low contrast)",
                     "washed out (low colour punch)", "very dark"]}


class ScoreMathTests(unittest.TestCase):
    def test_no_evidence_gives_no_number(self):
        self.assertIsNone(th.thumb_score({}, None, None))

    def test_both_halves_add_up_and_a_device_is_noted(self):
        got = th.thumb_score({"emphasis": "ellipse"}, 10, GOOD)
        self.assertEqual(got["score"], 100)
        self.assertEqual(got["label"], "strong")
        self.assertIn("reviewer 10/10", got["why"])
        self.assertIn("attention device: ellipse", got["why"])

    def test_flat_at_phone_size_is_penalised(self):
        got = th.thumb_score({}, 10, FLAT)
        self.assertEqual(got["score"], 67)              # 60 + (40-33)
        self.assertIn("flat at phone size (low contrast)", got["why"])

    def test_a_missing_half_is_scaled_over_100_and_said(self):
        only_local = th.thumb_score({}, None, FLAT)
        self.assertEqual(only_local["score"], 18)       # 7 of 40 -> 17.5->18
        self.assertIn("no reviewer score yet", only_local["why"])
        only_judge = th.thumb_score({}, 5, None)
        self.assertEqual(only_judge["score"], 50)
        self.assertEqual(only_judge["label"], "ok")


class ComposeAllScoreTests(Base):
    def setUp(self):
        super().setUp()
        data = th.empty_thumbs()
        cs = th.parse_concepts(three())
        for c in cs:
            art = png(self.pdir / "thumbnails" / "art" / f"{c['id']}.png")
            c["art_file"] = str(art.relative_to(self.pdir)).replace("\\", "/")
        data["concepts"] = cs
        data["score"] = 9.0
        th.save_thumbs(self.pdir, data)

    def test_compose_all_stores_a_score_for_every_final(self):
        done = th.compose_all(self.pdir)
        self.assertEqual(len(done), 3)
        for c in th.load_thumbs(self.pdir)["concepts"]:
            self.assertTrue(c["final"])
            self.assertIsNotNone(c["thumb_score"])
            self.assertTrue(0 <= c["thumb_score"]["score"] <= 100)
            self.assertIn("reviewer 9/10", c["thumb_score"]["why"])


if __name__ == "__main__":
    unittest.main()
