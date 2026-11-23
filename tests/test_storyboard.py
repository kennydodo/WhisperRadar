import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_beats import make_cues  # noqa: E402
from whisperradar import beats, storyboard as sb  # noqa: E402

ALLOWED = ["ZI", "ZO", "PU", "PD"]


def row(sw, action="Joe pays at the counter.", who=("Joe",), where="kitchen",
        status="actual", scene=1, motion="ZI"):
    return {"starts_with": sw, "scene": scene, "type": "SCN", "motion": motion,
            "who": list(who), "where": where, "status": status, "action": action}


THREE = [row("You can sit in a", "Joe walks into a restaurant.", where="restaurant"),
         row("But crocodile meat is a", "Joe carries a tray past the counter.", where="restaurant", motion="ZO"),
         row("It is the industry behind", "Joe packs a leather bag.", where="workshop", scene=2, motion="PU")]


def reply(rows):
    return "```json\n" + json.dumps({"rows": rows}) + "\n```"


class ParseTests(unittest.TestCase):
    def test_rows_are_cleaned(self):
        got = sb.parse(reply([
            {"starts_with": "You can sit in a", "who": "Joe, Maya", "status": "Flashback",
             "type": "xyz", "action": "  Joe   walks. "},
            {"starts_with": "", "action": "dropped"},
            {"starts_with": "But crocodile meat", "who": "none", "status": "bogus"}]))
        self.assertEqual(len(got["rows"]), 2)
        a, b = got["rows"]
        self.assertEqual(a["who"], ["Joe", "Maya"])
        self.assertEqual(a["status"], "remembered")        # alias
        self.assertEqual(a["type"], "SCN")
        self.assertEqual(a["action"], "Joe walks.")
        self.assertEqual(b["who"], [])
        self.assertEqual(b["status"], "")                  # unknown -> blank, checked later
        self.assertEqual(b["scene"], 1)

    def test_cut_off_is_not_a_result_and_continuation_joins(self):
        full = reply(THREE)
        cut = full[:full.index('"But crocodile') + 40]
        self.assertIsNone(sb.parse(cut))
        rest = reply(THREE[1:])
        got = sb.parse(sb.join_continuation(cut, rest, "rows", sb.row_ident))
        self.assertEqual([r["starts_with"] for r in got["rows"]],
                         [r["starts_with"] for r in THREE])

    def test_prompts_parse_and_continue(self):
        text = json.dumps({"prompts": [{"n": 1, "prompt": "Joe in a kitchen.", "refs": ["CH_JOE"]},
                                       {"n": "x", "prompt": "bad"}, {"n": 2, "prompt": ""}],
                           "refs": {"CH_JOE": None}, "refPrompts": {"CH_JOE": "a man"}})
        got, meta = sb.parse_prompts(text)
        self.assertEqual(got, {1: {"prompt": "Joe in a kitchen.", "refs": ["CH_JOE"]}})
        self.assertEqual(meta["refs"], {"CH_JOE": None})
        cut = '{"prompts": [{"n": 1, "prompt": "a"}, {"n": 2, "prompt": "b'
        self.assertEqual(sb.parse_prompts(cut), ({}, {}))
        joined = sb.join_continuation(cut[:cut.index('{"n": 2')] + '{"n": 2, "prompt": "b',
                                      '{"prompts": [{"n": 2, "prompt": "bb"}, {"n": 3, "prompt": "c"}]}',
                                      "prompts", sb.prompt_ident)
        self.assertEqual(sorted(sb.parse_prompts(joined)[0]), [1, 2, 3])


class CheckTests(unittest.TestCase):
    def check(self, rows, **kw):
        return sb.check_rows(rows, make_cues(4.0), ALLOWED, 12, 0, **kw)

    def test_clean_storyboard_has_no_faults(self):
        _, ranges, faults, warns = self.check([dict(r) for r in THREE])
        self.assertEqual(faults, [])
        self.assertEqual(ranges, [(1, 2), (3, 4), (5, 6)])

    def test_missing_action_and_status_are_faults(self):
        rows = [dict(r) for r in THREE]
        rows[1]["action"], rows[2]["status"] = "", ""
        faults = self.check(rows)[2]
        self.assertTrue(any(f.startswith("row 2:") and "action" in f for f in faults))
        self.assertTrue(any(f.startswith("row 3:") and "status" in f for f in faults))

    def test_cue_problems_come_from_the_beats_checks(self):
        rows = [dict(r) for r in THREE]
        rows[1]["starts_with"] = "never said anywhere at all"
        self.assertTrue(any("not found" in f for f in self.check(rows)[2]))

    def test_unknown_character_only_when_names_are_given(self):
        rows = [dict(r) for r in THREE]
        rows[0]["who"] = ["Maya"]
        self.assertEqual(self.check(rows)[2], [])
        faults = self.check(rows, known_names=["Joe"])[2]
        self.assertTrue(any("unknown character Maya" in f for f in faults))

    def test_warnings_repeat_posture_and_missing_place(self):
        rows = [dict(r) for r in THREE]
        rows[1]["action"] = rows[0]["action"]
        warns = self.check(rows)[3]
        self.assertTrue(any("same action as the previous row" in w for w in warns))
        seated = [row("You can sit in a", f"Joe sits at desk {i}.") for i in range(6)]
        cues = [{"index": i + 1, "start": f"00:00:{i * 4:02d},000", "end": f"00:00:{i * 4 + 4:02d},000",
                 "text": t} for i, t in enumerate(
                     ["alpha beta gamma delta", "epsilon zeta eta theta", "iota kappa lambda mu",
                      "nu xi omicron pi", "rho sigma tau upsilon", "phi chi psi omega"])]
        seated = [row(c["text"], f"Joe sits at desk {i}.") for i, c in enumerate(cues)]
        w = sb.check_rows(seated, cues, ALLOWED, 12, 0)[3]
        self.assertTrue(any("sitting actions in a row" in x for x in w))
        clinic = [row("alpha beta gamma delta", "Joe walks.", where="street")]
        cues2 = [dict(cues[0], text="alpha beta gamma delta at the clinic")]
        w2 = sb.check_rows(clinic, cues2, ALLOWED, 12, 0)[3]
        self.assertTrue(any("names clinic" in x for x in w2))


class EditAndFileTests(unittest.TestCase):
    def test_apply_edits_changes_only_posted_cells(self):
        rows = [dict(r, cues="1-2") for r in THREE]
        new = sb.apply_edits(rows, {"action_1": " Joe jogs. ", "who_2": "Joe, Maya",
                                    "status_3": "Planned", "type_3": "zzz", "scene_2": "x"})
        self.assertEqual(new[0]["action"], "Joe jogs.")
        self.assertEqual(new[1]["who"], ["Joe", "Maya"])
        self.assertEqual(new[1]["scene"], 1)                 # bad number ignored
        self.assertEqual(new[2]["status"], "planned")
        self.assertEqual(new[2]["type"], "SCN")
        self.assertEqual(new[0]["where"], "restaurant")      # untouched
        self.assertEqual(rows[0]["action"], "Joe walks into a restaurant.")   # input not mutated

    def test_save_load_and_out_of_date(self):
        pdir = Path(tempfile.mkdtemp())
        rows = [dict(r, asset=f"A{i}.png") for i, r in enumerate(THREE)]
        compiled = {r["asset"]: sb.row_hash(r) for r in rows}
        sb.save(pdir, rows, [(1, 2), (3, 4), (5, 6)], "style", approved=True, compiled=compiled)
        data = sb.load(pdir)
        self.assertTrue(data["approved"])
        self.assertEqual([r["cues"] for r in data["rows"]], ["1-2", "3-4", "5-6"])
        self.assertEqual(sb.out_of_date(data), [])
        data["rows"][1]["action"] = "Joe sprints."
        self.assertEqual(sb.out_of_date(data), ["A1.png"])
        (pdir / sb.FILE).write_text("{broken")
        self.assertIsNone(sb.load(pdir))

    def test_note_names_who_where_status(self):
        self.assertEqual(sb.note_for(THREE[0]), "Joe, restaurant, actual")
        self.assertEqual(sb.note_for(row("x", who=())), "nobody, kitchen, actual")

    def test_consistency_faults(self):
        names = ["A.png", "B.png", "C.png"]
        prompts = {1: {"prompt": "Joe walks into a busy restaurant."},
                   2: {"prompt": "A man carries a tray."}}
        faults = sb.consistency_faults(THREE, names, prompts)
        self.assertFalse(any(f.startswith("A.png") for f in faults))
        self.assertTrue(any(f.startswith("B.png") and "must name them" in f for f in faults))
        self.assertTrue(any(f.startswith("C.png") and "no prompt" in f for f in faults))
        prompts[2] = {"prompt": "Joe in a hall."}
        self.assertTrue(any(f.startswith("B.png") and "approved place" in f
                            for f in sb.consistency_faults(THREE, names, prompts)))


class ShotlistFieldsTests(unittest.TestCase):
    def test_build_shotlist_carries_action_and_note_only_when_set(self):
        b = dict(THREE[0], prompt="Joe in a restaurant.", note="Joe, restaurant, actual")
        data, _ = beats.build_shotlist([b], [(1, 6)], "style", None, make_cues())
        self.assertEqual(data["shots"][0]["action"], b["action"])
        self.assertEqual(data["shots"][0]["note"], "Joe, restaurant, actual")
        plain = {"starts_with": "x", "scene": 1, "type": "SCN", "motion": "ZI", "prompt": "p"}
        data2, _ = beats.build_shotlist([plain], [(1, 6)], "style", None, make_cues())
        self.assertNotIn("action", data2["shots"][0])
        self.assertNotIn("note", data2["shots"][0])


class PromptWordingTests(unittest.TestCase):
    def test_prompts_have_no_word_target_but_state_the_character_budget(self):
        rows = [dict(r) for r in THREE]
        txt = sb.prompts_prompt(rows, [(1, 2), (3, 4), (5, 6)], make_cues(4.0), "S" * 100, "")
        self.assertNotIn("40-90", txt)
        self.assertIn("2420 characters", txt)
        self.assertIn("at most 2319 characters", txt)

    def test_remembered_is_shown_as_having_happened(self):
        rows = [dict(r) for r in THREE]
        txt = sb.prompts_prompt(rows, [(1, 2), (3, 4), (5, 6)], make_cues(4.0), "", "")
        self.assertNotIn("never as a done event", txt)
        self.assertIn("`remembered` as something that happened at an earlier time", txt)

    def test_near_identical_adjacent_actions_get_an_advisory_only(self):
        rows = [dict(r) for r in THREE]
        rows[0]["action"] = "Joe reviews a portfolio balance of twenty one thousand dollars on screen"
        rows[1]["action"] = "Joe reviews a portfolio balance of twenty one thousand dollars on paper"
        _, _, faults, warns = sb.check_rows(rows, make_cues(4.0), ALLOWED, 12.0, 0)
        self.assertTrue(any("nearly the same wording" in w for w in warns))


class ShotIdAndMapTests(unittest.TestCase):
    ROWS = [
        {"scene": 1, "who": ["Joe"], "where": "kitchen"},
        {"scene": 1, "who": ["Joe", "Joe's parents"], "where": "kitchen"},
        {"scene": 2, "who": ["Clinic receptionist"], "where": "clinic desk"},
    ]

    def test_row_ids_use_scene_and_position_in_scene(self):
        self.assertEqual(sb.row_ids(self.ROWS), ["S01_01", "S01_02", "S02_01"])
        self.assertEqual(sb.relabel("row 2: x and beat 3 and row 9", sb.row_ids(self.ROWS)),
                         "S01_02: x and S02_01 and row 9")

    def test_default_character_refs_split_family_and_keep_supplied(self):
        self.assertEqual(sb.default_character_refs("Joe's parents"),
                         ["CH_JOE_FATHER", "CH_JOE_MOTHER"])
        self.assertEqual(sb.default_character_refs("Joe's younger brother"),
                         ["CH_JOE_YOUNGER_BROTHER"])
        self.assertEqual(sb.default_character_refs("Joe", ["CH_JOE"]), ["CH_JOE"])

    def test_sync_keeps_the_reviewed_entries_and_adds_new_names(self):
        cur = {"Joe": ["CH_JOE", "CH_JOE_OLDER"], "Gone": ["CH_X"]}
        out = sb.sync_characters(self.ROWS, cur)
        self.assertEqual(out["Joe"], ["CH_JOE", "CH_JOE_OLDER"])
        self.assertNotIn("Gone", out)
        self.assertEqual(out["Clinic receptionist"], ["CH_CLINIC_RECEPTIONIST"])
        rows = [{"where": "bungalow kitchen"}] * sb.MIN_PLACE_ROWS + [{"where": "cafe"}]
        loc = sb.sync_locations(rows, {"cafe": "BG_CAFE"})
        self.assertEqual(loc["bungalow kitchen"], "BG_BUNGALOW_KITCHEN")
        self.assertEqual(loc["cafe"], "BG_CAFE")

    def test_expected_refs_are_the_people_then_the_place(self):
        chars = {"Joe": ["CH_JOE"], "Joe's parents": ["CH_JOE_FATHER", "CH_JOE_MOTHER"]}
        self.assertEqual(sb.expected_refs(self.ROWS[1], chars, {"kitchen": "BG_KITCHEN"}),
                         ["CH_JOE", "CH_JOE_FATHER", "CH_JOE_MOTHER", "BG_KITCHEN"])

    def test_apply_maps_tidies_names(self):
        chars, locs = sb.apply_maps({"char_name_1": "Joe", "char_refs_1": "ch_joe, joe_older",
                                     "loc_where_1": "kitchen", "loc_id_1": "bungalow kitchen"})
        self.assertEqual(chars, {"Joe": ["CH_JOE", "CH_JOE_OLDER"]})
        self.assertEqual(locs, {"kitchen": "BG_BUNGALOW_KITCHEN"})
        self.assertEqual(sb.apply_maps({}), (None, None))

    def test_name_check_accepts_role_words_in_any_case(self):
        rows = [{"who": ["Joe's parents"], "where": "kitchen"},
                {"who": ["Clinic receptionist"], "where": "clinic desk"}]
        chars = {"Joe's parents": ["CH_JOE_FATHER", "CH_JOE_MOTHER"],
                 "Clinic receptionist": ["CH_CLINIC_RECEPTIONIST"]}
        prompts = {1: {"prompt": "In the kitchen his father and mother sit."},
                   2: {"prompt": "At the clinic desk the RECEPTIONIST types."}}
        self.assertEqual(sb.consistency_faults(rows, ["A", "B"], prompts, chars), [])
        prompts[1] = {"prompt": "In the kitchen a neighbour waits."}
        self.assertEqual(len(sb.consistency_faults(rows, ["A", "B"], prompts, chars)), 1)

    def test_ref_check_follows_who_is_pictured_not_who_is_named(self):
        from whisperradar import studio
        base = {
            "style": "s", "refs": {"CH_JOE": "refs/CH_JOE.png", "CH_SAM": None,
                                   "BG_KITCHEN": None},
            "refPrompts": {"CH_SAM": "Sam alone", "BG_KITCHEN": "empty kitchen"},
            "characters": {"Joe": ["CH_JOE"], "Sam": ["CH_SAM"]},
            "locations": {"Sam and Beth's kitchen": "BG_KITCHEN"},
            "shots": [{"cues": "1", "asset": "S01_01_SCN_ZO.png", "scene": "S01",
                       "motion": "ZO", "who": ["Joe"], "where": "Sam and Beth's kitchen"}],
            "images": [{"file": "S01_01_SCN_ZO.png",
                        "prompt": "Joe phones Sam and invites Beth in Sam and Beth's kitchen",
                        "refs": ["CH_JOE", "BG_KITCHEN"]}],
        }

        def mixups(d):
            return [f for f in studio.shotlist_structural_faults(d, 1)
                    if "reviewed references" in f or "name a character" in f]
        self.assertEqual(mixups(base), [])
        base["images"][0]["refs"] = ["CH_JOE"]
        self.assertTrue(any("missing BG_KITCHEN" in f for f in mixups(base)))
        base["images"][0]["refs"] = ["CH_JOE", "BG_KITCHEN", "CH_SAM"]
        self.assertTrue(any("CH_SAM is not in this shot" in f for f in mixups(base)))


class RunTests(unittest.TestCase):
    def setup(self, replies):
        from whisperradar import briefs
        from whisperradar import webstages as ws
        pdir = Path(tempfile.mkdtemp())
        prof = briefs.resolve_profile("custom", custom=json.dumps({"allowed": ALLOWED}))
        plan = {"cues": make_cues(4.0), "profile": prof, "max_hold": 12.0, "total_s": 24.0,
                "allow_refs": False, "min_align": .8}
        ctx = {"pdir": pdir, "visual_style": "Flat doodles", "bible": ""}
        asked, saved = [], []

        class T:
            def ask(self, site, text, files, new_chat=True, ready=None):
                asked.append(text)
                return replies.pop(0)

        for p in (mock.patch.object(ws.ep, "_context", return_value=ctx),
                  mock.patch.object(ws.ep, "_plan_inputs", return_value=plan),
                  mock.patch.object(ws.ep, "planning_context_prompt", return_value="CHANNEL TEST RULES"),
                  mock.patch.object(ws.ep, "shotlist_judge_prompt", return_value=("", [])),
                  mock.patch.object(ws, "_dump"),
                  mock.patch.object(ws, "save_shotlist",
                                    side_effect=lambda cfg, pid, data, detail, advance: saved.append(data))):
            p.start()
            self.addCleanup(p.stop)
        return ws, pdir, asked, saved, T()

    def test_pass_one_saves_an_unapproved_draft_and_never_writes_a_shotlist(self):
        ws, pdir, asked, saved, t = self.setup([reply(THREE)])
        data = ws.run_storyboard(None, 1, t, "zai", log=lambda m: None)
        self.assertEqual(len(asked), 1)
        self.assertIn("STORYBOARD ADAPTER", asked[0])
        self.assertIn("You do NOT write image prompts", asked[0])
        self.assertFalse(data["approved"])
        self.assertEqual([r["cues"] for r in sb.load(pdir)["rows"]], ["1-2", "3-4", "5-6"])
        self.assertEqual(saved, [])
        self.assertFalse((pdir / "shotlist.json").exists())

    def test_pass_one_asks_once_for_a_correction_and_keeps_faults_visible(self):
        bad = [dict(r) for r in THREE]
        bad[1]["action"] = ""
        ws, pdir, asked, saved, t = self.setup([reply(bad), reply(THREE)])
        data = ws.run_storyboard(None, 1, t, "zai", log=lambda m: None)
        self.assertEqual(len(asked), 2)
        self.assertEqual(data["faults"], [])
        ws2, pdir2, asked2, _s, t2 = self.setup([reply(bad)] + ["no idea"] * 14)
        data2 = ws2.run_storyboard(None, 1, t2, "zai", log=lambda m: None)
        self.assertTrue(any("action" in f for f in data2["faults"]))   # draft kept for the page

    def approved(self, pdir):
        rows = [dict(r) for r in THREE]
        sb.save(pdir, rows, [(1, 2), (3, 4), (5, 6)], "Flat doodles", approved=True)

    def prompts(self, items):
        return "```json\n" + json.dumps({"prompts": items}) + "\n```"

    def test_pass_two_stops_before_the_writer_when_the_maps_exceed_the_reference_cap(self):
        ws, pdir, asked, saved, t = self.setup([])
        ws.ep._plan_inputs()["allow_refs"] = True
        from whisperradar import studio as _st
        per_row = _st.REFS_ON_THE_FLY_CAP // len(THREE) + 1
        rows = [dict(r, who=[f"Person{i}x{n}" for i in range(per_row)]) for n, r in enumerate(THREE)]
        sb.save(pdir, rows, [(1, 2), (3, 4), (5, 6)], "s", approved=True)
        with self.assertRaises(ws.StageFailed) as cm:
            ws.run_storyboard_prompts(None, 1, t, "zai", log=lambda m: None)
        self.assertIn(f"the cap is {_st.REFS_ON_THE_FLY_CAP}", str(cm.exception))
        self.assertEqual(asked, [])

    def test_pass_two_refuses_an_unapproved_storyboard(self):
        ws, pdir, asked, saved, t = self.setup([])
        sb.save(pdir, [dict(r) for r in THREE], [(1, 2), (3, 4), (5, 6)], "s", approved=False)
        with self.assertRaises(ws.StageFailed):
            ws.run_storyboard_prompts(None, 1, t, "zai", log=lambda m: None)
        self.assertEqual(asked, [])

    def test_pass_two_writes_the_shotlist_with_action_and_note(self):
        ok = self.prompts([
            {"n": 1, "prompt": "Joe walks into a restaurant."},
            {"n": 2, "prompt": "Joe carries a tray past the restaurant counter."},
            {"n": 3, "prompt": "Joe packs a leather bag in the workshop."}])
        ws, pdir, asked, saved, t = self.setup([ok])
        self.approved(pdir)
        ws.run_storyboard_prompts(None, 1, t, "zai", log=lambda m: None)
        self.assertEqual(len(asked), 1)
        self.assertIn("PROMPT ADAPTER", asked[0])
        self.assertEqual(len(saved), 1)
        shots = saved[0]["shots"]
        self.assertEqual([s["cues"] for s in shots], ["1-2", "3-4", "5-6"])
        self.assertEqual(shots[0]["action"], THREE[0]["action"])
        self.assertEqual(shots[0]["note"], "Joe, restaurant, actual")
        after = sb.load(pdir)
        self.assertEqual(sb.out_of_date(after), [])
        self.assertTrue(after["approved"])
        self.assertTrue(all(r["asset"] for r in after["rows"]))

    def test_pass_two_sends_only_the_faulty_rows_back_and_keeps_the_casting(self):
        first = self.prompts([
            {"n": 1, "prompt": "Joe walks into a restaurant."},
            {"n": 2, "prompt": "A man carries a tray."},                  # Joe missing
            {"n": 3, "prompt": "Joe packs a leather bag in the workshop."}])
        fix = self.prompts([{"n": 2, "prompt": "Joe carries a tray past the restaurant counter."}])
        ws, pdir, asked, saved, t = self.setup([first, fix])
        self.approved(pdir)
        ws.run_storyboard_prompts(None, 1, t, "zai", log=lambda m: None)
        self.assertEqual(len(asked), 2)
        self.assertIn("Fix ONLY these rows", asked[1])
        self.assertIn('"n": 2', asked[1])
        self.assertNotIn('"n": 1', asked[1])
        self.assertIn("Joe", saved[0]["images"][1]["prompt"])

    def test_pass_two_that_never_passes_keeps_the_old_shotlist(self):
        bad = self.prompts([{"n": 1, "prompt": "A man."}])
        ws, pdir, asked, saved, t = self.setup([bad, bad, bad, bad, bad, bad, bad, bad])
        self.approved(pdir)
        (pdir / "shotlist.json").write_text('{"old": true}')
        with self.assertRaises(ws.StageFailed):
            ws.run_storyboard_prompts(None, 1, t, "zai", log=lambda m: None)
        self.assertEqual(saved, [])
        self.assertEqual((pdir / "shotlist.json").read_text(), '{"old": true}')


class RouteTests(unittest.TestCase):
    def setUp(self):
        from tests import wr_tmp
        from whisperradar import briefs, db, studio
        from whisperradar import webstages as ws
        from whisperradar.config import load_config
        from whisperradar.webapp import create_app
        self._wr_tmp = wr_tmp
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(__file__).resolve().parents[1]
        self.cfg = load_config(root / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.pid = db.create_production(conn, "SB Title", "general", None, None)
        db.update_production(conn, self.pid, stage="shots")
        conn.commit()
        conn.close()
        self.pdir = studio.prod_dir(self.cfg, self.pid)
        self.pdir.mkdir(parents=True, exist_ok=True)
        prof = briefs.resolve_profile("custom", custom=json.dumps({"allowed": ALLOWED}))
        self.plan = {"cues": make_cues(4.0), "profile": prof}
        p = mock.patch.object(ws, "_storyboard_inputs",
                              return_value=({}, self.plan, prof, 12.0, 0, ALLOWED))
        p.start()
        self.addCleanup(p.stop)
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self._wr_tmp.cleanup(self.tmp)

    def seed(self, approved=False, rows=None):
        rows = rows or [dict(r) for r in THREE]
        sb.save(self.pdir, rows, [(1, 2), (3, 4), (5, 6)], "style", approved=approved)

    def form(self, **over):
        f = {}
        for n, r in enumerate(sb.load(self.pdir)["rows"], 1):
            for k in sb.EDITABLE:
                f[f"{k}_{n}"] = ", ".join(r[k]) if k == "who" else str(r[k])
        f.update(over)
        return f

    def test_stage_box_shows_state_and_review_link(self):
        page = self.client.get(f"/studio/{self.pid}?stage=shots").data
        self.assertIn(b"Write the storyboard in web chat", page)
        self.assertNotIn(b"Review and edit the storyboard", page)
        self.assertNotIn(b"Write prompts from the approved storyboard", page)
        self.seed()
        page = self.client.get(f"/studio/{self.pid}?stage=shots").data
        self.assertIn(b"not approved", page)
        self.assertIn(b"Review and edit the storyboard", page)
        self.assertNotIn(b"Write prompts from the approved storyboard", page)
        self.seed(approved=True)
        page = self.client.get(f"/studio/{self.pid}?stage=shots").data
        self.assertIn(b"rows approved", page)
        self.assertIn(b"Write prompts from the approved storyboard", page)

    def test_stage_box_also_shows_in_simple_mode(self):
        from whisperradar import db
        conn = db.connect(self.cfg.db_path)
        db.set_setting(conn, "simple_mode", "1")
        conn.close()
        page = self.client.get(f"/studio/{self.pid}?stage=shots").data
        self.assertIn(b"Write the storyboard in web chat", page)
        self.assertNotIn(b'data-key="elements"', page)
        self.seed()
        page = self.client.get(f"/studio/{self.pid}?stage=shots").data
        self.assertIn(b"Review and edit the storyboard", page)

    def test_edit_page_needs_a_storyboard_then_shows_rows(self):
        r = self.client.get(f"/studio/{self.pid}/storyboard/edit", follow_redirects=True)
        self.assertIn(b"Create the storyboard first", r.data)
        self.seed()
        r = self.client.get(f"/studio/{self.pid}/storyboard/edit")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"SB Title", r.data)
        self.assertIn(b"name='action_3'", r.data)

    def test_save_rechecks_and_clears_approval(self):
        self.seed(approved=True)
        r = self.client.post(f"/studio/{self.pid}/storyboard/save",
                             data=self.form(action_2="Joe jogs to the counter."))
        self.assertIn(r.status_code, (302, 303))
        data = sb.load(self.pdir)
        self.assertFalse(data["approved"])
        self.assertEqual(data["rows"][1]["action"], "Joe jogs to the counter.")
        self.assertEqual(data["faults"], [])

    def test_approve_is_refused_while_faults_remain(self):
        self.seed()
        r = self.client.post(f"/studio/{self.pid}/storyboard/approve",
                             data=self.form(action_1="", status_2=""), follow_redirects=True)
        self.assertIn(b"Not approved", r.data)
        data = sb.load(self.pdir)
        self.assertFalse(data["approved"])
        self.assertTrue(any("action" in f for f in data["faults"]))
        self.assertTrue(any("status" in f for f in data["faults"]))

    def test_approve_a_clean_storyboard(self):
        self.seed()
        r = self.client.post(f"/studio/{self.pid}/storyboard/approve", data=self.form(),
                             follow_redirects=True)
        self.assertIn(b"Approved. You can write the prompts now.", r.data)
        self.assertTrue(sb.load(self.pdir)["approved"])

    def test_prompts_run_is_refused_until_approved(self):
        self.seed()
        r = self.client.post(f"/studio/{self.pid}/webchat/storyboard-prompts",
                             data={"writer": "zai"}, follow_redirects=True)
        self.assertIn(b"Approve the storyboard first", r.data)

    def test_shotlist_button_needs_an_approved_storyboard_when_one_exists(self):
        self.seed()
        for stage in ("shots", "beats"):
            r = self.client.post(f"/studio/{self.pid}/webchat/{stage}",
                                 data={"writer": "zai", "judge": "zai"}, follow_redirects=True)
            self.assertIn(b"not approved yet", r.data)

    def test_shotlist_button_writes_from_the_approved_storyboard(self):
        from whisperradar import webstages as ws
        self.seed(approved=True)
        calls = []
        with mock.patch.object(ws, "storyboard_prompts_job",
                               side_effect=lambda *a, **k: calls.append("sb")), \
             mock.patch.object(ws, "shotlist_job",
                               side_effect=lambda *a, **k: calls.append("old")):
            r = self.client.post(f"/studio/{self.pid}/webchat/shots",
                                 data={"writer": "zai", "judge": "zai"}, follow_redirects=True)
            self.assertIn(b"written from your approved storyboard", r.data)
            import time
            for _ in range(50):
                if calls:
                    break
                time.sleep(0.1)
        self.assertEqual(calls, ["sb"])

    def test_ignore_storyboard_runs_the_old_way(self):
        from whisperradar import webstages as ws
        self.seed()
        calls = []
        with mock.patch.object(ws, "shotlist_job",
                               side_effect=lambda *a, **k: calls.append("old")):
            r = self.client.post(f"/studio/{self.pid}/webchat/shots",
                                 data={"writer": "zai", "judge": "zai",
                                       "ignore_storyboard": "on"}, follow_redirects=True)
            self.assertNotIn(b"not approved yet", r.data)
            import time
            for _ in range(50):
                if calls:
                    break
                time.sleep(0.1)
        self.assertEqual(calls, ["old"])

    def test_review_page_shows_shot_ids_and_the_two_maps_and_saves_them(self):
        self.seed()
        page = self.client.get(f"/studio/{self.pid}/storyboard/edit").data
        self.assertIn(b"S01_01", page)
        self.assertIn(b"Characters and their references", page)
        self.assertIn(b"Places and their background references", page)
        f = self.form()
        f.update({"char_name_1": "Joe", "char_refs_1": "CH_JOE, CH_JOE_OLDER",
                  "loc_where_1": "kitchen", "loc_id_1": "BG_KITCHEN"})
        self.client.post(f"/studio/{self.pid}/storyboard/save", data=f)
        saved = sb.load(self.pdir)
        self.assertEqual(saved["characters"]["Joe"], ["CH_JOE", "CH_JOE_OLDER"])

    def test_review_page_counts_new_references_against_the_cap(self):
        self.seed()
        page = self.client.get(f"/studio/{self.pid}/storyboard/edit").data
        from whisperradar import studio as _st
        self.assertIn(f"new references, the cap is {_st.REFS_ON_THE_FLY_CAP}".encode(), page)
        per_row = _st.REFS_ON_THE_FLY_CAP // len(THREE) + 1
        rows = [dict(r, who=[f"P{i}x{n}" for i in range(per_row)]) for n, r in enumerate(THREE)]
        sb.save(self.pdir, rows, [(1, 2), (3, 4), (5, 6)], "s")
        page = self.client.get(f"/studio/{self.pid}/storyboard/edit").data
        self.assertIn(b"remove ", page)

    def test_edit_after_prompts_marks_the_shot_out_of_date(self):
        rows = [dict(r, asset=f"A{i}.png") for i, r in enumerate(THREE)]
        sb.save(self.pdir, rows, [(1, 2), (3, 4), (5, 6)], "s", approved=True,
                compiled={r["asset"]: sb.row_hash(r) for r in rows})
        self.client.post(f"/studio/{self.pid}/storyboard/save",
                         data=self.form(action_2="Joe sprints."))
        page = self.client.get(f"/studio/{self.pid}/storyboard/edit").data
        self.assertIn(b"changed after its prompt was written", page)
        stage = self.client.get(f"/studio/{self.pid}?stage=shots").data
        self.assertIn(b"changed after their prompt", stage)


if __name__ == "__main__":
    unittest.main()
