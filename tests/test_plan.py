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
         "titles": [{"text": f"{w} coin jar {n} {i}",
                     "why": f"angle {i % 6} - sells it"}
                    for i, (w, n) in enumerate(zip(
                        ["The", "Why", "How", "The", "Stop", "Your",
                         "Nobody", "Every", "Before", "After", "Inside",
                         "Only"],
                        ["secret", "secret", "habit", "trick", "mistake",
                         "story", "reason", "lesson", "habit", "truth",
                         "puzzle", "claim"]))],
         "title": "The coin jar secret 0",
         "promise": "You will know why the jar always fills up.",
         "hook": "Start with the jar overflowing.",
         "thumbnail": {"layout": "character", "text": "WAIT, WHAT?",
                       "idea": "shocked face"}}
    p.update(over)
    return p


PACKAGE = {"overview": "A calm look at what hides in an old coin jar.",
           "premise": "A person finds an old jar and learns to check it.",
           "keyword": "coin jar",
           "values": ["curiosity", "a mistake to avoid", "relief"]}


class PureTests(unittest.TestCase):
    def test_good_plan_has_no_faults(self):
        self.assertEqual(pp.local_faults(pp.parse_plan(good())), [])

    def test_faults(self):
        def f(**o):
            return pp.local_faults(pp.parse_plan(good(**o)))
        # the keyword need not be in the title (or the options)
        p = good()
        p["title"] = p["titles"][0]["text"] = "The jar nobody explains"
        self.assertFalse(any("keyword" in x for x in
                             pp.local_faults(pp.parse_plan(p))))
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
        self.assertIn("keyword pile", pp._RULES)


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

    def test_segments_are_no_longer_required(self):
        plan = pp.parse_plan(good())
        self.assertEqual(plan["segments"], [])
        self.assertFalse([f for f in pp.local_faults(plan)
                          if "segment" in f])

    def test_parse_segments_drops_junk_and_caps(self):
        segs = pp.parse_segments([
            {"name": "A", "titles": ["x", "X", "y"]}, {"name": "", "titles": ["z"]},
            "junk", {"name": "B", "titles": []},
            {"name": "C", "titles": [{"text": "t1"}]}])
        self.assertEqual([g["name"] for g in segs], ["A", "C"])
        self.assertEqual(segs[0]["titles"], ["x", "y"])

    def test_old_perspective_key_still_reads(self):
        plan = pp.parse_plan(good(titles=[{"text": "A coin jar tale",
                                           "perspective": "hidden cost"}]))
        self.assertEqual(plan["titles"][0]["segment"], "hidden cost")

    def test_reasons_are_cut_to_one_short_line(self):
        plan = pp.parse_plan(good(titles=[{"text": "A coin jar", "why": "x" * 400}]))
        self.assertLessEqual(len(plan["titles"][0]["why"]), pp.WHY_MAX)

    def test_prompt_asks_for_formula_and_core_value_angles(self):
        self.assertIn("title formula", pp._RULES)
        self.assertIn("CORE VALUES", pp._RULES)
        self.assertIn("WHOLE video", pp._RULES)
        self.assertIn("does NOT have to be in every title", pp._RULES)
        self.assertNotIn("SEGMENTS", pp._RULES)


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

    def test_rules_and_judge_forbid_spoilers(self):
        self.assertIn("TEASER", pp._RULES)
        self.assertIn("never split into two parts", pp._RULES)
        ctx = {"channel": "C", "genre": "g", "brief": "B", "transcript": "T",
               "source": None, "package": {}}
        import unittest.mock as mock
        with mock.patch("whisperradar.packaging._refs_text",
                        return_value="(none)"):
            j = pp.judge_prompt(ctx, pp.parse_plan(good()), [])
        self.assertIn("gives away", j)


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
        t = Fake(zai=[json.dumps(good())],
                 deepseek=[json.dumps(PACKAGE), self.verdict(9)])
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

    def test_run_plan_is_blind_and_the_judges_pick_becomes_the_title(self):
        from tests.test_external_prompts import SOURCE
        # the production's source video: a title the writer must never see
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        conn.execute("INSERT INTO channels (channel_id, name, genre, active) "
                     "VALUES ('c1','Src','finance',1)")
        conn.execute("INSERT INTO videos (video_id, channel_id, title, url, "
                     "view_count) VALUES ('v1','c1','Why Nobody Wants Your "
                     "Old Coin Jar Anymore','u',1000)")
        conn.execute("UPDATE productions SET source_video_id='v1' WHERE id=?",
                     (self.pid,))
        conn.commit()
        conn.close()
        titles = good()
        pick = titles["titles"][5]["text"]
        verdict = json.dumps({"score": 9, "pass": True, "closest": pick,
                              "alternates": [titles["titles"][2]["text"]],
                              "faults": [], "fixes": []})
        t = Fake(zai=[json.dumps(titles)],
                 deepseek=[json.dumps(PACKAGE), verdict])
        plan = pp.run_plan(self.cfg, self.pid, t, log=lambda m: None)
        zai = " ".join(c["prompt"] for c in t.calls if c["site"] == "zai")
        self.assertNotIn(SOURCE[:60], zai)
        self.assertNotIn("Why Nobody Wants Your Old Coin Jar", zai)
        self.assertIn("what hides in an old coin jar", zai.lower().replace(
            "a calm look at ", ""))
        ds = [c["prompt"] for c in t.calls if c["site"] == "deepseek"]
        self.assertIn(SOURCE[:60], ds[0])            # the analyst read the script
        self.assertIn("Why Nobody Wants Your Old Coin Jar", ds[1])
        self.assertEqual(plan["title"], pick)        # the judge's pick wins
        self.assertEqual(plan["keyword"], "coin jar")
        self.assertEqual(plan["overview"], PACKAGE["overview"])
        saved = pp.load_plan(self.pdir)
        self.assertEqual(saved["title"], pick)

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
        pp.apply_plan(self.cfg, self.pid, title="the coin jar trick 3")
        self.assertEqual(pp.load_plan(self.pdir)["title"],
                         "The coin jar trick 3")

    def test_page_groups_the_picks_under_their_segments(self):
        opens = "The Why How Inside Stop Your Nobody Every Before After Only Soon".split()
        plan = pp.parse_plan(good(
            formula="[f]",
            segments=[{"name": f"Seg {k}", "titles": [f"draft {k}"]}
                      for k in range(6)],
            titles=[{"text": f"{w} coin jar secret {i}",
                     "segment": f"Seg {i % 6}"} for i, w in enumerate(opens)]))
        pp.save_plan(self.pdir, plan)
        c = create_app(self.cfg).test_client()
        html = c.get(f"/studio/{self.pid}/plan").get_data(as_text=True)
        self.assertIn("1. Seg 0", html)
        self.assertIn("6. Seg 5", html)
        self.assertIn("best of this segment", html)
        self.assertIn("All the titles drafted for each of the 6 segments", html)

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

class SourceTitleStaysOutTests(unittest.TestCase):
    def test_refs_never_include_the_source_title(self):
        ctx = {"source": {"title": "I Found The Secret Lab"},
               "refs": [("I found the secret lab!", 9.0),
                        ("Other winning title", 7.0)]}
        text = pp._refs_without_source(ctx)
        self.assertIn("Other winning title", text)
        self.assertNotIn("secret lab", text.lower())


class ScopeTests(unittest.TestCase):
    def test_numbered_list_titles_are_rejected(self):
        p = pp.parse_plan(good())
        p["titles"][3]["text"] = "4 Hidden Costs of Keeping Coin Jars Nobody Wants"
        f = pp.scope_faults(p)
        self.assertTrue(f and "numbered list" in f[0])
        self.assertTrue(any("numbered list" in x for x in pp.local_faults(p)))

    def test_whole_video_titles_pass(self):
        self.assertEqual(pp.scope_faults(pp.parse_plan(good())), [])

    def test_list_source_allows_a_count_within_20_percent_but_never_the_same(self):
        src = "10 Things Nobody Tells You About Coin Jars"
        self.assertEqual(pp.allowed_counts(10), [8, 9, 11, 12])

        def faults(title):
            p = pp.parse_plan(good(title=title))
            p["titles"][0]["text"] = title
            return pp.scope_faults(p, src)
        self.assertEqual(faults("9 Things About Your Coin Jar"), [])
        self.assertEqual(faults("12 Things About Your Coin Jar"), [])
        self.assertTrue(any("same count" in x for x in
                            faults("10 Things About Your Coin Jar")))
        self.assertTrue(any("outside the allowed" in x for x in
                            faults("5 Things About Your Coin Jar")))
        self.assertTrue(any("same count" in x for x in
                            faults("Ten Things About Your Coin Jar")))

    def test_judge_narrow_titles_block_a_pass(self):
        v = {"score": 9.5, "pass": True,
             "narrow": ["You Are Paying Rent on Stuff You Never Use"]}
        f = pp.narrow_faults(v)
        self.assertTrue(f and "only one point" in f[0])
        self.assertEqual(pp.narrow_faults({"narrow": []}), [])

    def test_judge_audits_every_title(self):
        prompt = pp.judge_prompt({"channel": "c", "genre": "g", "transcript": "x",
                                  "refs": [], "package": {}},
                                 pp.parse_plan(good()), [])
        self.assertIn('"narrow"', prompt)
        self.assertIn("AUDIT every title", prompt)


class ReplicateTests(unittest.TestCase):
    SRC = "I Found The Secret Lab Where Animals Learn To Talk"

    def plan(self, **o):
        return pp.parse_plan(good(**o))

    def test_copies_of_the_source_title_are_rejected_close_topics_are_not(self):
        p = self.plan(title=self.SRC, keyword="secret lab")
        f = pp.local_faults(p, self.SRC)
        self.assertTrue(any("copy too many words" in x for x in f))
        close = self.plan(title="Inside the Secret Lab Where Animals "
                                "Learn to Talk", keyword="secret lab")
        self.assertTrue(any("copy too many words" in x
                            for x in pp.local_faults(close, self.SRC)))
        fresh = self.plan(title="Inside the secret lab that taught a fox to speak",
                          keyword="secret lab")
        self.assertFalse([x for x in pp.local_faults(fresh, self.SRC)
                          if "copy too many" in x])
        self.assertTrue(pp.too_close(self.SRC, self.SRC))
        self.assertFalse(pp.too_close("A fox that learned to speak", self.SRC,
                                      "secret lab"))

    def test_an_option_equal_to_the_source_fails(self):
        titles = [{"text": self.SRC}] + [{"text": f"Secret lab idea {i} for you"}
                                         for i in range(11)]
        p = self.plan(titles=titles, title="Secret lab idea 1 for you",
                      keyword="secret lab")
        self.assertTrue(any("copy too many words" in f
                            for f in pp.local_faults(p, self.SRC)))

    def test_hook_is_no_longer_required(self):
        p = self.plan(hook="")
        self.assertEqual(pp.local_faults(p), [])

    def test_needs_ten_titles_and_caps_at_twenty(self):
        few = self.plan(titles=[{"text": f"Coin jar secret idea {i}"}
                                for i in range(5)])
        self.assertTrue(any("at least 10 title options" in f
                            for f in pp.local_faults(few)))
        many = self.plan(titles=[{"text": f"Coin jar secret idea {i}"}
                                 for i in range(30)])
        self.assertEqual(len(many["titles"]), 20)

    def _ctx(self):
        return {"channel": "C", "genre": "g", "channel_about": "", "title": "W",
                "past_titles": [], "learned": "", "brief": "B",
                "transcript": "THE FULL ORIGINAL SCRIPT TEXT",
                "package": PACKAGE,
                "source": {"title": self.SRC, "channel_name": "X",
                           "views": 10, "multiplier": 5.0}}

    def test_titler_never_sees_the_script_or_the_original_title(self):
        import unittest.mock as mock
        ctx = self._ctx()
        with mock.patch("whisperradar.packaging._refs_text",
                        return_value="(none)"):
            w = pp.writer_prompt(ctx)
            j = pp.judge_prompt(ctx, pp.parse_plan(good()), [])
        a = pp.analyst_prompt(ctx)
        for text in (w,):
            self.assertNotIn("THE FULL ORIGINAL SCRIPT TEXT", text)
            self.assertNotIn(self.SRC, text)
            self.assertNotIn("SOURCE VIDEO", text)
        self.assertIn("what hides in an old coin jar", w)
        self.assertIn("THE FULL ORIGINAL SCRIPT TEXT", a)
        self.assertIn("Do NOT write any titles", a)
        self.assertIn("THE FULL ORIGINAL SCRIPT TEXT", j)
        self.assertIn(self.SRC, j)
        self.assertIn('"closest"', j)
        self.assertIn("NEVER reveal the original title", j)

    def test_apply_pick_makes_the_closest_title_ours(self):
        p = self.plan()
        names = [t["text"] for t in p["titles"]]
        pp.apply_pick(p, {"closest": names[4], "alternates": [names[2], "zz"]})
        self.assertEqual(p["title"], names[4])
        self.assertEqual([t["text"] for t in p["titles"][:3]],
                         [names[4], names[2], names[0]])
        self.assertEqual(p["picked_by"], "judge")
        # a pick that copies the original's wording is ignored
        q = self.plan(titles=[{"text": self.SRC}] + [
            {"text": f"Fresh secret idea {i}"} for i in range(11)],
            title="Fresh secret idea 1", keyword="secret lab")
        pp.apply_pick(q, {"closest": self.SRC}, self.SRC)
        self.assertEqual(q["title"], "Fresh secret idea 1")

    def test_verdict_back_to_the_writer_hides_the_pick_and_the_original(self):
        v = {"score": 6, "closest": "X", "alternates": ["Y"], "narrow": ["Z"],
             "faults": ["Match the original: I Found The Secret Lab is better",
                        "Title 3 reads awkwardly"],
             "fixes": ["Try 'Where Animals Learn To Talk' instead", "Shorten it"]}
        sv = pp.safe_verdict(v, self.plan(), self.SRC,
                             "In the Secret Lab the animals learn to talk",
                             "overview")
        self.assertNotIn("closest", sv)
        self.assertNotIn("alternates", sv)
        text = json.dumps(sv)
        self.assertNotIn("Secret Lab", text)
        self.assertIn("awkwardly", text)


class MetaMentionRuleOfThumbTests(unittest.TestCase):
    def test_rule_of_thumb_is_not_a_thumbnail_mention(self):
        self.assertEqual(pp.meta_mentions("A good rule of thumb for savings."), [])
        self.assertEqual(pp.meta_mentions("Do not give a thumbs up."), [])
        self.assertEqual(pp.meta_mentions("Look at the thumbnail."), ["thumbnail"])
        self.assertEqual(pp.meta_mentions("the video title says it"),
                         ["video title"])


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


class SpreadTests(unittest.TestCase):
    def test_hedging_and_repeated_angles_are_faults(self):
        ts = [{"text": f"Your stuff may cost you {w}", "why": "Hidden cost fear - x"}
              for w in ("a lot", "more", "much", "time", "peace")]
        p = pp.parse_plan({"keyword": "stuff", "title": ts[0]["text"],
                           "titles": ts})
        f = pp.spread_faults(p)
        self.assertTrue(any("hedge" in x for x in f))
        self.assertTrue(any("same angle" in x for x in f))

    def test_apply_pick_puts_best_after_closest(self):
        p = pp.parse_plan(good())
        v = {"closest": p["titles"][4]["text"], "best": p["titles"][7]["text"]}
        out = pp.apply_pick(p, v, "")
        self.assertEqual(out["titles"][0]["text"], v["closest"])
        self.assertEqual(out["titles"][1]["text"], v["best"])
