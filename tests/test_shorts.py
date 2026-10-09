import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402
from tests.test_external_prompts import Base  # noqa: E402
from whisperradar import shorts  # noqa: E402


def srt(cues):
    out = []
    for i, (a, b, t) in enumerate(cues, 1):
        def f(x):
            return f"00:{int(x) // 60:02d}:{int(x) % 60:02d},000"
        out.append(f"{i}\n{f(a)} --> {f(b)}\n{t}\n")
    return "\n".join(out)


# 0-60s intro, then a lively 70-100s stretch starting with a question
CUES = [(0, 10, "Welcome back to the channel."),
        (10, 30, "Today we talk about rooms."),
        (30, 50, "Some background first."),
        (70, 80, "Why do 3 habits ruin every home?"),
        (80, 92, "Never keep this one thing, it is the biggest mistake."),
        (92, 100, "Throw it out today.")]


class ShortsTests(unittest.TestCase):
    def test_parse_and_shift(self):
        cues = shorts.parse_srt(srt(CUES))
        self.assertEqual(len(cues), 6)
        self.assertEqual(cues[3]["start"], 70.0)
        shifted = shorts.parse_srt(shorts.srt_for_clip(cues, 70, 100))
        self.assertEqual(shifted[0]["start"], 0.0)
        self.assertEqual(len(shifted), 3)

    def test_plan_prefers_the_hook_and_never_overlaps(self):
        cues = shorts.parse_srt(srt(CUES))
        plan = shorts.plan_clips(cues, n=3)
        self.assertTrue(plan)
        self.assertEqual(plan[0]["start"] if len(plan) == 1 else
                         max(plan, key=lambda c: c["score"])["start"], 70.0)
        for a, b in zip(plan, plan[1:]):
            self.assertLessEqual(a["end"], b["start"])
        for c in plan:
            self.assertTrue(20 <= c["end"] - c["start"] <= 58)

    def test_ffmpeg_command_is_vertical_with_captions(self):
        cmd = shorts.ffmpeg_cmd("in.mp4", 70, 100, "out.mp4", "short1.srt")
        joined = " ".join(cmd)
        self.assertIn("1080:1920", joined)
        self.assertIn("subtitles=short1.srt", joined)
        self.assertEqual(cmd[cmd.index("-t") + 1], "30.00")
        self.assertEqual(cmd[-1], "out.mp4")

    def test_make_clips_runs_ffmpeg_per_clip_and_needs_inputs(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            pdir = Path(tmp.name)
            with self.assertRaises(ValueError):
                shorts.make_clips(pdir)
            (pdir / "subtitles.srt").write_text(srt(CUES), encoding="utf-8")
            (pdir / "final.mp4").write_bytes(b"x")
            calls = []

            def fake(cmd, **kw):
                calls.append((cmd, kw["cwd"]))
                Path(cmd[-1]).write_bytes(b"clip")
            got = shorts.make_clips(pdir, n=2, run=fake)
            self.assertEqual(len(got), len(calls))
            # ffmpeg runs inside shorts/, so video and output must be absolute
            self.assertTrue(Path(calls[0][0][calls[0][0].index("-i") + 1]
                                 ).is_absolute())
            self.assertTrue(Path(calls[0][0][-1]).is_absolute())
            self.assertTrue(got and (pdir / got[0]["file"]).is_file())
            self.assertTrue((pdir / "shorts" / "short1.srt").is_file())
        finally:
            wr_tmp.cleanup(tmp)


class PageTests(Base):
    def test_page_plans_and_make_reports_missing_inputs(self):
        from whisperradar.webapp import create_app
        c = create_app(self.cfg).test_client()
        html = c.get(f"/studio/{self.pid}/shorts").get_data(as_text=True)
        self.assertIn("Needs the finished video", html)
        (self.pdir / "subtitles.srt").write_text(srt(CUES), encoding="utf-8")
        html = c.get(f"/studio/{self.pid}/shorts").get_data(as_text=True)
        self.assertIn("Why do 3 habits ruin every home?", html)
        r = c.post(f"/studio/{self.pid}/shorts/make", data={"n": "2"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("error=", r.headers["Location"])     # no final.mp4 yet
        self.assertIn("/shorts", c.get(f"/studio/{self.pid}").get_data(
            as_text=True))


if __name__ == "__main__":
    unittest.main()
