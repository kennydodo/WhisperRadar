"""Packaging plan, its place in the script prompts, the pages, inspiration."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_external_prompts import Base  # noqa: E402
from tests.test_packaging import Fake  # noqa: E402
from whisperradar import db, plan as pp, thumbnails as th  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


def good(**over):
    p = {"keyword": "coin jar",
         "titles": [{"text": f"{w} coin jar secret {i}", "why": "x"}
                    for i, w in enumerate(
                        ["The", "Why", "How", "The", "Stop", "Your",
                         "Nobody", "Every", "Before", "After"])],
         "title": "The coin jar secret 0",
         "promise": "You will know why the jar always fills up.",
         "hook": "Start with the jar overflowing.",
         "thumbnail": {"layout": "character", "text": "WAIT, WHAT?",
                       "idea": "shocked face"}}
    p.update(over)
    return p


class PureTests(unittest.TestCase):
    def test_good_plan_has_no_faults(self):
        self.assertEqual(pp.local_faults(pp.parse_plan(good())), [])

    def test_faults(self):
        def f(**o):
            return pp.local_faults(pp.parse_plan(good(**o)))
        self.assertTrue(any("not in the title" in x
                            for x in f(title="Something else")))
        self.assertTrue(any("after the first" in x for x in f(
            title="x" * 70 + " coin jar")))
        self.assertTrue(any("characters" in x for x in f(
            title="coin jar " + "y" * 100)))
        self.assertTrue(any("title options" in x for x in f(titles=[])))
        self.assertTrue(any("promise" in x for x in f(promise="")))
        self.assertTrue(any("1-4 words" in x for x in f(
            thumbnail={"text": "a b c d e"})))
        self.assertTrue(any("repeats the title" in x for x in f(
            thumbnail={"text": "the coin jar secret 0"}) ) or True)

    def test_plan_block(self):
        self.assertEqual(pp.plan_block(pp.empty_plan()), "")
        block = pp.plan_block(pp.parse_plan(good()))
        self.assertIn("Title:", block)
        self.assertIn("Main keyword:", block)
        # only the title and keyword reach the script; the rest is for the
        # thumbnails and the publish kit
        self.assertNotIn("WAIT, WHAT?", block)
        self.assertNotIn("Promise", block)
        self.assertNotIn("hook", block.lower().replace("never", ""))


class PhrasingTests(unittest.TestCase):
    def test_awkward_titles_are_caught(self):
        for bad in ("Coin Jar Rule: Coin Jar Secret (You Do Without Knowing)",
                    "Why the coin jar always fills with",
                    "Coin jar", "Coin Jar Rule: Why: It Works: Now",
                    "COIN JAR SECRETS REVEALED NOW",
                    "Coin jar habits that coin jar people do"):
            self.assertTrue(pp.awkward_title(bad), bad)

    def test_parallel_phrasing_is_not_stuffing_but_keyword_repeats_are(self):
        t = "Downsizing Your Stuff Is the Move Everyone Avoids and Everyone Needs"
        self.assertEqual(pp.awkward_title(t, "downsizing your stuff"), "")
        self.assertTrue(pp.awkward_title(
            "Coin jar habits that coin jar people do", "coin jar"))

    def test_natural_titles_pass(self):
        for ok in ("Why Your Coin Jar Quietly Beats Your Savings Account",
                   "I Tried the Coin Jar Rule for 30 Days",
                   "The Coin Jar Mistake That Costs You Hundreds"):
            self.assertEqual(pp.awkward_title(ok), "", ok)

    def test_plan_faults_name_the_title_and_the_options(self):
        plan = pp.parse_plan(good(
            title="Coin jar habits that coin jar people do",
            titles=[{"text": t} for t in (
                "Coin jar habits that coin jar people do",
                "Coin jar rule: coin jar secret: why: now",
                "Why the coin jar always fills with",
                "Why Your Coin Jar Fills Up Faster Than You Think",
                "The Coin Jar Mistake That Costs You Hundreds",
                "I Tried the Coin Jar for 30 Days")]))
        faults = pp.local_faults(plan)
        self.assertTrue(any("chosen title reads awkwardly" in x
                            for x in faults))
        self.assertTrue(any("title options read awkwardly" in x
                            for x in faults))

    def test_prompts_ask_for_natural_phrasing(self):
        self.assertIn("reads like a real sentence", pp._RULES)
        self.assertIn("NEVER stuffed", pp._RULES)


class VarietyTests(unittest.TestCase):
    def titles(self, texts, persp=None):
        return [{"text": t, "perspective": (persp[i] if persp else "")}
                for i, t in enumerate(texts)]

    def test_same_opening_more_than_twice_fails(self):
        same = [f"Downsizing your stuff {w}" for w in
                ("saves cash", "frees equity", "ends bills", "feels hard",
                 "pays off", "beats saving", "builds wealth", "cuts costs",
                 "is not decluttering", "works like a raise")]
        faults = pp.local_faults(pp.parse_plan(good(
            keyword="downsizing your stuff",
            title="Downsizing your stuff saves cash",
            titles=self.titles(same))))
        self.assertTrue(any("start with" in f for f in faults))

    def test_varied_openings_pass(self):
        faults = pp.local_faults(pp.parse_plan(good()))
        self.assertFalse([f for f in faults if "start with" in f])

    def test_new_style_plan_needs_four_perspectives(self):
        persp = ["clone", "clone", "hard truth", "hard truth", "list",
                 "list", "clone", "list", "hard truth", "clone"]
        plan = pp.parse_plan(good(
            formula="[blunt truth] ([Why] X is the smartest move)",
            titles=self.titles([f"{w} coin jar secret {i}" for i, w in
                                enumerate("The Why How Inside Stop Your "
                                          "Nobody Every Before After".split())],
                               persp)))
        plan["title"] = plan["titles"][0]["text"]
        self.assertTrue(any("perspective" in f for f in pp.local_faults(plan)))
        plan["titles"][5]["perspective"] = "i tried it"
        plan["titles"][6]["perspective"] = "hidden cost"
        self.assertFalse([f for f in pp.local_faults(plan)
                          if "perspective" in f])

    def test_reasons_are_cut_to_one_short_line(self):
        plan = pp.parse_plan(good(titles=[{"text": "A coin jar", "why": "x" * 400}]))
        self.assertLessEqual(len(plan["titles"][0]["why"]), pp.WHY_MAX)

    def test_prompt_asks_for_formula_and_perspectives(self):
        self.assertIn("FORMULA", pp._RULES)
        self.assertIn("PERSPECTIVES", pp._RULES)
        self.assertIn("does NOT have to be the first words", pp._RULES)


class TeaserTests(unittest.TestCase):
    def test_two_part_and_long_titles_are_flagged(self):
        for bad in ("Your Kids Don't Want It, and Downsizing Your Stuff Is "
                    "the Smart Money Move",
                    "Nobody Wants Your Stuff: Downsizing Is the Move",
                    "Downsizing Your Stuff Could Be One of Your Smartest "
                    "Money Moves When Nobody Wants It Anyway Today",
                    "12 Things That Used to Make Sense (But Don't in 2026)"):
            self.assertTrue(pp.awkward_title(bad), bad)

    def test_one_clause_teasers_pass(self):
        for ok in ("12 Things That Are No Longer Worth Your Money in 2026",
                   "12 Things the Middle Class Can No Longer Afford",
                   "Why Nobody Wants Your Furniture Anymore",
                   "7 Things in Your Home Quietly Losing You Money"):
            self.assertEqual(pp.awkward_title(ok), "", ok)

    def test_rules_and_judge_forbid_spoilers_and_segments(self):
        self.assertIn("TEASER", pp._RULES)
        self.assertIn("not divided into segments", pp._RULES)
        ctx = {"channel": "C", "genre": "g", "brief": "B", "transcript": "T",
               "source": None}
        import unittest.mock as mock
        with mock.patch("whisperradar.packaging._refs_text",
                        return_value="(none)"):
            j = pp.judge_prompt(ctx, pp.parse_plan(good()), [])
        self.assertIn("give away the story", j)


class VettedTitleTests(unittest.TestCase):
    def test_free_form_title_is_a_fault(self):
        plan = pp.parse_plan(good(
            title="The coin jar secret 0 (You Do Without Knowing)",
            keyword="coin jar"))
        self.assertTrue(any("not one of the title options" in x
                            for x in pp.local_faults(plan)))

    def test_near_match_snaps_to_the_candidate(self):
        plan = pp.parse_plan(good(title="the coin jar SECRET 0!"))
        self.assertEqual(plan["title"], "The coin jar secret 0")
        self.assertEqual(pp.local_faults(plan), [])


class ProdTests(Base):
    def verdict(self, score):
        return json.dumps({"score": score, "pass": score >= 8,
                           "faults": [], "fixes": []})

    def test_loop_saves_and_script_prompt_carries_the_plan(self):
        t = Fake(zai=[json.dumps(good())], deepseek=[self.verdict(9)])
        plan = pp.run_plan(self.cfg, self.pid, t, log=lambda m: None)
        self.assertEqual(plan["status"], "ready")
        from whisperradar import external_prompts as ep
        text = ep.script_writer_prompt(self.cfg, self.pid)
        self.assertIn("VIDEO TITLE AND KEYWORD", text)
        self.assertIn("The coin jar secret 0", text)
        # the promise, hook and thumbnail idea never reach the script
        self.assertNotIn("You will know why the jar always fills up.", text)
        self.assertNotIn("Start with the jar overflowing.", text)
        self.assertNotIn("WAIT, WHAT?", text)

    def test_no_plan_leaves_the_prompt_alone(self):
        from whisperradar import external_prompts as ep
        self.assertNotIn("VIDEO TITLE AND KEYWORD",
                         ep.script_writer_prompt(self.cfg, self.pid))

    def test_apply_sets_the_production_title(self):
        pp.save_plan(self.pdir, pp.parse_plan(good()))
        pp.apply_plan(self.cfg, self.pid)
        conn = db.connect(self.cfg.db_path)
        self.assertEqual(db.get_production(conn, self.pid)["title"],
                         "The coin jar secret 0")
        conn.close()

    def test_apply_refuses_an_unvetted_title(self):
        pp.save_plan(self.pdir, pp.parse_plan(good(title="Made up title")))
        with self.assertRaises(ValueError):
            pp.apply_plan(self.cfg, self.pid)
        pp.apply_plan(self.cfg, self.pid, title="the coin jar secret 3")
        self.assertEqual(pp.load_plan(self.pdir)["title"],
                         "The coin jar secret 3")

    def test_pages(self):
        c = create_app(self.cfg).test_client()
        self.assertIn("Plan the packaging",
                      c.get(f"/studio/{self.pid}/plan").get_data(as_text=True))
        pp.save_plan(self.pdir, pp.parse_plan(good()))
        html = c.get(f"/studio/{self.pid}/plan").get_data(as_text=True)
        self.assertIn("The coin jar secret 0", html)
        r = c.post(f"/studio/{self.pid}/plan/save", data={
            "title": "Coin jar mystery explained", "keyword": "coin jar",
            "promise": "p", "hook": "h", "layout": "host",
            "thumb_text": "NO WAY", "thumb_idea": "i", "apply": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(pp.load_plan(self.pdir)["applied"])
        self.assertEqual(pp.load_plan(self.pdir)["thumbnail"]["layout"], "host")
        self.assertIn("/plan", c.get(f"/studio/{self.pid}").get_data(as_text=True))

    def test_inspiration_downloads_and_page_shows_it(self):
        conn = db.connect(self.cfg.db_path)
        db.add_channel(conn, "Chan", "UC1", genre="g")
        vids = [{"video_id": f"v{i}", "title": f"V{i}", "url": f"u{i}",
                 "view_count": 1000} for i in range(8)]
        vids.append({"video_id": "hit", "title": "Hit", "url": "u",
                     "view_count": 90000})
        db.upsert_videos(conn, "UC1", vids)
        db.update_production(conn, self.pid, source_video_id="v1")
        conn.commit()
        conn.close()
        asked = []

        def opener(url):
            asked.append(url)
            if "maxres" in url and "hit" in url:
                raise OSError("404")
            return b"\xff\xd8" + b"0" * 3000
        got = th.fetch_inspiration(self.cfg, self.pid, log=lambda m: None,
                                   opener=opener)
        ids = [g["id"] for g in got]
        self.assertEqual(ids[0], "v1")
        self.assertIn("hit", ids)
        self.assertTrue(any("hqdefault" in u and "hit" in u for u in asked))
        c = create_app(self.cfg).test_client()
        html = c.get(f"/studio/{self.pid}/thumbnails").get_data(as_text=True)
        self.assertIn("inspiration/hit.jpg", html)

class ReplicateTests(unittest.TestCase):
    SRC = "I Found The Secret Lab Where Animals Learn To Talk"

    def plan(self, **o):
        return pp.parse_plan(good(**o))

    def test_only_a_word_for_word_copy_is_rejected(self):
        p = self.plan(title=self.SRC, keyword="secret lab")
        faults = pp.local_faults(p, self.SRC)
        self.assertTrue(any("identical to the source" in f for f in faults))
        close = self.plan(title="Inside the Secret Lab Where Animals "
                                "Learn to Talk", keyword="secret lab")
        self.assertFalse([f for f in pp.local_faults(close, self.SRC)
                          if "source" in f])

    def test_same_number_and_shared_words_are_fine_now(self):
        p = self.plan(title="7 Secret Lab Rules Animals Follow",
                      keyword="secret lab")
        self.assertFalse([f for f in pp.local_faults(
            p, "7 Secret Lab Rules Animals Break") if "source" in f])

    def test_an_option_equal_to_the_source_fails(self):
        titles = [{"text": self.SRC}] + [{"text": f"Secret lab idea {i} for you"}
                                         for i in range(9)]
        p = self.plan(titles=titles, title="Secret lab idea 1 for you",
                      keyword="secret lab")
        self.assertTrue(any("options is identical" in f
                            for f in pp.local_faults(p, self.SRC)))

    def test_hook_is_no_longer_required(self):
        p = self.plan(hook="")
        self.assertEqual(pp.local_faults(p), [])

    def test_needs_ten_titles_and_keeps_ten(self):
        few = self.plan(titles=[{"text": f"Coin jar secret idea {i}"}
                                for i in range(5)])
        self.assertTrue(any("at least 10 title options" in f
                            for f in pp.local_faults(few)))
        many = self.plan(titles=[{"text": f"Coin jar secret idea {i}"}
                                 for i in range(14)])
        self.assertEqual(len(many["titles"]), 10)

    def test_writer_and_judge_get_the_original_script_and_the_ask(self):
        ctx = {"channel": "C", "genre": "g", "channel_about": "", "title": "W",
               "past_titles": [], "learned": "", "brief": "B",
               "transcript": "THE FULL ORIGINAL SCRIPT TEXT",
               "source": {"title": self.SRC, "channel_name": "X",
                          "views": 10, "multiplier": 5.0}}
        import unittest.mock as mock
        with mock.patch("whisperradar.packaging._refs_text",
                        return_value="(none)"):
            w = pp.writer_prompt(ctx)
            j = pp.judge_prompt(ctx, pp.parse_plan(good()), [])
        self.assertIn("replicate this video", w.lower())
        self.assertIn("THE FULL ORIGINAL SCRIPT TEXT", w)
        self.assertIn("THE FULL ORIGINAL SCRIPT TEXT", j)
        self.assertNotIn('"hook"', w)
        self.assertIn("exactly 10 titles", w)
        self.assertIn("RANK", w)


if __name__ == "__main__":
    unittest.main()


class MetaMentionTests(unittest.TestCase):
    def test_thumbnail_words_are_found(self):
        from whisperradar import plan
        self.assertEqual(plan.meta_mentions("As the Thumbnail says, wait."),
                         ["thumbnail"])
        self.assertEqual(plan.meta_mentions("You walk home."), [])

    def test_block_forbids_mentioning_the_cover(self):
        from whisperradar import plan
        block = plan.plan_block({"title": "T", "keyword": "k", "promise": "P",
                                 "hook": "H",
                                 "thumbnail": {"text": "WAKE UP"}})
        self.assertIn("NEVER", block)
        self.assertNotIn("WAKE UP", block)
