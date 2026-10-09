"""Thumbnails: concepts, the writer/judge loop, compositing, the page."""
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from tests.test_external_prompts import Base  # noqa: E402
from tests.test_packaging import Fake  # noqa: E402
from whisperradar import thumbnails as th  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


def concept(layout="character", text="NOBODY KNEW", **over):
    c = {"layout": layout, "text": text, "text_pos": "left",
         "text_color": "#FFFFFF", "accent": "#FFD400",
         "art_prompt": f"A {layout} scene, bright", "idea": "curiosity"}
    c.update(over)
    return c


def three():
    return [concept("character_host", "WAIT, WHAT?"),
            concept("character", "NOBODY KNEW"),
            concept("host", "THE REAL SECRET")]


def reply(concepts):
    return json.dumps({"concepts": concepts})


def png(path, size=(800, 450), color=(30, 120, 200)):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


class ParseTests(unittest.TestCase):
    def test_text_is_trimmed_to_four_short_words(self):
        self.assertEqual(th.clean_text("  one two three four five six "),
                         "one two three four")
        self.assertLessEqual(len(th.clean_text("extraordinarily " * 5)),
                             th.TEXT_MAX_CHARS)

    def test_concepts_are_normalised_with_ids(self):
        got = th.parse_concepts({"concepts": three()})
        self.assertEqual([c["id"] for c in got], ["c1", "c2", "c3"])
        self.assertEqual(got[0]["layout"], "character_host")

    def test_layout_aliases_and_bad_entries(self):
        raw = [concept("both"), concept("nonsense"), "x",
               concept("host", art_prompt="")]
        got = th.parse_concepts(raw)
        self.assertEqual([c["layout"] for c in got], ["character_host"])

    def test_bad_colours_and_positions_fall_back(self):
        c = th.parse_concepts([concept(text_color="red", text_pos="middle")])[0]
        self.assertEqual((c["text_color"], c["text_pos"]), ("#FFFFFF", "left"))

    def test_at_most_five(self):
        self.assertEqual(len(th.parse_concepts([concept()] * 9)), 5)

    def test_faults(self):
        ok = th.parse_concepts(three())
        self.assertEqual(th.local_faults(ok, "A title"), [])
        self.assertTrue(th.local_faults(ok[:2]))                    # too few
        same = th.parse_concepts([concept("host", "SAME")] * 3)
        self.assertTrue(any("layouts" in f for f in th.local_faults(same)))
        self.assertTrue(any("same text" in f for f in th.local_faults(same)))
        rep = th.parse_concepts(three())
        self.assertTrue(any("repeats the title" in f
                            for f in th.local_faults(rep, "wait, what?")))
        empty = th.parse_concepts(three())
        empty[0]["text"] = ""
        self.assertTrue(any("empty" in f for f in th.local_faults(empty)))


class ComposeTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = Path(tempfile.mkdtemp())

    def test_every_position_gives_1280x720_under_2mb(self):
        art = png(self.dir / "a.png", (1600, 900))
        for pos in th.POSITIONS:
            c = {"text": "Nobody knew this", "text_pos": pos,
                 "text_color": "#FFFFFF", "accent": "#FFD400"}
            out = th.compose(art, c, self.dir / f"{pos}.jpg")
            im = Image.open(out)
            self.assertEqual(im.size, (1280, 720))
            self.assertLess(out.stat().st_size, th.MAX_BYTES)

    def test_text_changes_the_picture_where_it_is_placed(self):
        art = png(self.dir / "a.png", (1280, 720), (200, 220, 255))
        plain = th.compose(art, {"text": "", "text_pos": "left"},
                           self.dir / "p.jpg")
        txt = th.compose(art, {"text": "Hi", "text_pos": "left",
                               "text_color": "#FFFFFF", "accent": "#FFD400"},
                         self.dir / "t.jpg")
        a, b = Image.open(plain).convert("L"), Image.open(txt).convert("L")
        left = (0, 0, 640, 720)
        right = (700, 0, 1280, 720)
        self.assertNotEqual(a.crop(left).tobytes(), b.crop(left).tobytes())
        self.assertEqual(a.crop(right).tobytes(), b.crop(right).tobytes())

    def test_wrong_aspect_art_is_covered_not_stretched(self):
        art = png(self.dir / "tall.png", (500, 1000))
        out = th.compose(art, {"text": "", "text_pos": "left"},
                         self.dir / "o.jpg")
        self.assertEqual(Image.open(out).size, (1280, 720))


class SimilarityTests(unittest.TestCase):
    def test_alike_pictures_rank_first(self):
        import tempfile
        from PIL import Image, ImageDraw
        with tempfile.TemporaryDirectory() as d:
            def make(name, bg, box):
                im = Image.new("RGB", (320, 180), bg)
                ImageDraw.Draw(im).rectangle(box, fill=(250, 250, 250))
                p = Path(d) / name
                im.save(p)
                return str(p)
            ref = make("ref.png", (200, 30, 30), (200, 20, 300, 160))
            near = make("near.png", (210, 40, 35), (190, 25, 295, 150))
            far = make("far.png", (20, 40, 200), (10, 20, 100, 160))
            self.assertGreater(th.visual_similarity(ref, near),
                               th.visual_similarity(ref, far))
            self.assertEqual(th.rank_by_similarity([far, near], ref),
                             [near, far])
            self.assertEqual(th.rank_by_similarity(
                [str(Path(d) / "missing.png"), near], ref)[0], near)


class PhoneMetricsTests(unittest.TestCase):
    def test_flat_grey_warns_and_punchy_image_does_not(self):
        import tempfile
        from PIL import Image, ImageDraw
        with tempfile.TemporaryDirectory() as d:
            flat = Path(d) / "flat.jpg"
            Image.new("RGB", (640, 360), (120, 120, 120)).save(flat)
            w = th.phone_metrics(flat)["warnings"]
            self.assertTrue(any("flat" in x for x in w))
            self.assertTrue(any("washed" in x for x in w))
            punchy = Path(d) / "punchy.jpg"
            im = Image.new("RGB", (640, 360), (230, 30, 30))
            ImageDraw.Draw(im).rectangle((0, 0, 320, 360), fill=(250, 220, 0))
            ImageDraw.Draw(im).ellipse((380, 80, 600, 300), fill=(10, 20, 120))
            im.save(punchy)
            self.assertEqual(th.phone_metrics(punchy)["warnings"], [])


class JudgeEmphasisTests(unittest.TestCase):
    def test_judge_prompt_covers_attention_devices(self):
        ctx = {"channel": "c", "genre": "g", "kit_title": "t", "script": "s"}
        self.assertIn("ATTENTION DEVICES",
                      th.judge_prompt(ctx, [], []))


class EmphasisTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = Path(tempfile.mkdtemp())
        self.art = png(self.dir / "a.png", (1280, 720), (200, 220, 255))

    def base(self, **over):
        c = {"text": "WAIT", "text_pos": "left", "text_color": "#FFFFFF",
             "accent": "#FFD400"}
        c.update(over)
        return c

    def px(self, concept, name):
        out = th.compose(self.art, concept, self.dir / f"{name}.jpg")
        return Image.open(out).convert("RGB")

    def test_parse_keeps_emphasis_and_focal_and_defaults(self):
        got = th.parse_concepts([
            concept(emphasis="Ellipse", focal="RIGHT"),
            concept("host", emphasis="sparkles", focal="nowhere"),
            concept("host")])
        self.assertEqual([(c["emphasis"], c["focal"]) for c in got],
                         [("ellipse", "right"), ("none", "auto"),
                          ("none", "auto")])

    def test_concepts_json_round_trips_emphasis_and_focal(self):
        got = th.parse_concepts([concept(emphasis="arrow", focal="top")])
        back = th.parse_concepts(json.loads(th._concepts_json(got)))
        self.assertEqual((back[0]["emphasis"], back[0]["focal"]),
                         ("arrow", "top"))

    def test_writer_prompt_explains_focal_and_follows_the_original(self):
        ctx = {"channel": "C", "genre": "g", "channel_about": "", "kit_title": "T",
               "keyword": "k", "script": "s", "bible": "b", "ref_text": "-",
               "refs": [], "plan": {}, "learned": ""}
        text = th.writer_prompt(ctx, has_inspiration=True)
        self.assertIn('"focal"', text)
        self.assertIn("OPPOSITE text_pos", text)
        self.assertIn("FOLLOW the ORIGINAL", text)
        self.assertIn("composition", text)

    def test_device_on_the_text_side_is_a_rule_fault(self):
        cs = th.parse_concepts(three())
        cs[0].update(emphasis="ellipse", focal="left", text_pos="left")
        self.assertTrue(any("sit on the words" in f
                            for f in th.local_faults(cs)))
        cs[0]["focal"] = "right"
        self.assertFalse(any("sit on the words" in f
                             for f in th.local_faults(cs)))

    def test_every_device_changes_the_picture_and_stays_small(self):
        plain = self.px(self.base(), "none")
        for kind in ("ellipse", "arrow", "underline"):
            im = self.px(self.base(emphasis=kind), kind)
            self.assertEqual(im.size, (1280, 720))
            self.assertNotEqual(plain.tobytes(), im.tobytes(), kind)
            self.assertLess((self.dir / f"{kind}.jpg").stat().st_size,
                            th.MAX_BYTES)

    def test_device_without_text_does_not_raise(self):
        plain = self.px(self.base(text=""), "t0")
        for kind in ("ellipse", "arrow", "underline"):
            im = self.px(self.base(text="", emphasis=kind), f"t{kind}")
            if kind != "underline":     # nothing to underline without words
                self.assertNotEqual(plain.tobytes(), im.tobytes(), kind)

    def test_focal_point_moves_the_device(self):
        red = th.EMPHASIS_RGB

        def red_centre(focal):
            im = self.px(self.base(text="", emphasis="ellipse", focal=focal),
                         f"f_{focal}")
            xs, ys = [], []
            data = im.load()
            for y in range(0, 720, 4):
                for x in range(0, 1280, 4):
                    r, g, b = data[x, y]
                    if r > 200 and g < 90 and b < 90 and abs(r - red[0]) < 40:
                        xs.append(x)
                        ys.append(y)
            self.assertTrue(xs, focal)
            return sum(xs) / len(xs), sum(ys) / len(ys)

        lx, _ = red_centre("left")
        rx, _ = red_centre("right")
        _, ty = red_centre("top")
        _, by = red_centre("bottom")
        self.assertLess(lx, 640 - 150)
        self.assertGreater(rx, 640 + 150)
        self.assertLess(ty, by)

    def test_auto_focal_keeps_the_old_opposite_side_behaviour(self):
        self.assertEqual(th.focal_point({"text_pos": "left"}),
                         (th.W * 0.72, th.H * 0.52))
        self.assertEqual(th.focal_point({"text_pos": "right"}),
                         (th.W * 0.28, th.H * 0.52))
        self.assertEqual(th.focal_point({"text_pos": "top", "focal": "auto"}),
                         (th.W * 0.5, th.H * 0.72))
        self.assertEqual(th.focal_point({"focal": "center"}),
                         (th.W * 0.5, th.H * 0.5))


class ProductionTests(Base):
    def setUp(self):
        super().setUp()
        (self.pdir / "script.md").write_text("A script about coins. " * 30,
                                             "utf-8")

    def verdict(self, score, **kw):
        return json.dumps({"score": score, "pass": score >= 8,
                           "faults": kw.get("faults", []), "fixes": []})

    def saved(self, n=3):
        data = th.empty_thumbs()
        data["concepts"] = th.parse_concepts(three()[:n])
        th.save_thumbs(self.pdir, data)
        return data

    # ---- the loop
    def test_loop_passes_and_saves(self):
        t = Fake(zai=[reply(three())], deepseek=[self.verdict(9)])
        data = th.run_concepts(self.cfg, self.pid, t, log=lambda m: None)
        self.assertEqual(data["status"], "ready")
        self.assertEqual(len(th.load_thumbs(self.pdir)["concepts"]), 3)
        self.assertIn("character_host", t.calls[0]["prompt"])
        self.assertEqual([c["new_chat"] for c in t.calls], [True, True])

    def test_verbatim_verdict_goes_back_and_a_fixed_set_passes(self):
        better = three()
        better[1]["text"] = "NOBODY SAW THIS"
        t = Fake(zai=[reply(three()), reply(better)],
                 deepseek=[self.verdict(5, faults=["weak words"]),
                           self.verdict(9)])
        data = th.run_concepts(self.cfg, self.pid, t, log=lambda m: None)
        self.assertEqual(data["concepts"][1]["text"], "NOBODY SAW THIS")
        self.assertIn("weak words", t.calls[2]["prompt"])
        self.assertEqual([(c["site"], c["new_chat"]) for c in t.calls],
                         [("zai", True), ("deepseek", True),
                          ("zai", False), ("deepseek", False)])

    def test_stop_keeps_the_best_as_a_draft(self):
        t = Fake(zai=[reply(three())], deepseek=[self.verdict(4)])
        data = th.run_concepts(self.cfg, self.pid, t, log=lambda m: None,
                               should_stop=lambda: True)
        self.assertEqual(data["status"], "draft")

    def test_unusable_reply_is_asked_again(self):
        t = Fake(zai=["no idea", reply(three())], deepseek=[self.verdict(9)])
        data = th.run_concepts(self.cfg, self.pid, t, log=lambda m: None)
        self.assertEqual(data["status"], "ready")
        self.assertIn("no usable concepts", t.calls[1]["prompt"])

    def test_redo_keeps_pictures_whose_prompt_did_not_change(self):
        first = self.saved()
        art = png(self.pdir / "thumbnails" / "art" / "c1.png")
        first["concepts"][0]["art_file"] = "thumbnails/art/c1.png"
        th.save_thumbs(self.pdir, first)
        t = Fake(zai=[reply(three())], deepseek=[self.verdict(9)])
        data = th.run_concepts(self.cfg, self.pid, t, log=lambda m: None)
        self.assertEqual(data["concepts"][0]["art_file"],
                         "thumbnails/art/c1.png")
        self.assertEqual(data["concepts"][1]["art_file"], "")
        self.assertTrue(art.exists())

    def test_no_script_fails_clearly(self):
        (self.pdir / "script.md").unlink()
        from whisperradar import webstages
        with self.assertRaises(webstages.StageFailed):
            th.run_concepts(self.cfg, self.pid, Fake(), log=lambda m: None)

    # ---- the art
    def fake_engine(self, calls, name):
        def engine(cfg, pdir, pid, prompts, *a, **k):
            calls.append((name, dict(prompts)))
            for cid in prompts:
                png(Path(pdir) / "thumbnails" / "art" / f"{cid}.png",
                    (1024, 576))
        return engine

    def test_art_goes_through_the_flowbatch_engine_and_composes(self):
        self.saved()
        calls = []
        with mock.patch.object(th, "_engine_flowbatch",
                               self.fake_engine(calls, "flow")), \
             mock.patch.object(th, "_engine_renderly",
                               self.fake_engine(calls, "renderly")), \
             mock.patch("whisperradar.studio.effective_engine",
                        return_value="flowbatch"):
            res = th.generate_art(self.cfg, self.pid, log=lambda m: None)
        self.assertEqual(res["made"], ["c1", "c2", "c3"])
        self.assertEqual([c[0] for c in calls], ["flow"])
        data = th.load_thumbs(self.pdir)
        for c in data["concepts"]:
            self.assertTrue((self.pdir / c["final"]).is_file())
        self.assertTrue((self.pdir / "thumbnails" / "sheet.jpg").is_file())
        # the engine prompt asks for room for the words and no text
        prompt = calls[0][1]["c1"]
        self.assertIn("No text", prompt)
        self.assertIn("empty", prompt)

    def test_other_engines_use_renderly_and_a_single_id_only_makes_it(self):
        self.saved()
        calls = []
        with mock.patch.object(th, "_engine_flowbatch",
                               self.fake_engine(calls, "flow")), \
             mock.patch.object(th, "_engine_renderly",
                               self.fake_engine(calls, "renderly")), \
             mock.patch("whisperradar.studio.effective_engine",
                        return_value="renderly"), \
             mock.patch("whisperradar.studio.resolve_renderly_channel",
                        return_value=None):
            res = th.generate_art(self.cfg, self.pid, ["c2"],
                                  log=lambda m: None)
        self.assertEqual(res["made"], ["c2"])
        self.assertEqual(calls[0][0], "renderly")
        self.assertEqual(list(calls[0][1]), ["c2"])

    def test_a_picture_that_did_not_come_back_is_reported(self):
        self.saved()
        logs = []
        with mock.patch.object(th, "_engine_flowbatch", lambda *a, **k: None), \
             mock.patch("whisperradar.studio.effective_engine",
                        return_value="flowbatch"):
            res = th.generate_art(self.cfg, self.pid, log=logs.append)
        self.assertEqual(res["made"], [])
        self.assertEqual(len(res["missing"]), 3)
        self.assertTrue(any("no picture came back" in m for m in logs))

    def test_art_prompt_puts_the_subject_away_from_the_words(self):
        c = th.parse_concepts([concept("host", text_pos="left")])[0]
        p = th.art_prompt_for(c)
        self.assertIn("on the right of the frame", p)
        self.assertIn("empty space on the left", p)

    def test_set_art_uses_your_picture_and_composes(self):
        self.saved()
        src = png(self.pdir / "mine.png", (900, 900))
        self.assertTrue(th.set_art(self.pdir, "c2", src))
        c = th.load_thumbs(self.pdir)["concepts"][1]
        self.assertTrue((self.pdir / c["final"]).is_file())
        self.assertFalse(th.set_art(self.pdir, "zz", src))


class PageTests(Base):
    def setUp(self):
        super().setUp()
        self.client = create_app(self.cfg).test_client()
        data = th.empty_thumbs()
        data["concepts"] = th.parse_concepts(three())
        th.save_thumbs(self.pdir, data)
        png(self.pdir / "thumbnails" / "art" / "c1.png")
        data["concepts"][0]["art_file"] = "thumbnails/art/c1.png"
        th.save_thumbs(self.pdir, data)
        th.compose_all(self.pdir)

    def url(self, tail=""):
        return f"/studio/{self.pid}/thumbnails{tail}"

    def test_page_shows_the_concepts_and_the_phone_sheet(self):
        (self.pdir / "script.md").write_text("x " * 50, "utf-8")
        html = self.client.get(self.url()).get_data(as_text=True)
        self.assertIn("the character and the host together", html)
        self.assertIn("WAIT, WHAT?", html)
        self.assertIn("sheet.jpg", html)
        self.assertIn("Redesign concepts", html)

    def test_edit_words_recomposes_and_choose_toggles(self):
        r = self.client.post(self.url("/save"), data={
            "text_c1": "ONE TWO THREE FOUR FIVE", "pos_c1": "right",
            "color_c1": "#ff0000", "accent_c1": "bad"})
        self.assertEqual(r.status_code, 302)
        c = th.load_thumbs(self.pdir)["concepts"][0]
        self.assertEqual(c["text"], "ONE TWO THREE FOUR")
        self.assertEqual((c["text_pos"], c["text_color"], c["accent"]),
                         ("right", "#ff0000", "#FFD400"))
        self.client.post(self.url("/choose"), data={"id": "c1"})
        self.assertEqual(th.load_thumbs(self.pdir)["chosen"], "c1")
        self.client.post(self.url("/choose"), data={"id": "c1"})
        self.assertIsNone(th.load_thumbs(self.pdir)["chosen"])
        r = self.client.post(self.url("/choose"), data={"id": "nope"})
        self.assertIn("error", r.headers["Location"])

    def test_edit_emphasis_and_focal_from_the_page(self):
        self.client.post(self.url("/save"), data={
            "emph_c1": "arrow", "focal_c1": "right",
            "emph_c2": "bogus", "focal_c2": "nowhere"})
        cs = th.load_thumbs(self.pdir)["concepts"]
        self.assertEqual((cs[0]["emphasis"], cs[0]["focal"]), ("arrow", "right"))
        self.assertEqual((cs[1]["emphasis"], cs[1]["focal"]), ("none", "auto"))
        (self.pdir / "script.md").write_text("x " * 50, "utf-8")
        html = self.client.get(self.url()).get_data(as_text=True)
        self.assertIn("Attention device", html)
        self.assertIn('name="focal_c1"', html)

    def test_upload_your_own_picture(self):
        buf = io.BytesIO()
        Image.new("RGB", (640, 360), (9, 99, 9)).save(buf, "PNG")
        buf.seek(0)
        r = self.client.post(self.url("/picture"), data={
            "id": "c3", "file": (buf, "mine.png")},
            content_type="multipart/form-data")
        self.assertIn("msg=", r.headers["Location"])
        c = th.load_thumbs(self.pdir)["concepts"][2]
        self.assertTrue((self.pdir / c["final"]).is_file())
        bad = self.client.post(self.url("/picture"), data={
            "id": "c3", "file": (io.BytesIO(b"x"), "x.exe")},
            content_type="multipart/form-data")
        self.assertIn("error=", bad.headers["Location"])

    def test_concepts_need_a_script_and_art_needs_concepts(self):
        r = self.client.post(self.url("/concepts"), data={})
        self.assertIn("no%20script", r.headers["Location"])
        shutil_dir = self.pdir / "thumbnails"
        for f in shutil_dir.rglob("*"):
            if f.is_file():
                f.unlink()
        r = self.client.post(self.url("/art"), data={})
        self.assertIn("Design%20the%20concepts%20first", r.headers["Location"])

    def test_finished_page_links_to_thumbnails(self):
        conn = __import__("whisperradar.db", fromlist=["db"]).connect(
            self.cfg.db_path)
        from whisperradar import db
        db.update_production(conn, self.pid, status="ready")
        conn.commit()
        conn.close()
        html = self.client.get("/finished").get_data(as_text=True)
        self.assertIn(f"/studio/{self.pid}/thumbnails", html)
        self.assertIn("3 thumbnails", html)

    def test_unknown_production_redirects(self):
        self.assertEqual(self.client.get("/studio/9999/thumbnails")
                         .status_code, 302)


if __name__ == "__main__":
    unittest.main()
