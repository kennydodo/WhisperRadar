"""Animated opening in the shotlist: writer note, free checks, judge rules."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import motion, studio  # noqa: E402


def _cues(n=20, dur=4.0):
    def ts(x):
        h, rem = divmod(int(x * 1000), 3600000)
        m, rem = divmod(rem, 60000)
        sec, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"
    return [{"index": i + 1, "start": ts(i * dur), "end": ts((i + 1) * dur),
             "text": f"cue {i + 1}"} for i in range(n)]


def _data(cues, motion_for=None, hold_cues=1):
    shots, images = [], []
    i = 1
    n = len(cues)
    c = 1
    while c <= n:
        last = min(n, c + hold_cues - 1)
        asset = f"S{i:02d}_SCN_ZI.png"
        shot = {"cues": f"{c}-{last}", "asset": asset, "motion": "ZI", "scene": 1}
        if motion_for is None or motion_for(c):
            shot["ai_motion"] = "slow push in; leaves drift"
        shots.append(shot)
        images.append({"file": asset, "prompt": "a prompt"})
        i += 1
        c = last + 1
    return {"shots": shots, "images": images}


class WriterNoteTests(unittest.TestCase):
    def test_note_names_the_window_and_ai_motion(self):
        cues = _cues()
        last = motion.window_last_cue(cues, 30, studio._srt_seconds)
        self.assertEqual(last, 8)           # cue 8 starts at 28s
        note = motion.writer_note(30, last)
        self.assertIn("cues 1-8", note)
        self.assertIn("ai_motion", note)
        self.assertIn("NO text", note)

    def test_prompt_gets_the_note_only_when_given(self):
        base = studio.shotlist_prompt("BRIEF", "1: hi (4s)")
        withn = studio.shotlist_prompt("BRIEF", "1: hi (4s)",
                                       animation_note=motion.writer_note(60, 15))
        self.assertNotIn("ANIMATED OPENING", base)
        self.assertIn("ANIMATED OPENING", withn)


class WindowCheckTests(unittest.TestCase):
    def test_missing_ai_motion_is_a_fault_only_inside_the_window(self):
        cues = _cues()
        data = _data(cues, motion_for=lambda c: c > 8 or c in (1, 2))
        faults, warnings, assets = motion.window_check(
            data, cues, 30, studio._srt_seconds)
        self.assertEqual(len(assets), 8)
        self.assertEqual(len(faults), 1)
        self.assertIn("6 shot(s)", faults[0])
        # shots after the window need nothing
        ok = _data(cues, motion_for=lambda c: c <= 8)
        self.assertEqual(motion.window_check(ok, cues, 30, studio._srt_seconds)[0], [])

    def test_long_hold_is_only_a_warning(self):
        cues = _cues()
        data = _data(cues, hold_cues=3)         # 12 s per shot
        faults, warnings, _ = motion.window_check(data, cues, 30, studio._srt_seconds)
        self.assertEqual(faults, [])
        self.assertTrue(any("12.0s" in w for w in warnings))


class JudgeTests(unittest.TestCase):
    def test_alignment_prompt_adds_animation_rules_for_marked_shots(self):
        shot = {"asset": "S01", "cues": "1-1", "prompt": "p", "animate": True,
                "ai_motion": "slow push in"}
        plain = {"asset": "S02", "cues": "2-2", "prompt": "p"}
        text = studio.alignment_prompt([shot, plain], {1: "a", 2: "b"})
        self.assertIn("ANIMATION CHECK", text)
        self.assertIn('planned motion: "slow push in"', text)
        self.assertNotIn("ANIMATION CHECK", studio.alignment_prompt([plain], {2: "b"}))

    def test_review_flags_missing_motion_without_calling_a_real_judge(self):
        from unittest import mock
        cues = _cues(8)
        data = _data(cues, motion_for=lambda c: False)
        reply = '{"shots": []}'
        with mock.patch.object(studio, "llm_generate", return_value=reply):
            rv = studio.review_shotlist(None, data, cues, "x", animate_seconds=30)
            off = studio.review_shotlist(None, data, cues, "x")
        self.assertTrue(any("ai_motion" in f for f in rv["faults"]))
        self.assertFalse(any("ai_motion" in f for f in off["faults"]))


if __name__ == "__main__":
    unittest.main()
