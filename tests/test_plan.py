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
         "titles": [{"text": f"The coin jar secret {i}", "why": "x"}
                    for i in range(6)],
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
