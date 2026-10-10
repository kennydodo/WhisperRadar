"""Port of ShotListTests.cs + MissingShotListTests.cs + export-batch, plus golden checks."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from whisperradar.video import export_batch as eb
from whisperradar.video import missing_shotlist as ms
from whisperradar.video import models as m
from whisperradar.video import options as opt
from whisperradar.video import parsing
from whisperradar.video import shotlist as sl

FIXTURES = Path(__file__).parent / "fixtures" / "video"


def subtitles():
    return [m.SubtitleBlock(1, 0.0, 5.0, "First line one."),
            m.SubtitleBlock(2, 5.2, 9.5, "First line two."),
            m.SubtitleBlock(3, 10.2, 15.0, "Second line one."),
            m.SubtitleBlock(4, 15.2, 19.0, "Second line two.")]


IMAGES = ["S01_01_SCN.png", "S01_02_CU_ZI.png"]


def do_parse(text, master=0, prompt=0):
    issues = []
    return sl.parse(text, issues, master, prompt), issues


def do_expand(text, issues=None, subs=None, images=None):
    return sl.expand(do_parse(text)[0], subs or subtitles(), images or IMAGES,
                     opt.TransitionOptions(), [] if issues is None else issues)


class ParserTests(unittest.TestCase):
    def test_style_and_prompts(self):
        doc, issues = do_parse("""{"style": "Warm documentary photo style",
          "images": [{"file": "S01_01_SCN.png", "prompt": "A walker at dawn."},
                     {"file": "S01_02_CU_ZI.png"}],
          "shots": [{"cues": "1", "asset": "S01_01_SCN.png"}]}""")
        self.assertEqual("Warm documentary photo style", doc.style)
        self.assertEqual(2, len(doc.prompts))
        self.assertEqual("A walker at dawn.", doc.prompts["S01_01_SCN.png"])
        self.assertEqual("", doc.prompts["S01_02_CU_ZI.png"])
        self.assertEqual("A walker at dawn.", doc.prompts["s01_01_scn.png"])  # case-insensitive
        self.assertFalse(any(i.severity == m.ERROR for i in issues))

    def test_master_prompt_long(self):
        _, issues = do_parse('{"style": "%s", "shots": [{"cues": "1", "asset": "a.png"}]}' % ("s" * 1600),
                             master=1500)
        found = [i for i in issues if i.code == "SHOTLIST_MASTER_PROMPT_LONG"]
        self.assertEqual(1, len(found))
        self.assertEqual(m.WARNING, found[0].severity)
        self.assertIn("1600", found[0].message)
        self.assertIn("1500", found[0].message)

    def test_image_prompt_long(self):
        text = ('{"images": [{"file": "S01_01_SCN.png", "prompt": "%s"}, '
                '{"file": "S01_02_CU_ZI.png", "prompt": "short"}], '
                '"shots": [{"cues": "1", "asset": "S01_01_SCN.png"}]}') % ("p" * 2500)
        _, issues = do_parse(text, prompt=2400)
        found = [i for i in issues if i.code == "SHOTLIST_PROMPT_LONG"]
        self.assertEqual(1, len(found))
        self.assertIn("S01_01_SCN.png", found[0].message)
        self.assertIn("2500", found[0].message)

    def test_limits_disabled_or_not_exceeded(self):
        text = ('{"style": "short style", "images": [{"file": "S01_01_SCN.png", "prompt": "short"}], '
                '"shots": [{"cues": "1", "asset": "S01_01_SCN.png"}]}')
        for a, b in ((0, 0), (1500, 2400)):
            _, issues = do_parse(text, a, b)
            self.assertFalse([i for i in issues if i.code in
                              ("SHOTLIST_MASTER_PROMPT_LONG", "SHOTLIST_PROMPT_LONG")])

    def test_bad_cue_specs(self):
        doc, issues = do_parse("""{"shots": [
          {"cues": "x", "asset": "S01_01_SCN.png"},
          {"cues": 0, "asset": "S01_01_SCN.png"},
          {"asset": "S01_01_SCN.png"}]}""")
        self.assertEqual([], doc.shots)
        self.assertEqual(3, len([i for i in issues if i.code in ("SHOTLIST_CUES_INVALID", "SHOTLIST_INVALID")]))

    def test_prompts_without_file(self):
        doc, issues = do_parse("""{"images": [{"prompt": "no file here"}],
           "shots": [{"cues": "1", "asset": "S01_01_SCN.png"}]}""")
        self.assertEqual(1, len(doc.shots))
        self.assertEqual(0, len(doc.prompts))
        self.assertTrue(any(i.code == "SHOTLIST_INVALID" for i in issues))

    def test_missing_shots_array_throws(self):
        with self.assertRaises(ValueError):
            do_parse('{"nope": []}')
        with self.assertRaises(ValueError):
            do_parse('{broken')

    def test_cue_forms(self):
        doc, _ = do_parse("""{"shots": [
          {"cues": "1-3", "asset": "a"}, {"cues": "4/6", "asset": "a"}, {"cues": [5, 2, 3], "asset": "a"},
          {"cues": "3,4", "asset": "a"}, {"cues": [1.5], "asset": "a"}, {"cues": "1-2-3", "asset": "a"},
          {"cues": [true], "asset": "a"}, {"cues": " 7 ", "asset": "a"}]}""")
        self.assertEqual([(1, 3), (4, 6), (2, 5), (3, 4), (7, 7)],
                         [(e.first_cue, e.last_cue) for e in doc.shots])

    def test_motion_transition_framing(self):
        doc, _ = do_parse("""{"shots": [{"cues": "1", "asset": "a", "motion": {"start_scale": 1, "end_scale": 1.2,
          "duration_ms": 3000}, "transition": {"duration_ms": 300}, "framing": {"focal_region": "r"}},
          {"cues": "2", "asset": "b", "motion": "ZI", "transition": "DIP", "framing": "tight"}]}""")
        a, b = doc.shots
        self.assertEqual(("STATIC", 1.0, 1.2, 3000), (a.motion.type, a.motion.start_scale,
                                                       a.motion.end_scale, a.motion.duration_ms))
        self.assertEqual(("CUT", 300), (a.transition.type, a.transition.duration_ms))
        self.assertEqual(("wide", "r"), (a.framing.type, a.framing.focal_region))
        self.assertEqual("ZI", b.motion.type)
        self.assertEqual(("DIP", 0), (b.transition.type, b.transition.duration_ms))
        self.assertEqual(("tight", None), (b.framing.type, b.framing.focal_region))

    def test_reveal_forms(self):
        doc, issues = do_parse("""{"shots": [
          {"cues": "1-4", "asset": "a", "reveal": [1, 2, 3]},
          {"cues": "1-4", "asset": "a", "reveal": {"cues": [1, 2, 3, 4], "layout": "grid"}},
          {"cues": "1-4", "asset": "a", "reveal": {"cues": [1, 2], "assets": ["x.png", "y.png"]}},
          {"cues": "1-4", "asset": "a", "reveal": [1]},
          {"cues": "1-4", "asset": "a", "reveal": {"cues": [1, 2, 3], "layout": "grid"}},
          {"cues": "1-4", "asset": "a", "reveal": {"cues": [1, 2, 3], "layout": "spiral"}},
          {"cues": "1-4", "asset": "a", "reveal": {"cues": [1, 2, 3], "assets": ["x.png"]}},
          {"cues": "1-4", "asset": "a", "reveal": {"cues": [1, 2], "assets": [3, 4]}}]}""")
        s = doc.shots
        self.assertEqual(([1, 2, 3], "row", None), (s[0].reveal_cues, s[0].reveal_layout, s[0].reveal_assets))
        self.assertEqual(("grid", [1, 2, 3, 4]), (s[1].reveal_layout, s[1].reveal_cues))
        self.assertEqual(["x.png", "y.png"], s[2].reveal_assets)
        for i in range(3, 8):
            self.assertIsNone(s[i].reveal_cues)
        msgs = [i.message for i in issues if i.code == "SHOTLIST_REVEAL_INVALID"]
        self.assertEqual(5, len(msgs))
        self.assertIn("(a grid reveal needs exactly 4 items)", msgs[1])
        self.assertIn('("layout" must be "row" or "grid")', msgs[2])
        self.assertIn('("assets" must have one image per cue)', msgs[3])
        self.assertIn('("assets" must be a list of image file names)', msgs[4])
        self.assertTrue(msgs[0].endswith("The whole image is shown instead."))

    def test_sfx(self):
        doc, issues = do_parse("""{"shots": [
          {"cues": "1", "asset": "a", "sfx": " pop "}, {"cues": "2", "asset": "a", "sfx": ["pop", "ding 2"]},
          {"cues": "3", "asset": "a", "sfx": "../evil"}, {"cues": "4", "asset": "a", "sfx": [1]}]}""")
        self.assertEqual(["pop"], doc.shots[0].sfx)
        self.assertEqual(["pop", "ding 2"], doc.shots[1].sfx)
        self.assertIsNone(doc.shots[2].sfx)
        self.assertIsNone(doc.shots[3].sfx)
        self.assertEqual(2, len([i for i in issues if i.code == "SHOTLIST_SFX_INVALID"]))


class ExpanderTests(unittest.TestCase):
    def test_cue_ranges_derive_timing(self):
        mf = do_expand("""{"shots": [
          {"cues": "1-3", "asset": "S01_01_SCN.png", "motion": "ZI"},
          {"cues": "4", "asset": "S01_02_CU_ZI.png", "transition": "CROSSFADE"}]}""")
        self.assertEqual(2, len(mf.timeline))
        s1 = mf.timeline[0]
        self.assertEqual(("shot-001", 0, 15000, "S01", "c1", [1, 2, 3]),
                         (s1.shot_id, s1.start_ms, s1.end_ms, s1.scene_id, s1.beat_id, s1.srt_cue_ids))
        self.assertEqual("First line one. First line two. Second line one.", s1.narration_text)
        self.assertEqual("S01_01_SCN.png", mf.assets[0].file)
        self.assertEqual("ZI", s1.motion.type)
        self.assertIsNone(s1.transition_out)
        s2 = mf.timeline[1]
        self.assertEqual((15200, 19000), (s2.start_ms, s2.end_ms))
        self.assertEqual(("CROSSFADE", 500), (s2.transition_out.type, s2.transition_out.duration_ms))

    def test_array_and_comma_forms(self):
        mf = do_expand("""{"shots": [{"cues": [1, 2], "asset": "S01_01_SCN.png"},
                                      {"cues": "3,4", "asset": "S01_02_CU_ZI.png"}]}""")
        self.assertEqual([1, 2], mf.timeline[0].srt_cue_ids)
        self.assertEqual([3, 4], mf.timeline[1].srt_cue_ids)
        self.assertEqual((9500, 19000), (mf.timeline[0].end_ms, mf.timeline[1].end_ms))

    def test_stems_match_without_extension(self):
        mf = do_expand("""{"shots": [{"cues": "1", "asset": "S01_02_CU_ZI"},
                                      {"cues": "2", "asset": "s01_01_scn"}]}""")
        self.assertEqual("S01_02_CU_ZI.png", mf.assets[0].file)
        self.assertEqual("S01_01_SCN.png", mf.assets[1].file)

    def test_typo_flagged_but_not_blocking(self):
        issues = []
        mf = do_expand('{"shots": [{"cues": "1", "asset": "S01_01_SCN_ZI.png"}]}', issues)
        self.assertEqual("S01_01_SCN_ZI.png", mf.assets[0].file)
        self.assertTrue(any(i.severity == m.INFO and i.code == "SHOTLIST_ASSET_TYPO"
                            and "S01_01_SCN" in i.message for i in issues))

    def test_missing_files_become_generate_requests(self):
        issues = []
        mf = do_expand("""{"images": [{"file": "S02_01_INF_ZI.png",
              "prompt": "infographic: bar chart of results"}],
            "shots": [{"cues": "1-2", "asset": "S01_01_SCN.png"},
                      {"cues": "3-4", "asset": "S02_01_INF_ZI.png"}]}""",
                       issues, images=["S01_01_SCN.png"])
        self.assertEqual(2, len(mf.timeline))
        shot = mf.timeline[1]
        self.assertEqual("S02_01_INF_ZI.png", shot.asset_id)
        self.assertEqual("infographic: bar chart of results", shot.visual_intent)
        self.assertTrue(any(i.code == "SHOTLIST_GENERATE_AHEAD" and "S02_01_INF_ZI.png" in i.message
                            for i in issues))

    def test_cue_out_of_range(self):
        issues = []
        self.assertIsNone(do_expand('{"shots": [{"cues": "99", "asset": "S01_01_SCN.png"}]}', issues))
        codes = [i.code for i in issues]
        self.assertIn("SHOTLIST_CUE_UNKNOWN", codes)
        self.assertIn("SHOTLIST_EMPTY", codes)

    def test_partial_overlap_clamped(self):
        issues = []
        mf = do_expand("""{"shots": [{"cues": "1-3", "asset": "S01_01_SCN.png"},
                                      {"cues": "2-4", "asset": "S01_02_CU_ZI.png"}]}""", issues)
        self.assertEqual(2, len(mf.timeline))
        self.assertEqual(15000, mf.timeline[1].start_ms)
        self.assertTrue(any(i.code == "SHOTLIST_CUES_OVERLAP" for i in issues))

    def test_fully_covered_dropped(self):
        issues = []
        mf = do_expand("""{"shots": [{"cues": "1-3", "asset": "S01_01_SCN.png"},
                                      {"cues": "2", "asset": "S01_02_CU_ZI.png"}]}""", issues)
        self.assertEqual(1, len(mf.timeline))
        self.assertEqual(15000, mf.timeline[0].end_ms)
        self.assertTrue(any(i.code == "SHOTLIST_CUES_OVERLAP" for i in issues))

    def test_sorted_by_narration_time(self):
        issues = []
        mf = do_expand("""{"shots": [{"cues": "3-4", "asset": "S01_02_CU_ZI.png"},
                                      {"cues": "1-2", "asset": "S01_01_SCN.png"}]}""", issues)
        self.assertEqual(0, mf.timeline[0].start_ms)
        self.assertEqual(10200, mf.timeline[1].start_ms)
        self.assertTrue(any(i.code == "SHOTLIST_REORDERED" for i in issues))
        self.assertEqual("shot-002", mf.timeline[0].shot_id)

    def test_duplicate_ids_drop_later(self):
        issues = []
        mf = do_expand("""{"shots": [{"cues": "1", "asset": "S01_01_SCN.png", "shot_id": "x"},
                                      {"cues": "2", "asset": "S01_02_CU_ZI.png", "shot_id": "x"}]}""", issues)
        self.assertEqual(1, len(mf.timeline))
        self.assertTrue(any(i.code == "SHOTLIST_DUPLICATE_ID" for i in issues))

    def test_empty_and_no_subtitles(self):
        issues = []
        self.assertIsNone(do_expand('{"shots": []}', issues))
        self.assertEqual("SHOTLIST_EMPTY", issues[0].code)
        issues = []
        self.assertIsNone(sl.expand(do_parse('{"shots": [{"cues": "1", "asset": "a"}]}')[0], [], IMAGES,
                                    opt.TransitionOptions(), issues))
        self.assertEqual("SHOTLIST_NO_SUBTITLES", issues[0].code)

    def test_reveal_expansion(self):
        issues = []
        mf = do_expand("""{"shots": [{"cues": "1-4", "asset": "S01_01_SCN.png", "sfx": "pop",
              "reveal": {"cues": [2, 3, 4], "assets": ["S01_02_CU_ZI", "n1", "n2.png"]}}]}""", issues)
        s = mf.timeline[0]
        self.assertEqual([0, 10200, 15200], s.reveal_at_ms)
        self.assertEqual("row", s.reveal_layout)
        self.assertEqual(["S01_02_CU_ZI.png", "n1.png", "n2.png"], s.reveal_assets)
        self.assertEqual("S01_02_CU_ZI.png", s.asset_id)
        self.assertEqual(["S01_02_CU_ZI.png", "n1.png", "n2.png"], [a.id for a in mf.assets])
        self.assertEqual(["pop"], s.sfx)
        self.assertTrue(any(i.code == "SHOTLIST_REVEAL_FIRST" for i in issues))

    def test_reveal_out_of_range_ignored(self):
        issues = []
        mf = do_expand("""{"shots": [{"cues": "1-2", "asset": "S01_01_SCN.png", "reveal": [1, 4]}]}""", issues)
        self.assertIsNone(mf.timeline[0].reveal_at_ms)
        self.assertTrue(any(i.code == "SHOTLIST_REVEAL_INVALID" and "outside the shot's cues 1-2" in i.message
                            for i in issues))


SHOTLIST_JSON = """{
  "style": "Master prompt.",
  "shots": [{ "cues": "1-3", "asset": "S01_01_SCN_ZI.png", "scene": "S01" }],
  "refs": {"maya": "D:/Refs/maya.png", "room": "D:/Refs/room.png"},
  "images": [
    { "file": "S01_01_SCN_ZI.png", "prompt": "on disk already" },
    { "file": "S01_02_CU_ZO.png", "prompt": "missing one", "refs": ["maya"] },
    { "file": "S02_01_INF_ST.png", "prompt": "missing two — dash" }
  ]
}"""


class MissingShotListTests(unittest.TestCase):
    def test_keeps_only_missing(self):
        r = ms.build_from_json(SHOTLIST_JSON, ["S01_01_SCN_ZI.png"])
        self.assertEqual(2, r.count)
        files = [i["file"] for i in json.loads(r.json)["images"]]
        self.assertEqual(["S01_02_CU_ZO.png", "S02_01_INF_ST.png"], files)
        self.assertIn("— dash", r.json)

    def test_style_refs_preserved(self):
        root = json.loads(ms.build_from_json(SHOTLIST_JSON, ["S01_01_SCN_ZI.png"]).json)
        self.assertEqual("Master prompt.", root["style"])
        self.assertIn("maya", root["refs"])
        self.assertNotIn("shots", root)
        self.assertEqual("maya", root["images"][0]["refs"][0])

    def test_case_insensitive(self):
        self.assertEqual(2, ms.build_from_json(SHOTLIST_JSON, ["s01_01_scn_zi.PNG"]).count)

    def test_zero_when_all_present(self):
        r = ms.build_from_json(SHOTLIST_JSON, ["S01_01_SCN_ZI.png", "S01_02_CU_ZO.png", "S02_01_INF_ST.png"])
        self.assertEqual(0, r.count)
        self.assertEqual([], json.loads(r.json)["images"])

    def test_skips_entries_without_file(self):
        r = ms.build_from_json('{ "images": [ { "prompt": "no file" }, { "file": "  " }, { "file": "S01_01.png" } ] }', [])
        self.assertEqual(1, r.count)
        self.assertEqual("S01_01.png", json.loads(r.json)["images"][0]["file"])

    def test_plain_text(self):
        r = ms.build_from_json(SHOTLIST_JSON, ["S01_01_SCN_ZI.png"])
        lines = r.plain_text.split("\n")
        self.assertEqual("Master prompt: Master prompt.", lines[0])
        self.assertEqual("", lines[1])
        self.assertEqual('"file": "S01_02_CU_ZO.png", "prompt": "missing one"', lines[2])
        self.assertEqual('"file": "S02_01_INF_ST.png", "prompt": "missing two — dash"', lines[3])

    def test_plain_text_without_style(self):
        r = ms.build_from_json('{ "images": [ { "file": "S09_01_SCN_ST.png", "prompt": "a prompt" } ] }', [])
        self.assertEqual('"file": "S09_01_SCN_ST.png", "prompt": "a prompt"', r.plain_text)

    def test_for_project(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(ms.build_for_project(d))
            Path(d, "shotlist.json").write_text(SHOTLIST_JSON, encoding="utf-8")
            os.makedirs(Path(d, "images"))
            Path(d, "images", "S01_01_SCN_ZI.png").write_bytes(b"x")
            self.assertEqual(2, ms.build_for_project(d).count)


class ExportBatchTests(unittest.TestCase):
    def test_export_batch(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "shotlist.json").write_text(json.dumps({
                "style": "Style — x", "shots": [{"cues": "1", "asset": "A_01_SCN_ZI.png"}],
                "images": [{"file": "A_01_SCN_ZI.png", "prompt": "p1"},
                           {"file": "A_02_SCN_PL.png", "prompt": "p2 — é"},
                           {"file": "A_03_SCN_PU.png"}, {"file": "odd.png"}]}, ensure_ascii=False),
                encoding="utf-8")
            os.makedirs(Path(d, "images"))
            Path(d, "images", "a_01_scn_zi.PNG").write_bytes(b"x")
            res = eb.export_batch(d, opt.ProjectOptions())
            self.assertEqual((3, 4), (res["missing_count"], res["plan_count"]))
            raw = Path(res["path"]).read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            data = json.loads(raw.decode("utf-8"))
            self.assertIn("é".encode("utf-8"), raw)
            self.assertEqual("Style — x", data["master_prompt"])
            self.assertEqual(3, data["count"])
            pl, pu, odd = data["images"]
            self.assertEqual(("3024x1296", "21:9", "PL", "p2 — é"),
                             (pl["canvas"], pl["aspect"], pl["motion"], pl["prompt"]))
            self.assertEqual(("2304x2304", "16:9", ""), (pu["canvas"], pu["aspect"], pu["prompt"]))
            self.assertEqual(("2304x1296", "16:9", "odd"), (odd["canvas"], odd["aspect"], odd["motion"]))
            self.assertTrue(raw.decode().startswith('{\n  "note"'))

    def test_no_shotlist(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                eb.export_batch(d, opt.ProjectOptions())


def _norm(v):
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return v


class GoldenTests(unittest.TestCase):
    def _case(self, name):
        d = FIXTURES / name
        options = opt.load_from_json((d / "imgtovideo.json").read_text(encoding="utf-8-sig"))
        exts = {(e if e.startswith(".") else "." + e).lower() for e in options.naming.image_extensions}
        images = json.loads((d / "images.json").read_text(encoding="utf-8"))
        names = sorted((n for n in images if os.path.splitext(n)[1].lower() in exts),
                       key=parsing.natural_key)
        subs = parsing.parse_srt((d / "subtitles.srt").read_text(encoding="utf-8-sig"))
        issues = []
        doc = sl.parse((d / "shotlist.json").read_text(encoding="utf-8-sig"), issues,
                       options.shotlist.master_prompt_max_chars, options.shotlist.prompt_max_chars)
        manifest = sl.expand(doc, subs, names, options.transitions, issues)
        self.assertIsNotNone(manifest)
        expected = json.loads((d / "visual_manifest.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(_norm(expected), _norm(manifest.to_dict()))
        self.assertFalse([i for i in issues if i.severity == m.ERROR], issues)

    def test_fees(self):
        self._case("fees")

    def test_animals(self):
        self._case("animals")

    def test_more_to_live(self):
        self._case("more_to_live")


if __name__ == "__main__":
    unittest.main()
