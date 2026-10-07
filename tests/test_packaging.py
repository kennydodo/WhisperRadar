"""Publish kit: chapters, rule checks, description assembly, the loop."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_external_prompts import Base  # noqa: E402
from whisperradar import packaging as pk  # noqa: E402


def cue(i, start, end, text="words here"):
    return {"index": i, "start": start, "end": end, "text": text}


def hhmmss(sec):
    return "%02d:%02d:%02d,000" % (sec // 3600, sec % 3600 // 60, sec % 60)


def cues_every(n, step=10):
    return [cue(i + 1, hhmmss(i * step), hhmmss(i * step + step - 1), f"c{i+1}")
            for i in range(n)]


def shot(a, b, scene):
    return {"cues": f"{a}-{b}", "asset": f"{scene}_01_X_ST.png",
            "scene": scene, "motion": "ST"}


class ChaptersTests(unittest.TestCase):
    def test_one_chapter_per_scene_starting_at_zero(self):
        cues = cues_every(30)                      # 300 s
        shots = [shot(1, 10, "S01"), shot(11, 20, "S02"), shot(21, 30, "S03")]
        ch = pk.build_chapters(cues, shots)
        self.assertEqual([c["scene"] for c in ch], ["S01", "S02", "S03"])
        self.assertEqual([c["start"] for c in ch], [0.0, 100.0, 200.0])
        self.assertEqual(ch[-1]["end"], 299.0)
        self.assertIn("c11", ch[1]["text"])

    def test_scene_split_over_several_shots_is_one_chapter(self):
        cues = cues_every(30)
        shots = [shot(1, 5, "S01"), shot(6, 10, "S01"), shot(11, 30, "S02")]
        self.assertEqual(len(pk.build_chapters(cues, shots)), 2)

    def test_short_scenes_merge_into_the_previous_one(self):
        cues = cues_every(30)
        shots = [shot(1, 10, "S01"), shot(11, 12, "S02"), shot(13, 30, "S03")]
        ch = pk.build_chapters(cues, shots, min_len=25)
        self.assertEqual([c["scene"] for c in ch], ["S01", "S03"])

    def test_a_tiny_last_scene_folds_into_the_one_before(self):
        cues = cues_every(30)
        shots = [shot(1, 10, "S01"), shot(11, 29, "S02"), shot(30, 30, "S03")]
        ch = pk.build_chapters(cues, shots, min_len=25)
        self.assertEqual([c["scene"] for c in ch], ["S01", "S02"])

    def test_without_a_shotlist_the_narration_is_cut_evenly(self):
        ch = pk.build_chapters(cues_every(60), [])
        self.assertGreaterEqual(len(ch), 3)
        self.assertEqual(ch[0]["start"], 0.0)

    def test_timestamps(self):
        self.assertEqual(pk.fmt_ts(0), "0:00")
        self.assertEqual(pk.fmt_ts(125), "2:05")
        self.assertEqual(pk.fmt_ts(3725), "1:02:05")


def good_kit(n_chapters=3):
    return {"keyword": "cheetah speed",
            "titles": [{"text": "x", "why": ""}],
            "title": "How Fast Is a Cheetah Really? The Cheetah Speed Truth",
            "description": ("The cheetah speed secret most people get wrong. "
                            + "Learn how the fastest land animal really runs. " * 6),
            "chapter_titles": [f"Part {i}" for i in range(n_chapters)],
            "hashtags": ["#cheetah", "#wildlife", "#animals"],
            "tags": ["cheetah", "cheetah speed", "big cats", "wildlife",
                     "fastest animal", "animal facts"],
            "pinned_comment": "Which animal should we cover next?"}


def chapters3():
    return [{"start": 0.0}, {"start": 60.0}, {"start": 120.0}]


class ChecksTests(unittest.TestCase):
    def test_a_good_kit_has_no_faults(self):
        self.assertEqual(pk.local_faults(good_kit(), chapters3()), [])
        self.assertEqual(pk.kit_score(pk.checks(good_kit(), chapters3())), 100)

    def test_each_hard_rule(self):
        k = good_kit(); k["title"] = "x" * 101
        self.assertTrue(any("100 characters" in f
                            for f in pk.local_faults(k, chapters3())))
        k = good_kit(); k["hashtags"] = ["#a"] * 2
        self.assertTrue(any("hashtags" in f
                            for f in pk.local_faults(k, chapters3())))
        k = good_kit(); k["hashtags"] = ["#a", "#b", "#c", "#d", "#e", "#f"]
        self.assertTrue(pk.local_faults(k, chapters3()))
        k = good_kit(); k["tags"] = ["t" * 100] * 6
        self.assertTrue(any("tags fit" in f
                            for f in pk.local_faults(k, chapters3())))
        k = good_kit(); k["chapter_titles"] = ["a", "", "c"]
        self.assertTrue(any("chapter has a title" in f
                            for f in pk.local_faults(k, chapters3())))
        k = good_kit()
        close = [{"start": 0.0}, {"start": 5.0}, {"start": 120.0}]
        self.assertTrue(any("apart" in f for f in pk.local_faults(k, close)))
        k = good_kit(); k["title"] = "Something else entirely"
        self.assertTrue(any("keyword" in f
                            for f in pk.local_faults(k, chapters3())))

    def test_tips_cost_less_than_faults(self):
        k = good_kit(); k["pinned_comment"] = ""
        items = pk.checks(k, chapters3())
        self.assertEqual(pk.local_faults(k, chapters3()), [])
        self.assertEqual(pk.kit_score(items), 95)

    def test_long_title_is_a_tip_only(self):
        k = good_kit(); k["title"] = "The cheetah speed truth: " + "x" * 50
        self.assertEqual(pk.local_faults(k, chapters3()), [])
        self.assertLess(pk.kit_score(pk.checks(k, chapters3())), 100)


class AssembleTests(unittest.TestCase):
    def test_body_chapters_and_hashtags_in_order(self):
        text = pk.assemble_description(good_kit(), chapters3())
        self.assertTrue(text.startswith("The cheetah speed secret"))
        self.assertIn("Chapters:\n0:00 Part 0\n1:00 Part 1\n2:00 Part 2", text)
        self.assertTrue(text.endswith("#cheetah #wildlife #animals"))

    def test_parse_kit_cleans_hashtags_and_tags(self):
        kit = pk.parse_kit({"titles": ["A", {"text": "B", "why": "w"}],
                            "hashtags": ["cat", "#Cat", "big cats", "##x"],
                            "tags": "a, b,\n c", "keyword": " k "})
        self.assertEqual(kit["title"], "A")
        self.assertEqual(kit["hashtags"], ["#cat", "#bigcats", "#x"])
        self.assertEqual(kit["tags"], ["a", "b", "c"])
        self.assertEqual(kit["keyword"], "k")


class Fake:
    def __init__(self, **replies):
        self.replies = {k: list(v) for k, v in replies.items()}
        self.calls = []

    def ask(self, site, prompt, files=(), new_chat=True, ready=None):
        self.calls.append({"site": site, "prompt": prompt,
                           "new_chat": new_chat})
        return self.replies[site].pop(0)

    def urls(self):
        return {}


class LoopTests(Base):
    def setUp(self):
        super().setUp()
        (self.pdir / "script.md").write_text("A script about coins. " * 30,
                                             "utf-8")
        srt = []
        for i in range(30):
            srt.append(f"{i+1}\n{hhmmss(i*10)} --> {hhmmss(i*10+9)}\nline {i+1}\n")
        (self.pdir / "subtitles.srt").write_text("\n".join(srt), "utf-8")
        shots = [shot(1, 10, "S01"), shot(11, 20, "S02"), shot(21, 30, "S03")]
        (self.pdir / "shotlist.json").write_text(
            json.dumps({"shots": shots, "images": []}), "utf-8")

    def kit_json(self, **over):
        k = good_kit(3)
        k["keyword"] = "coin jar"
        k["title"] = "The Coin Jar Secret Nobody Tells You"
        k["titles"] = [{"text": k["title"], "why": "secret"}]
        k["description"] = "The coin jar secret in plain words. " * 8
        k.update(over)
        return json.dumps(k)

    def verdict(self, score, **kw):
        return json.dumps({"score": score, "pass": score >= 8,
                           "faults": kw.get("faults", []),
                           "fixes": kw.get("fixes", [])})

    def test_passes_and_saves_the_kit(self):
        t = Fake(zai=[self.kit_json()], deepseek=[self.verdict(9)])
        kit = pk.run_kit(self.cfg, self.pid, t, log=lambda m: None)
        self.assertEqual(kit["status"], "ready")
        self.assertEqual(kit["score"], 9.0)
        saved = pk.load_kit(self.pdir)
        self.assertEqual(saved["title"], "The Coin Jar Secret Nobody Tells You")
        self.assertEqual([c["new_chat"] for c in t.calls], [True, True])
        self.assertIn("coin", t.calls[0]["prompt"].lower())
        self.assertIn("3 SECTIONS", t.calls[0]["prompt"])

    def test_verbatim_verdict_goes_back_in_the_same_chat(self):
        t = Fake(zai=[self.kit_json(), self.kit_json(title="The Coin Jar Trick")],
                 deepseek=[self.verdict(5, faults=["weak hook"]),
                           self.verdict(9)])
        kit = pk.run_kit(self.cfg, self.pid, t, log=lambda m: None)
        self.assertEqual(kit["title"], "The Coin Jar Trick")
        sites = [(c["site"], c["new_chat"]) for c in t.calls]
        self.assertEqual(sites, [("zai", True), ("deepseek", True),
                                 ("zai", False), ("deepseek", False)])
        self.assertIn("weak hook", t.calls[2]["prompt"])
        self.assertIn("Change ONLY", t.calls[2]["prompt"])

    def test_rule_faults_block_a_passing_score(self):
        bad = self.kit_json(hashtags=["#one"])
        good = self.kit_json()
        t = Fake(zai=[bad, good], deepseek=[self.verdict(9), self.verdict(9)])
        kit = pk.run_kit(self.cfg, self.pid, t, log=lambda m: None)
        self.assertEqual(len(kit["hashtags"]), 3)
        self.assertIn("hashtags", t.calls[2]["prompt"])

    def test_stop_saves_the_best_draft(self):
        t = Fake(zai=[self.kit_json()], deepseek=[self.verdict(5)])
        kit = pk.run_kit(self.cfg, self.pid, t, log=lambda m: None,
                         should_stop=lambda: True)
        self.assertEqual(kit["status"], "draft")
        self.assertEqual(pk.load_kit(self.pdir)["status"], "draft")

    def test_an_unusable_reply_is_asked_again(self):
        t = Fake(zai=["sorry, here you go", self.kit_json()],
                 deepseek=[self.verdict(9)])
        kit = pk.run_kit(self.cfg, self.pid, t, log=lambda m: None)
        self.assertEqual(kit["status"], "ready")
        self.assertIn("no usable JSON", t.calls[1]["prompt"])


if __name__ == "__main__":
    unittest.main()
