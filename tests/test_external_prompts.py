"""External-LLM prompt builder: copy-paste prompts for the script and shotlist
stages, built from the same prompt functions and channel settings as the
built-in stages. Nothing here may call an LLM."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import db, external_prompts as ep, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402

SRT = """1
00:00:00,000 --> 00:00:06,000
Welcome to the video about old coins.

2
00:00:06,000 --> 00:00:14,000
A jar of pennies can hide something valuable.

3
00:00:14,000 --> 00:00:30,000
Check the year on every one of them before you spend them.
"""
SOURCE = ("Reference narration about hidden value in coin jars. " * 30)
NOTES = "- pennies from 1943 can be steel\n- check the date and mint mark"
WSTYLE = "## Voice & Tone\nCalm, direct, second person."
BIBLE = "MAYA - a woman in a mustard scarf."
VSTYLE = "Flat vector illustration, no text in images."


class Base(unittest.TestCase):
    channel_fields: dict = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.chan = db.create_own_channel(conn, "Ch", style=VSTYLE,
                                          bible=BIBLE, **self.channel_fields)
        self.pid = db.create_production(conn, "Coin Jar Secrets", "finance",
                                        None, None)
        db.update_production(conn, self.pid, own_channel_id=self.chan)
        conn.commit()
        conn.close()
        self.pdir = studio.prod_dir(self.cfg, self.pid)
        self.pdir.mkdir(parents=True, exist_ok=True)
        (self.pdir / "source_transcript.txt").write_text(SOURCE, "utf-8")
        (self.pdir / "writing_style.md").write_text(WSTYLE, "utf-8")
        (self.pdir / "research_notes.md").write_text(NOTES, "utf-8")
        (self.pdir / "subtitles.srt").write_text(SRT, "utf-8")

    def tearDown(self):
        self.tmp.cleanup()


class ScriptPromptTests(Base):
    def test_writer_prompt_carries_title_style_notes_and_bar(self):
        text = ep.script_writer_prompt(self.cfg, self.pid, title="My Own Title",
                                       target_words=900)
        self.assertIn('titled "My Own Title"', text)
        self.assertIn(WSTYLE, text)
        self.assertIn("pennies from 1943", text)
        self.assertIn("About 900 words", text)
        self.assertIn("QUALITY BAR", text)
        self.assertIn("plain text", text)

    def test_writer_prompt_never_includes_the_reference_script(self):
        text = ep.script_writer_prompt(self.cfg, self.pid)
        self.assertNotIn("Reference narration about hidden value", text)

    def test_writer_prompt_has_no_visual_style_or_bible(self):
        text = ep.script_writer_prompt(self.cfg, self.pid)
        self.assertNotIn(VSTYLE, text)
        self.assertNotIn(BIBLE, text)

    def test_default_title_is_the_production_title(self):
        self.assertIn('titled "Coin Jar Secrets"',
                      ep.script_writer_prompt(self.cfg, self.pid))

    def test_pasted_notes_and_style_override_the_files(self):
        text = ep.script_writer_prompt(self.cfg, self.pid, style_guide="STYLE-X",
                                       notes="- NOTE-Y")
        self.assertIn("STYLE-X", text)
        self.assertIn("NOTE-Y", text)
        self.assertNotIn(WSTYLE, text)

    def test_production_direction_reaches_the_prompt(self):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.update_production(conn, self.pid, stage_extras=json.dumps(
            {"script": "Open with a question."}))
        conn.commit()
        conn.close()
        self.assertIn("Open with a question.",
                      ep.script_writer_prompt(self.cfg, self.pid))

    def test_channel_pass_bar_reaches_both_script_prompts(self):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.update_own_channel(conn, self.chan, script_min_rating=8.3,
                              script_max_overlap=0.07)
        conn.commit()
        conn.close()
        writer = ep.script_writer_prompt(self.cfg, self.pid)
        judge = ep.script_judge_prompt(self.cfg, self.pid, "A script.")
        for text in (writer, judge):
            self.assertIn("8.3", text)
            self.assertIn("7%", text)

    def test_judge_prompt_has_script_overlap_and_channel_bar(self):
        script = SOURCE[:300]   # lifted straight from the source
        text = ep.script_judge_prompt(self.cfg, self.pid, script)
        self.assertIn(script, text)
        self.assertIn(WSTYLE, text)
        self.assertIn("5-gram overlap with the source transcript", text)
        self.assertIn("THE CHANNEL'S BAR", text)
        self.assertNotIn(BIBLE, text)

    def test_judge_prompt_needs_a_script(self):
        with self.assertRaises(ep.PromptError):
            ep.script_judge_prompt(self.cfg, self.pid, "   ")

    def test_style_and_notes_prompts_use_the_reference_script(self):
        self.assertIn("Reference narration about hidden value",
                      ep.style_extraction_prompt(self.cfg, self.pid))
        self.assertIn("Reference narration about hidden value",
                      ep.notes_extraction_prompt(self.cfg, self.pid))

    def test_style_prompt_without_a_reference_is_a_clear_error(self):
        (self.pdir / "source_transcript.txt").unlink()
        with self.assertRaises(ep.PromptError):
            ep.style_extraction_prompt(self.cfg, self.pid)


class ShotlistPromptTests(Base):
    channel_fields = {"brief_motion": "static",
                      "brief_presentation": "MAYA hosts on screen throughout."}

    def test_planner_prompt_has_brief_profile_inputs_and_output_rules(self):
        text = ep.shotlist_planner_prompt(self.cfg, self.pid)
        self.assertIn("MAYA hosts on screen throughout.", text)   # presentation
        self.assertIn(VSTYLE, text)                               # visual style
        self.assertIn(BIBLE, text)                                # bible
        self.assertIn("1: Welcome to the video about old coins.", text)
        self.assertIn("PACING MATH FOR THIS CHANNEL", text)
        self.assertIn("OUTPUT RULES FOR THIS CHAT", text)
        self.assertIn("```json", text)
        self.assertIn("continue", text)

    def test_planner_prompt_has_no_writing_style(self):
        self.assertNotIn(WSTYLE, ep.shotlist_planner_prompt(self.cfg, self.pid))

    def test_planner_prompt_needs_subtitles(self):
        (self.pdir / "subtitles.srt").unlink()
        with self.assertRaises(ep.PromptError):
            ep.shotlist_planner_prompt(self.cfg, self.pid)

    def test_no_bible_says_so_instead_of_triggering_the_gate(self):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.update_own_channel(conn, self.chan, bible=None)
        conn.commit()
        conn.close()
        text = ep.shotlist_planner_prompt(self.cfg, self.pid)
        self.assertIn("this channel has none", text)

    def _plan(self):
        return json.dumps({
            "style": "x",
            "images": [{"file": "S01_01_SCN_ST.png", "prompt": "a jar of coins"},
                       {"file": "S01_02_SCN_ST.png", "prompt": "a hand counting"}],
            "shots": [{"asset": "S01_01_SCN_ST.png", "cues": "1-2",
                       "scene": "S01", "motion": "ST"},
                      {"asset": "S01_02_SCN_ST.png", "cues": "3",
                       "scene": "S01", "motion": "ST"}]})

    def test_judge_prompt_states_the_channel_rules_and_has_the_plan(self):
        text, local = ep.shotlist_judge_prompt(self.cfg, self.pid, self._plan())
        self.assertIn("PART A - HARD RULES", text)
        self.assertIn("MAXIMUM HOLD", text)
        self.assertIn("only ST is allowed", text)   # the static profile
        self.assertIn("1: Welcome to the video about old coins.", text)
        self.assertIn("a jar of coins", text)
        self.assertIn("PART B", text)
        self.assertIn('"detailed_ratio"', text)
        # the completeness instructions are reused, but its own reply format is not
        self.assertNotIn('{"shots": [{"asset": "<asset>", "verdict": "ok"', text)
        self.assertNotIn(WSTYLE, text)
        self.assertIsInstance(local, list)

    def test_judge_prompt_reports_the_apps_local_faults(self):
        plan = json.loads(self._plan())
        plan["shots"].pop()   # cue 3 uncovered
        _text, local = ep.shotlist_judge_prompt(self.cfg, self.pid,
                                                json.dumps(plan))
        self.assertTrue(any("no shot" in f for f in local), local)

    def test_judge_prompt_rejects_junk(self):
        with self.assertRaises(ep.PromptError):
            ep.shotlist_judge_prompt(self.cfg, self.pid, "not json")
        with self.assertRaises(ep.PromptError):
            ep.shotlist_judge_prompt(self.cfg, self.pid, "")

    def test_channel_hold_range_reaches_the_judge_rules(self):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.update_own_channel(conn, self.chan, brief_motion="long_holds",
                              brief_min_hold=10, brief_max_hold=30)
        conn.commit()
        conn.close()
        text, _ = ep.shotlist_judge_prompt(self.cfg, self.pid, self._plan())
        self.assertIn("no shot holds longer than 30s", text)
        self.assertIn("MINIMUM HOLD", text)
        self.assertIn("10-30s", text)

    def test_alignment_reply_marker_still_exists(self):
        # the judge prompt reuses alignment_prompt minus its reply format; if
        # that wording changes the split must be revisited
        self.assertIn(ep._ALIGN_REPLY_MARK, studio.alignment_prompt([], {}))

    def test_references_off_rule_is_stated(self):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.update_own_channel(conn, self.chan, generate_references=0)
        conn.commit()
        conn.close()
        text, _ = ep.shotlist_judge_prompt(self.cfg, self.pid, self._plan())
        self.assertIn("REFERENCES ARE OFF", text)


class RouteTests(Base):
    def setUp(self):
        super().setUp()
        self.client = create_app(self.cfg).test_client()

    def _post(self, **data):
        return self.client.post(f"/studio/{self.pid}/external-prompt", data=data)

    def test_never_calls_an_llm(self):
        with mock.patch.object(studio, "llm_generate",
                               side_effect=AssertionError("LLM called")):
            for kind in ("script_writer", "style", "notes", "shot_planner"):
                self.assertEqual(self._post(kind=kind).status_code, 200, kind)

    def test_script_writer_returns_json_prompt(self):
        resp = self._post(kind="script_writer", title="T1", words="800")
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertIn('titled "T1"', body["prompt"])
        self.assertEqual(body["chars"], len(body["prompt"]))

    def test_judge_uses_the_posted_script(self):
        body = self._post(kind="script_judge", script="MY SCRIPT TEXT").get_json()
        self.assertIn("MY SCRIPT TEXT", body["prompt"])

    def test_missing_input_is_a_400_with_a_message(self):
        resp = self._post(kind="script_judge", script="")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())
        self.assertEqual(self._post(kind="bogus").status_code, 400)

    def test_shot_judge_returns_local_faults(self):
        plan = json.dumps({"images": [{"file": "A.png", "prompt": "p"}],
                           "shots": [{"asset": "A.png", "cues": "1",
                                      "motion": "ST"}]})
        body = self._post(kind="shot_judge", shotlist=plan).get_json()
        self.assertIn("local_faults", body)
        self.assertTrue(body["local_faults"])

    def test_views_tell_writing_style_and_visual_style_apart(self):
        page = lambda stage: self.client.get(
            f"/studio/{self.pid}?stage={stage}").data.decode()
        self.assertIn("writing style", page("style"))
        self.assertIn("how the script sounds", page("style"))
        self.assertIn("not the", page("style"))
        shots = page("shots")
        self.assertIn("Visual style (how the images look)", shots)
        self.assertIn(VSTYLE, shots)              # the channel's visual style
        self.assertNotIn("style guide applied", page("script"))
        self.assertIn("writing style applied", page("script"))
        chans = self.client.get("/my-channels").data.decode()
        self.assertIn("visual style - how the images look", chans)

    def test_studio_page_shows_the_collapsible_panels(self):
        for stage in ("script", "shots"):
            html = self.client.get(f"/studio/{self.pid}?stage={stage}").data
            self.assertIn(b"Use an external LLM", html, stage)
            self.assertTrue(b'data-f="mode"' in html, stage + " mode select")
            self.assertTrue(b'class="extfiles"' in html, stage + " files")
        script = self.client.get(f"/studio/{self.pid}?stage=script").data
        self.assertTrue(b"Save as research notes" in script)


class CustomProfileTests(Base):
    channel_fields = {"brief_motion": "custom", "brief_custom": json.dumps(
        {"allowed": ["ZI", "ZO"], "rules": "ONLY-MY-CUSTOM-RULE"})}

    def test_custom_motion_profile_reaches_planner_and_judge(self):
        planner = ep.shotlist_planner_prompt(self.cfg, self.pid)
        self.assertIn("ONLY-MY-CUSTOM-RULE", planner)
        plan = json.dumps({"shots": [
            {"asset": "a", "cues": [1, 3]}], "images": [
            {"file": "a", "prompt": "x"}]})
        text, _ = ep.shotlist_judge_prompt(self.cfg, self.pid, plan)
        self.assertIn("only ZI/ZO is allowed", text)


class SeedAndNotesTests(Base):
    def test_channel_reference_files_are_listed_for_the_planner(self):
        refs = Path(self.tmp.name) / "chanrefs"
        refs.mkdir()
        (refs / "MAYA.png").write_bytes(b"\x89PNG")
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.update_own_channel(conn, self.chan, refs_dir=str(refs))
        conn.commit()
        conn.close()
        self.assertFalse((self.pdir / "refs" / "MAYA.png").exists())
        text = ep.shotlist_planner_prompt(self.cfg, self.pid)
        self.assertIn("MAYA.png", text)
        self.assertTrue((self.pdir / "refs" / "MAYA.png").exists())

    def test_saved_notes_are_kept_by_the_built_in_cache_check(self):
        from whisperradar import autorun
        words = ep.save_notes(self.cfg, self.pid, "- a fact\n- another fact")
        self.assertEqual(words, 6)
        notes = (self.pdir / "research_notes.md").read_text("utf-8")
        # far shorter than 35% of a long source, still trusted
        self.assertTrue(autorun._notes_cache_valid(
            self.pdir, notes, "word " * 5000))

    def test_save_notes_needs_text(self):
        with self.assertRaises(ep.PromptError):
            ep.save_notes(self.cfg, self.pid, "  ")

    def test_route_saves_notes(self):
        app = create_app(self.cfg)
        r = app.test_client().post(f"/studio/{self.pid}/external-prompt",
                                   data={"kind": "save_notes",
                                         "notes": "- saved fact"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("saved fact",
                      (self.pdir / "research_notes.md").read_text("utf-8"))


class LengthAndStyleTests(Base):
    def test_length_window_reaches_writer_and_judge(self):
        w = ep.script_writer_prompt(self.cfg, self.pid, target_words=1000)
        self.assertIn("between 800 and 1150 words", w)
        self.assertIn("complete sentence", w)
        j = ep.script_judge_prompt(self.cfg, self.pid, "Short script here.",
                                   target_words=1000)
        self.assertIn("800-1150 words", j)
        self.assertIn("length 3 words", j)
        self.assertIn("ending is complete", j)
        self.assertIn("do not raise or lower", j)

    def test_judge_flags_a_cut_off_script(self):
        j = ep.script_judge_prompt(self.cfg, self.pid, "It stops mid")
        self.assertIn("LOOKS CUT OFF", j)

    def test_missing_writing_style_asks_instead_of_contradicting(self):
        (self.pdir / "writing_style.md").unlink()
        w = ep.script_writer_prompt(self.cfg, self.pid, target_words=900)
        self.assertNotIn("No style guide provided.", w)
        self.assertNotIn("STYLE GUIDE above", w)
        self.assertIn("ask me to paste or attach the writing style guide", w)


class FilesModeTests(Base):
    channel_fields = {"brief_motion": "static"}

    def _by_name(self, files):
        return {f["name"]: f["text"] for f in files}

    def test_writer_references_style_and_notes_as_files(self):
        files = []
        text = ep.script_writer_prompt(self.cfg, self.pid, target_words=900,
                                       files=files)
        by = self._by_name(files)
        self.assertEqual(set(by), {"writing_style.md", "research_notes.md"})
        self.assertIn(WSTYLE, by["writing_style.md"])
        self.assertIn("pennies from 1943", by["research_notes.md"])
        self.assertNotIn(WSTYLE, text)
        self.assertNotIn("pennies from 1943", text)
        self.assertIn("Missing: <the file names>", text)
        self.assertIn("attached writing_style.md", text)
        self.assertIn("attached research_notes.md", text)
        self.assertIn("QUALITY BAR", text)       # limits stay in the prompt
        self.assertNotIn("@@", text)

    def test_pasted_notes_become_the_notes_file(self):
        files = []
        ep.script_writer_prompt(self.cfg, self.pid, notes="- PASTED-N",
                                files=files)
        self.assertIn("PASTED-N", self._by_name(files)["research_notes.md"])

    def test_writer_files_only_lists_what_exists(self):
        (self.pdir / "writing_style.md").unlink()
        files = []
        text = ep.script_writer_prompt(self.cfg, self.pid, files=files)
        self.assertEqual([f["name"] for f in files], ["research_notes.md"])
        self.assertNotIn("writing_style.md", text.split("QUALITY BAR")[0]
                         .split("TASK:")[0])
        self.assertIn("ask me to paste or attach the writing style", text)

    def test_script_judge_files(self):
        files = []
        text = ep.script_judge_prompt(self.cfg, self.pid, "Judge me.",
                                      files=files)
        self.assertEqual({f["name"] for f in files},
                         {"writing_style.md", "research_notes.md"})
        self.assertNotIn(WSTYLE, text)
        self.assertIn("Judge me.", text)         # the script stays inline
        self.assertIn("Missing:", text)
        self.assertNotIn("@@", text)

    def test_judge_without_notes_attaches_the_source_as_facts(self):
        (self.pdir / "research_notes.md").unlink()
        files = []
        ep.script_judge_prompt(self.cfg, self.pid, "x.", files=files)
        self.assertIn("source_facts.txt", [f["name"] for f in files])

    def test_planner_files_split_brief_and_inputs(self):
        files = []
        text = ep.shotlist_planner_prompt(self.cfg, self.pid, files=files)
        by = self._by_name(files)
        self.assertEqual(set(by), {"planning_brief.md", "shotlist_inputs.md"})
        self.assertGreater(len(by["planning_brief.md"]), 10000)
        inputs = by["shotlist_inputs.md"]
        self.assertIn("Welcome to the video about old coins.", inputs)
        self.assertIn(VSTYLE, inputs)
        self.assertIn(BIBLE, inputs)
        self.assertIn("PACING MATH", inputs)
        self.assertLess(len(text), 6000)
        self.assertNotIn("Welcome to the video about old coins.", text)
        self.assertIn("Missing: <the file names>", text)
        self.assertIn("OUTPUT RULES FOR THIS CHAT", text)
        self.assertNotIn("@@", text + inputs)

    def test_shot_judge_files(self):
        plan = json.dumps({"shots": [{"asset": "a", "cues": [1, 3]}],
                           "images": [{"file": "a", "prompt": "a jar"}]})
        files = []
        text, _ = ep.shotlist_judge_prompt(self.cfg, self.pid, plan,
                                           files=files)
        by = self._by_name(files)
        self.assertEqual(set(by), {"narration.txt", "shotlist.json"})
        self.assertIn("a jar", by["shotlist.json"])
        self.assertNotIn("a jar", text)
        self.assertIn("PART A - HARD RULES", text)


class FilesRouteTests(Base):
    def _post(self, **data):
        app = create_app(self.cfg)
        r = app.test_client().post(f"/studio/{self.pid}/external-prompt",
                                   data=data)
        return r.status_code, r.get_json()

    def test_inline_has_no_files(self):
        code, j = self._post(kind="script_writer", mode="inline")
        self.assertEqual((code, j["files"]), (200, []))
        self.assertIn(WSTYLE, j["prompt"])

    def test_files_mode_returns_files(self):
        code, j = self._post(kind="script_writer", mode="files")
        self.assertEqual(code, 200)
        self.assertEqual({f["name"] for f in j["files"]},
                         {"writing_style.md", "research_notes.md"})
        self.assertNotIn(WSTYLE, j["prompt"])

    def test_auto_stays_inline_when_small(self):
        _c, j = self._post(kind="script_writer")
        self.assertEqual(j["files"], [])

    def test_auto_switches_to_files_when_big(self):
        with mock.patch.object(ep, "AUTO_FILES_CHARS", 500):
            _c, j = self._post(kind="script_writer")
        self.assertTrue(j["files"])

    def test_planner_auto_uses_files_over_the_threshold(self):
        with mock.patch.object(ep, "AUTO_FILES_CHARS", 20000):
            _c, j = self._post(kind="shot_planner")
        self.assertEqual({f["name"] for f in j["files"]},
                         {"planning_brief.md", "shotlist_inputs.md"})


if __name__ == "__main__":
    unittest.main()
