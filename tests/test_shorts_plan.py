import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import shorts, shorts_plan  # noqa: E402


def srt(cues):
    def f(x):
        return f"{int(x) // 3600:02d}:{int(x) // 60 % 60:02d}:{int(x) % 60:02d},000"
    return "\n".join(f"{i}\n{f(a)} --> {f(b)}\n{t}\n"
                     for i, (a, b, t) in enumerate(cues, 1))


def long_video(minutes=10):
    """One 10 s cue after another; every 6th cue is a lively hook."""
    cues = []
    for i in range(minutes * 6):
        a = i * 10
        if i % 6 == 3:
            text = f"Why do {i} habits ruin every home? Never keep this one."
        else:
            text = f"Background sentence number {i} about the room."
        cues.append((a, a + 10, text))
    return srt(cues)


class CountTests(unittest.TestCase):
    def test_scales_with_length_and_is_capped(self):
        self.assertEqual(shorts_plan.count_for(120), 1)          # never 0
        self.assertEqual(shorts_plan.count_for(270), 2)
        self.assertEqual(shorts_plan.count_for(600), 3)
        self.assertEqual(shorts_plan.count_for(3600), 6)         # cap
        self.assertEqual(shorts_plan.count_for(3600, max_n=4), 4)
        self.assertEqual(shorts_plan.count_for(600, minutes_per=1), 6)

    def test_bad_settings_fall_back(self):
        self.assertEqual(shorts_plan.count_for(600, minutes_per=0), 3)
        self.assertEqual(shorts_plan.count_for(600, minutes_per="x", max_n="y"), 3)


class ProposeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdir = Path(self.tmp.name)
        self.text = long_video(10)

    def tearDown(self):
        self.tmp.cleanup()

    def test_proposes_several_non_overlapping_sorted(self):
        plan = shorts_plan.propose(self.pdir, self.text, source_title="Rooms")
        items = plan["items"]
        self.assertEqual(len(items), shorts_plan.count_for(600))
        self.assertEqual([i["status"] for i in items], ["proposed"] * len(items))
        starts = [i["start"] for i in items]
        self.assertEqual(starts, sorted(starts))
        for a, b in zip(items, items[1:]):
            self.assertLessEqual(a["end"], b["start"])
        for i in items:
            self.assertGreaterEqual(i["end"] - i["start"], shorts.MIN_S)
            self.assertLessEqual(i["end"] - i["start"], shorts.MAX_S)
        self.assertEqual(items[0]["title"], f"Rooms - Short 1 of {len(items)}")
        self.assertTrue(shorts_plan.plan_path(self.pdir).exists())

    def test_nothing_is_created_by_planning(self):
        shorts_plan.propose(self.pdir, self.text)
        self.assertEqual(sorted(p.name for p in self.pdir.iterdir()),
                         [shorts_plan.PLAN_FILE])

    def test_empty_subtitles_raise(self):
        with self.assertRaises(ValueError):
            shorts_plan.propose(self.pdir, "")

    def test_created_window_is_kept_and_never_reproposed(self):
        plan = shorts_plan.propose(self.pdir, self.text)
        first = plan["items"][0]
        shorts_plan.mark_created(self.pdir, first["id"], 77)
        again = shorts_plan.propose(self.pdir, self.text)
        kept = [i for i in again["items"] if i["id"] == first["id"]][0]
        self.assertEqual((kept["status"], kept["pid"]), ("created", 77))
        # created + proposed still come to the wanted count
        self.assertEqual(len(again["items"]), shorts_plan.count_for(600))
        for other in again["items"]:
            if other["id"] != first["id"]:
                self.assertTrue(other["end"] <= first["start"] or other["start"] >= first["end"])

    def test_skipped_window_is_not_proposed_again(self):
        plan = shorts_plan.propose(self.pdir, self.text)
        gone = plan["items"][0]
        shorts_plan.mark_skipped(self.pdir, gone["id"])
        again = shorts_plan.propose(self.pdir, self.text)
        live = [i for i in again["items"] if i["status"] == "proposed"]
        self.assertTrue(all(i["end"] <= gone["start"] or i["start"] >= gone["end"] for i in live))
        self.assertEqual(len(live), shorts_plan.count_for(600))   # skipped does not count

    def test_changed_subtitles_replace_proposals_but_remember_created(self):
        plan = shorts_plan.propose(self.pdir, self.text, source_title="Rooms")
        first = plan["items"][0]
        shorts_plan.mark_created(self.pdir, first["id"], 9)
        newer = self.text.replace("Background", "Different")
        self.assertTrue(shorts_plan.is_stale(shorts_plan.load(self.pdir), newer))
        again = shorts_plan.propose(self.pdir, newer, source_title="Rooms")
        self.assertFalse(shorts_plan.is_stale(again, newer))
        self.assertTrue(all(i["status"] == "proposed" for i in again["items"]))
        self.assertEqual([p["pid"] for p in again["previous_created"]], [9])

    def test_not_stale_when_unchanged(self):
        plan = shorts_plan.propose(self.pdir, self.text)
        self.assertFalse(shorts_plan.is_stale(plan, self.text))
        self.assertFalse(shorts_plan.is_stale(plan, self.text.replace("\n", "\r\n")))


class EditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdir = Path(self.tmp.name)
        self.plan = shorts_plan.propose(self.pdir, long_video(10), source_title="Rooms")
        self.id = self.plan["items"][0]["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_edit_times_and_title(self):
        it = shorts_plan.update_item(self.pdir, self.id, start="12", end=55, title=" My cut ")
        self.assertEqual((it["start"], it["end"], it["title"]), (12.0, 55.0, "My cut"))
        self.assertEqual(shorts_plan.load(self.pdir)["items"][0]["start"], 12.0)

    def test_bad_times_rejected(self):
        with self.assertRaises(ValueError):
            shorts_plan.update_item(self.pdir, self.id, start=50, end=40)

    def test_created_cannot_be_edited(self):
        shorts_plan.mark_created(self.pdir, self.id, 5)
        with self.assertRaises(ValueError):
            shorts_plan.update_item(self.pdir, self.id, end=999)

    def test_skip_and_unskip_and_waiting(self):
        n = len(shorts_plan.waiting(shorts_plan.load(self.pdir)))
        shorts_plan.mark_skipped(self.pdir, self.id)
        self.assertEqual(len(shorts_plan.waiting(shorts_plan.load(self.pdir))), n - 1)
        shorts_plan.unskip(self.pdir, self.id)
        self.assertEqual(len(shorts_plan.waiting(shorts_plan.load(self.pdir))), n)

    def test_unknown_item(self):
        with self.assertRaises(KeyError):
            shorts_plan.mark_skipped(self.pdir, "s999")

    def test_titles_renumber_when_one_is_skipped(self):
        shorts_plan.mark_skipped(self.pdir, self.id)
        items = [i for i in shorts_plan.load(self.pdir)["items"] if i["status"] == "proposed"]
        self.assertTrue(items[0]["title"].endswith(f"Short 1 of {len(items)}"))


class LoadTests(unittest.TestCase):
    def test_missing_or_bad_file_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(shorts_plan.load(d))
            Path(d, shorts_plan.PLAN_FILE).write_text("{not json", encoding="utf-8")
            self.assertIsNone(shorts_plan.load(d))
            Path(d, shorts_plan.PLAN_FILE).write_text(json.dumps({"items": 3}), encoding="utf-8")
            self.assertIsNone(shorts_plan.load(d))


class ParseClockTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(shorts_plan.parse_clock("83"), 83.0)
        self.assertEqual(shorts_plan.parse_clock("1:23"), 83.0)
        self.assertEqual(shorts_plan.parse_clock("1:02:03"), 3723.0)
        self.assertEqual(shorts_plan.parse_clock("83,5"), 83.5)

    def test_bad_or_empty(self):
        with self.assertRaises(ValueError):
            shorts_plan.parse_clock("abc")
        with self.assertRaises(ValueError):
            shorts_plan.parse_clock("")
        self.assertEqual(shorts_plan.parse_clock("", default=5), 5.0)


if __name__ == "__main__":
    unittest.main()
