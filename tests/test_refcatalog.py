"""The reference catalog: describe every library image, and give each story only
the references it needs."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whisperradar import refcatalog as rc, studio  # noqa: E402

LIBRARY = ["CH_MAYA.png", "CH_JOE.png", "CH_JOE_OFFICE.png", "OBJ_LAPTOP.png", "OBJ_SMARTPHONE.png",
           "OBJ_PAYSLIP.png", "OBJ_COIN_JAR_02.png", "BG_KITCHEN.png", "david_face.png"]


def make_production(root, script="", library_index=None, files=LIBRARY, catalog=True):
    lib = Path(root) / "library"
    pdir = Path(root) / "prod"
    (pdir / "refs").mkdir(parents=True)
    lib.mkdir()
    for f in files:
        (lib / f).write_bytes(b"x")
        (pdir / "refs" / f).write_bytes(b"x")
    if library_index is not None:
        (lib / "refs.json").write_text(json.dumps(library_index), encoding="utf-8")
    (pdir / "script.md").write_text(script, encoding="utf-8")
    if catalog:
        rc.build_catalog(pdir, library=lib)
    return lib, pdir


class NameTests(unittest.TestCase):
    def test_kind_and_words(self):
        self.assertEqual(rc.kind_of("OBJ_LAPTOP"), "OBJ")
        self.assertEqual(rc.kind_of("CH_MAYA"), "CH")
        self.assertEqual(rc.kind_of("BG_KITCHEN"), "BG")
        self.assertEqual(rc.kind_of("david_face"), "other")
        self.assertEqual(rc.kind_of("CHAIR"), "other")           # no underscore, not a CH_ name
        self.assertEqual(rc.name_tokens("OBJ_COIN_JAR_02"), ["coin", "jar"])
        self.assertEqual(rc.derived_description("OBJ_SMARTPHONE"), "smartphone")


class ScanTests(unittest.TestCase):
    def test_scan_adds_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            lib = Path(d)
            (lib / "OBJ_LAPTOP.png").write_bytes(b"x")
            first = rc.scan(lib)
            self.assertEqual(first["added"], ["OBJ_LAPTOP"])
            data = json.loads((lib / "refs.json").read_text(encoding="utf-8"))
            data["refs"]["OBJ_LAPTOP"]["description"] = "silver laptop, open"
            data["refs"]["OBJ_LAPTOP"]["aliases"] = ["computer"]
            (lib / "refs.json").write_text(json.dumps(data), encoding="utf-8")
            (lib / "OBJ_NOTEBOOK.png").write_bytes(b"x")
            second = rc.scan(lib)
            self.assertEqual(second["added"], ["OBJ_NOTEBOOK"])
            kept = json.loads((lib / "refs.json").read_text(encoding="utf-8"))["refs"]
            self.assertEqual(kept["OBJ_LAPTOP"]["description"], "silver laptop, open")
            self.assertEqual(kept["OBJ_LAPTOP"]["aliases"], ["computer"])
            self.assertEqual(kept["OBJ_NOTEBOOK"]["description"], "notebook")

    def test_scan_reports_missing_images_and_keeps_their_entries(self):
        with tempfile.TemporaryDirectory() as d:
            lib = Path(d)
            (lib / "OBJ_A.png").write_bytes(b"x")
            rc.scan(lib)
            (lib / "OBJ_A.png").unlink()
            r = rc.scan(lib)
            self.assertEqual(r["missing"], ["OBJ_A"])
            self.assertIn("OBJ_A", json.loads((lib / "refs.json").read_text(encoding="utf-8"))["refs"])

    def test_a_broken_index_is_left_alone(self):
        with tempfile.TemporaryDirectory() as d:
            lib = Path(d)
            (lib / "OBJ_A.png").write_bytes(b"x")
            (lib / "refs.json").write_text("{not json", encoding="utf-8")
            r = rc.scan(lib)
            self.assertTrue(r["warnings"])
            self.assertEqual((lib / "refs.json").read_text(encoding="utf-8"), "{not json")


class MatchTests(unittest.TestCase):
    def active(self, script, **kw):
        with tempfile.TemporaryDirectory() as d:
            _, pdir = make_production(d, script, **kw)
            return rc.active_stems(pdir)

    def test_characters_follow_the_script_objects_follow_their_words(self):
        a = self.active("Maya opens her laptop and checks the payslip. Then the phone buzzes.")
        self.assertIn("CH_MAYA", a)
        self.assertNotIn("CH_JOE", a)                  # a character of another story
        self.assertNotIn("CH_JOE_OFFICE", a)
        self.assertIn("OBJ_LAPTOP", a)
        self.assertIn("OBJ_PAYSLIP", a)
        self.assertNotIn("OBJ_SMARTPHONE", a)          # "phone" is not "smartphone" without an alias
        self.assertNotIn("BG_KITCHEN", a)

    def test_a_variant_of_a_character_follows_the_name(self):
        a = self.active("Joe walks in.")
        self.assertEqual({s for s in a if s.startswith("CH_")}, {"CH_JOE", "CH_JOE_OFFICE"})

    def test_aliases_and_plurals(self):
        idx = {"refs": {"OBJ_SMARTPHONE": {"aliases": ["phone", "mobile"]},
                        "OBJ_COIN_JAR_02": {}}}
        a = self.active("She picks up the phone and two coin jars.", library_index=idx)
        self.assertIn("OBJ_SMARTPHONE", a)
        self.assertIn("OBJ_COIN_JAR_02", a)             # every word of the name, plural allowed

    def test_all_words_of_an_object_name_are_needed(self):
        a = self.active("A jar of pickles stands on the shelf.")
        self.assertNotIn("OBJ_COIN_JAR_02", a)

    def test_always_is_active_in_every_story(self):
        idx = {"refs": {"CH_JOE": {"always": True}}}
        a = self.active("Nothing about him here.", library_index=idx)
        self.assertIn("CH_JOE", a)

    def test_a_channel_file_outside_the_naming_is_kept_unless_switched_off(self):
        self.assertIn("david_face", self.active("A story about coins."))
        off = self.active("A story about coins.", library_index={"refs": {"david_face": {"always": False}}})
        self.assertNotIn("david_face", off)

    def test_whole_words_only(self):
        a = self.active("The mayan temple and a laptopstand.")
        self.assertNotIn("CH_MAYA", a)
        self.assertNotIn("OBJ_LAPTOP", a)


class ActiveListTests(unittest.TestCase):
    def test_no_catalog_means_no_filter(self):
        with tempfile.TemporaryDirectory() as d:
            _, pdir = make_production(d, "Maya.", catalog=False)
            self.assertIsNone(rc.active_stems(pdir))
            self.assertEqual(sorted(studio.find_supplied_refs(pdir)), sorted(LIBRARY))

    def test_no_story_text_yet_means_no_filter_and_nothing_is_saved(self):
        with tempfile.TemporaryDirectory() as d:
            _, pdir = make_production(d, "")
            self.assertIsNone(rc.active_stems(pdir))
            self.assertFalse((pdir / "refs_active.json").exists())
            self.assertEqual(len(studio.find_supplied_refs(pdir)), len(LIBRARY))

    def test_find_supplied_refs_lists_only_the_story_but_disk_lists_all(self):
        with tempfile.TemporaryDirectory() as d:
            _, pdir = make_production(d, "Maya reads the payslip.")
            self.assertEqual(studio.find_supplied_refs(pdir), ["CH_MAYA.png", "OBJ_PAYSLIP.png", "david_face.png"])
            self.assertEqual(len(studio.find_supplied_refs(pdir, active_only=False)), len(LIBRARY))

    def test_the_list_is_saved_and_survives_a_script_change(self):
        with tempfile.TemporaryDirectory() as d:
            _, pdir = make_production(d, "Maya reads the payslip.")
            rc.active_stems(pdir)
            (pdir / "script.md").write_text("Joe opens the laptop.", encoding="utf-8")
            a = rc.active_stems(pdir)
            self.assertEqual(a, {"CH_MAYA", "OBJ_PAYSLIP", "CH_JOE", "CH_JOE_OFFICE", "OBJ_LAPTOP", "david_face"})

    def test_manual_choices_win_and_stay(self):
        with tempfile.TemporaryDirectory() as d:
            _, pdir = make_production(d, "Maya reads the payslip.")
            rc.active_stems(pdir)
            rc.set_manual(pdir, add=["OBJ_SMARTPHONE"], remove=["OBJ_PAYSLIP"])
            (pdir / "script.md").write_text("Maya reads the payslip twice.", encoding="utf-8")
            a = rc.active_stems(pdir)
            self.assertIn("OBJ_SMARTPHONE", a)
            self.assertNotIn("OBJ_PAYSLIP", a)               # removed stays removed
            rc.set_manual(pdir, add=["OBJ_PAYSLIP"])
            self.assertIn("OBJ_PAYSLIP", rc.active_stems(pdir))

    def test_what_the_plan_already_uses_stays_active(self):
        with tempfile.TemporaryDirectory() as d:
            _, pdir = make_production(d, "A story about nothing.")
            (pdir / "shotlist.json").write_text(json.dumps(
                {"refs": {"OBJ_LAPTOP": "refs/OBJ_LAPTOP.png"},
                 "images": [{"refs": ["BG_KITCHEN"]}]}), encoding="utf-8")
            a = rc.active_stems(pdir)
            self.assertIn("OBJ_LAPTOP", a)
            self.assertIn("BG_KITCHEN", a)

    def test_generated_refs_are_never_listed_as_supplied(self):
        with tempfile.TemporaryDirectory() as d:
            _, pdir = make_production(d, "Maya.")
            (pdir / "refs" / "CH_NEW.png").write_bytes(b"x")
            (pdir / "refs_generated.json").write_text(json.dumps(["CH_NEW"]), encoding="utf-8")
            self.assertNotIn("CH_NEW.png", studio.find_supplied_refs(pdir, active_only=False))

    def test_a_broken_state_file_does_not_stop_anything(self):
        with tempfile.TemporaryDirectory() as d:
            _, pdir = make_production(d, "Maya.")
            (pdir / "refs_active.json").write_text("{oops", encoding="utf-8")
            self.assertIsNotNone(rc.active_stems(pdir))      # rebuilt from the story


class NotesAndSeedingTests(unittest.TestCase):
    def test_notes_carry_the_descriptions(self):
        idx = {"refs": {"OBJ_LAPTOP": {"description": "silver laptop, open"}}}
        with tempfile.TemporaryDirectory() as d:
            _, pdir = make_production(d, "Maya opens the laptop.", library_index=idx)
            n = rc.notes(pdir, studio.find_supplied_refs(pdir))
            self.assertEqual(n["OBJ_LAPTOP"], "silver laptop, open")
            self.assertEqual(n["CH_MAYA"], "maya")

    def test_the_prompt_lists_descriptions_for_the_story_only(self):
        idx = {"refs": {"OBJ_LAPTOP": {"description": "silver laptop, open"}}}
        with tempfile.TemporaryDirectory() as d:
            _, pdir = make_production(d, "Maya opens the laptop.", library_index=idx)
            names = studio.find_supplied_refs(pdir)
            text = studio.shotlist_prompt("BRIEF", "1: Maya opens the laptop. (3s)", supplied_refs=names,
                                          ref_notes=rc.notes(pdir, names))
            self.assertIn("- refs/OBJ_LAPTOP.png - silver laptop, open", text)
            self.assertIn("- refs/CH_MAYA.png - maya", text)
            self.assertNotIn("CH_JOE", text)
            self.assertNotIn("OBJ_SMARTPHONE", text)

    def test_the_prompt_is_unchanged_without_notes(self):
        text = studio.shotlist_prompt("BRIEF", "1: Hi. (3s)", supplied_refs=["CH_MAYA.png"])
        self.assertIn("- refs/CH_MAYA.png\n", text)

    def test_the_library_index_is_never_copied_into_refs(self):
        with tempfile.TemporaryDirectory() as d:
            lib, pdir = make_production(d, "Maya.", library_index={"refs": {}})
            self.assertTrue((lib / "refs.json").is_file())
            self.assertFalse((pdir / "refs" / "refs.json").exists())
            self.assertTrue(rc.has_catalog(pdir))

    def test_overview_marks_active_used_and_missing_files(self):
        with tempfile.TemporaryDirectory() as d:
            _, pdir = make_production(d, "Maya opens the laptop.")
            rows = {r["stem"]: r for r in rc.overview(pdir)}
            self.assertTrue(rows["CH_MAYA"]["active"])
            self.assertFalse(rows["CH_JOE"]["active"])
            self.assertTrue(rows["OBJ_LAPTOP"]["on_disk"])


if __name__ == "__main__":
    unittest.main()


class RouteTests(unittest.TestCase):
    """The Studio box and its two routes."""

    def setUp(self):
        from tests import wr_tmp
        from whisperradar import db
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
        self.pid = db.create_production(conn, "Ref Title", "general", None, None)
        db.update_production(conn, self.pid, stage="shots")
        conn.commit()
        conn.close()
        self.pdir = studio.prod_dir(self.cfg, self.pid)
        (self.pdir / "refs").mkdir(parents=True, exist_ok=True)
        for f in ("CH_MAYA.png", "CH_JOE.png", "OBJ_LAPTOP.png"):
            (self.pdir / "refs" / f).write_bytes(b"x")
        (self.pdir / "script.md").write_text("Maya opens the laptop.", encoding="utf-8")
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self._wr_tmp.cleanup(self.tmp)

    def page(self):
        r = self.client.get(f"/studio/{self.pid}?stage=shots")
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def test_an_older_production_offers_to_build_the_list_and_nothing_is_filtered(self):
        self.assertIn("Build the reference list", self.page())
        self.assertEqual(len(studio.find_supplied_refs(self.pdir)), 3)

    def test_building_the_list_filters_to_the_story(self):
        r = self.client.post(f"/studio/{self.pid}/refs/catalog")
        self.assertIn(r.status_code, (302, 303))
        self.assertEqual(studio.find_supplied_refs(self.pdir), ["CH_MAYA.png", "OBJ_LAPTOP.png"])
        html = self.page()
        self.assertIn("References for this story", html)
        self.assertIn("2 of 3 reference images", html)

    def test_the_checkboxes_save_only_real_changes(self):
        self.client.post(f"/studio/{self.pid}/refs/catalog")
        r = self.client.post(f"/studio/{self.pid}/refs/active", data={
            "shown": ["CH_MAYA", "CH_JOE", "OBJ_LAPTOP"], "on": ["CH_MAYA", "CH_JOE"]})
        self.assertIn(r.status_code, (302, 303))
        self.assertEqual(studio.find_supplied_refs(self.pdir), ["CH_JOE.png", "CH_MAYA.png"])
        state = json.loads((self.pdir / "refs_active.json").read_text(encoding="utf-8"))
        self.assertEqual(state["added"], ["CH_JOE"])
        self.assertEqual(state["removed"], ["OBJ_LAPTOP"])

    def test_saving_without_a_list_is_refused_politely(self):
        r = self.client.post(f"/studio/{self.pid}/refs/active", data={"shown": ["CH_MAYA"], "on": []})
        self.assertIn(r.status_code, (302, 303))
        self.assertFalse((self.pdir / "refs_active.json").exists())
