"""Two speed fixes for the shots-stage judge, confirmed against real timing
behavior reported on a live production (709-cue narration, 100+ shots):

1. review_shotlist's chunked judge calls used to run one after another even
   though each chunk is an independent LLM call - on a large plan that is
   several sequential round trips stacked end to end. They now run
   concurrently via _judge_shot_chunks.

2. A patch-mode attempt (_run_shots) only ever rewrites the handful of
   prompts the last review flagged as weak, but used to re-judge the ENTIRE
   shotlist again anyway. review_shotlist_patch now judges only the patched
   assets and carries every other shot's verdict forward from the prior
   review's `verdicts` map.

Run: python -m unittest discover -s tests
"""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import studio  # noqa: E402

CUES = [{"index": i, "text": f"cue {i} text", "start": "00:00:00,000",
        "end": "00:00:01,000"} for i in range(1, 61)]
CUE_TEXT = {c["index"]: c["text"] for c in CUES}


def _shotlist(n_shots):
    shots, images = [], []
    for i in range(1, n_shots + 1):
        asset = f"S{i:02d}_01.png"
        shots.append({"asset": asset, "cues": str(i)})
        images.append({"file": asset, "prompt": f"prompt for shot {i}"})
    return {"shots": shots, "images": images}


class JudgeChunksRunConcurrentlyTests(unittest.TestCase):
    def test_chunk_calls_overlap_in_wall_time(self):
        # 3 chunks of 20 shots (60 total), each call "takes" 0.3s - run
        # sequentially that is >=0.9s; run concurrently it is close to 0.3s.
        data = _shotlist(60)
        shots = [{**s, "prompt": "prompt for shot"} for s in data["shots"]]

        def _slow_generate(cfg, prompt, provider=None, temperature=1.0):
            time.sleep(0.3)
            return '{"shots": []}'

        with mock.patch.object(studio, "llm_generate", side_effect=_slow_generate):
            t0 = time.monotonic()
            result = studio._judge_shot_chunks(
                cfg=None, shots=shots, cue_text=CUE_TEXT, provider="p",
                chunk_size=20, style_guide="", temperature=1.0)
            elapsed = time.monotonic() - t0
        self.assertEqual(result["matched"], 60)  # empty reply -> all "ok"
        self.assertLess(elapsed, 0.7, "chunks did not run concurrently")


class JudgeChunkRetryTests(unittest.TestCase):
    def test_a_chunk_that_fails_once_then_succeeds_is_recovered(self):
        data = _shotlist(20)
        shots = [{**s, "prompt": "prompt for shot"} for s in data["shots"]]
        calls = {"n": 0}

        def _flaky_generate(cfg, prompt, provider=None, temperature=1.0):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("connection reset")
            return '{"shots": []}'

        with mock.patch.object(studio, "llm_generate", side_effect=_flaky_generate):
            result = studio._judge_shot_chunks(
                cfg=None, shots=shots, cue_text=CUE_TEXT, provider="p",
                chunk_size=20, style_guide="", temperature=1.0)

        self.assertEqual(calls["n"], 2, "expected exactly one retry")
        self.assertEqual(result["unreviewed"], 0)
        self.assertEqual(result["matched"], 20)
        self.assertIsNone(result["error"])

    def test_a_chunk_that_fails_twice_is_marked_unreviewed_not_retried_forever(self):
        data = _shotlist(20)
        shots = [{**s, "prompt": "prompt for shot"} for s in data["shots"]]
        calls = {"n": 0}

        def _always_fails(cfg, prompt, provider=None, temperature=1.0):
            calls["n"] += 1
            raise RuntimeError("still down")

        with mock.patch.object(studio, "llm_generate", side_effect=_always_fails):
            result = studio._judge_shot_chunks(
                cfg=None, shots=shots, cue_text=CUE_TEXT, provider="p",
                chunk_size=20, style_guide="", temperature=1.0)

        self.assertEqual(calls["n"], 2, "expected exactly two tries, not more")
        self.assertEqual(result["unreviewed"], 20)
        self.assertEqual(result["matched"], 0)
        self.assertIn("still down", result["error"])

    def test_two_failing_chunks_out_of_several_dont_sink_the_passing_ones(self):
        # 5 chunks of 20 (100 shots): chunks 0 and 2 fail every time, the
        # rest succeed - the 3 good chunks' shots must still count.
        data = _shotlist(100)
        shots = [{**s, "prompt": "prompt for shot"} for s in data["shots"]]

        def _selectively_flaky(cfg, prompt, provider=None, temperature=1.0):
            if "S01_01.png" in prompt or "S41_01.png" in prompt:
                raise RuntimeError("down for this chunk")
            return '{"shots": []}'

        with mock.patch.object(studio, "llm_generate", side_effect=_selectively_flaky):
            result = studio._judge_shot_chunks(
                cfg=None, shots=shots, cue_text=CUE_TEXT, provider="p",
                chunk_size=20, style_guide="", temperature=1.0)

        self.assertEqual(result["unreviewed"], 40)  # 2 chunks x 20 shots
        self.assertEqual(result["matched"], 60)      # the other 3 chunks


class ReviewShotlistPatchSkipsUnchangedShotsTests(unittest.TestCase):
    def test_only_patched_assets_are_sent_to_the_judge(self):
        data = _shotlist(10)
        prior_verdicts = {
            f"S{i:02d}_01.png": {"verdict": "ok", "missing": [], "reason": ""}
            for i in range(1, 11)
        }
        # S03 and S07 were flagged weak last time and just got patched
        prior_verdicts["S03_01.png"] = {"asset": "S03_01.png", "verdict": "weak",
                                       "missing": ["prop"], "reason": "r",
                                       "narration": "n"}
        prior_verdicts["S07_01.png"] = {"asset": "S07_01.png", "verdict": "weak",
                                       "missing": ["action"], "reason": "r",
                                       "narration": "n"}
        seen_assets = []

        def _fake_generate(cfg, prompt, provider=None, temperature=1.0):
            # record which assets this judge call actually covers
            for i in range(1, 11):
                asset = f"S{i:02d}_01.png"
                if asset in prompt:
                    seen_assets.append(asset)
            return '{"shots": [{"asset": "S03_01.png", "verdict": "ok"}, ' \
                  '{"asset": "S07_01.png", "verdict": "ok"}]}'

        with mock.patch.object(studio, "llm_generate", side_effect=_fake_generate):
            review = studio.review_shotlist_patch(
                cfg=None, data=data, cues=CUES, provider="p",
                prior_verdicts=prior_verdicts,
                patched_assets={"S03_01.png", "S07_01.png"})

        self.assertEqual(sorted(seen_assets), ["S03_01.png", "S07_01.png"])
        self.assertEqual(review["total"], 10)
        self.assertEqual(review["matched"], 10)
        self.assertEqual(review["ratio"], 1.0)

    def test_a_shot_with_no_prior_verdict_is_judged_fresh_not_assumed_ok(self):
        data = _shotlist(3)
        prior_verdicts = {
            "S01_01.png": {"verdict": "ok", "missing": [], "reason": ""},
            "S02_01.png": {"verdict": "ok", "missing": [], "reason": ""},
            # S03 has no prior verdict at all
        }

        def _fake_generate(cfg, prompt, provider=None, temperature=1.0):
            return '{"shots": [{"asset": "S03_01.png", "verdict": "weak", ' \
                  '"missing": ["setting"], "reason": "vague"}]}'

        with mock.patch.object(studio, "llm_generate", side_effect=_fake_generate):
            review = studio.review_shotlist_patch(
                cfg=None, data=data, cues=CUES, provider="p",
                prior_verdicts=prior_verdicts, patched_assets=set())

        self.assertEqual(review["matched"], 2)
        self.assertEqual([w["asset"] for w in review["weak"]], ["S03_01.png"])


if __name__ == "__main__":
    unittest.main()
