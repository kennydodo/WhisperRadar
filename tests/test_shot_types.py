"""Shot-type caps (diagrams/infographics) and host-in-frame shares per channel: the planner/judge text, and the hard-fault gate.

Run: python -m unittest discover -s tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

from whisperradar import briefs, db, settings, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


def plan(codes, host_on=()):
    """A shotlist whose shots follow `codes`; `host_on` = shot numbers (1-based)
    whose image lists the host ref."""
    shots, images = [], []
    for i, code in enumerate(codes, 1):
        f = f"S01_{i:02d}_{code}_ZI.png"
        shots.append({"asset": f, "cues": f"{i}-{i}", "motion": "ZI"})
        img = {"file": f, "prompt": f"p{i}"}
        if i in host_on:
            img["refs"] = ["CH_HOST"]
        images.append(img)
    return {"shots": shots, "images": images}


def prof(**spec):
    return briefs.resolve_profile(None, types=spec)


class NormalizeTests(unittest.TestCase):
    def test_empty_is_none_and_host_name_alone_sets_nothing(self):
        self.assertIsNone(briefs.normalize_types(None))
        self.assertIsNone(briefs.normalize_types({"host_ref": "CH_X"}))

    def test_values_clamped_and_unknown_codes_dropped(self):
        t = briefs.normalize_types({"max": {"inf": "150", "ZZZ": 5, "CMP": 0},
                                    "graphics_max": "30"})
        self.assertEqual(t["max"], {"INF": 100, "CMP": 0})
        self.assertEqual(t["graphics_max"], 30)

    def test_channel_overrides_global_and_100_means_no_limit(self):
        t = briefs.effective_types({"INF": 20, "CMP": 100}, {"max": {"INF": 5}},
                                   40)
        self.assertEqual(t, {"max": {"INF": 5}, "graphics_max": 40})
        self.assertIsNone(briefs.effective_types({"INF": 100}, None, 100))

    def test_host_rules_come_from_the_channel_only(self):
        t = briefs.effective_types(
            {}, {"host_ref": "CH_H", "host": {"INF": 30}})
        self.assertEqual(t["host"], {"INF": 30})


class PromptTextTests(unittest.TestCase):
    def test_hostless_default_profile_is_unchanged(self):
        base = briefs.resolve_profile(None).slot_values()
        self.assertEqual(briefs.resolve_profile(None, types=None).slot_values(),
                         base)
        self.assertNotIn("SHOT TYPE TARGETS", base["MOTION_SECTION"])

    def test_text_reaches_the_brief_and_the_check(self):
        v = prof(max={"CMP": 0}, graphics_max=20, host_ref="CH_H",
                 host={"INF": 100}).slot_values()
        for key in ("MOTION_SECTION", "CHECK_MOTION"):
            self.assertIn("CMP", v[key])
            self.assertIn("NEVER used", v[key])
            self.assertIn("at most 20%", v[key])
            self.assertIn("EVERY INF shot", v[key])


class GateTests(unittest.TestCase):
    def faults(self, data, **spec):
        return studio.shotlist_type_faults(data, prof(**spec))

    def test_no_spec_no_faults(self):
        self.assertEqual(studio.shotlist_type_faults(plan(["INF"] * 5), None), [])

    def test_zero_cap_forbids_the_type_even_in_a_short_plan(self):
        out = self.faults(plan(["SCN", "CMP", "SCN"]), max={"CMP": 0})
        self.assertTrue(out and "never used" in out[0])

    def test_cap_checked_on_plans_of_ten_or_more(self):
        codes = ["SCN"] * 8 + ["INF"] * 2
        self.assertEqual(self.faults(plan(codes), max={"INF": 20}), [])
        codes = ["SCN"] * 7 + ["INF"] * 3
        self.assertTrue(self.faults(plan(codes), max={"INF": 20}))

    def test_combined_graphics_cap(self):
        codes = ["SCN"] * 6 + ["INF", "INF", "CMP", "HYB"]
        out = self.faults(plan(codes), graphics_max=30)
        self.assertTrue(out and "diagram/infographic" in out[0])
        self.assertEqual(self.faults(plan(codes), graphics_max=40), [])

    def test_host_never_always_and_band(self):
        codes = ["INF"] * 4 + ["SCN"] * 4
        spec = dict(host_ref="CH_HOST")
        # never
        self.assertTrue(self.faults(plan(codes, host_on=(1,)),
                                    host={"INF": 0}, **spec))
        self.assertEqual(self.faults(plan(codes), host={"INF": 0}, **spec), [])
        # always
        self.assertTrue(self.faults(plan(codes, host_on=(1, 2, 3)),
                                    host={"INF": 100}, **spec))
        self.assertEqual(self.faults(plan(codes, host_on=(1, 2, 3, 4)),
                                     host={"INF": 100}, **spec), [])
        # about 50% of 4 = 2 +/- 15 points: 2 ok, 0 not
        self.assertEqual(self.faults(plan(codes, host_on=(1, 2)),
                                     host={"INF": 50}, **spec), [])
        self.assertTrue(self.faults(plan(codes), host={"INF": 50}, **spec))

    def test_pacing_includes_the_type_faults(self):
        data = plan(["SCN", "CMP"])
        cues = [{"index": 1, "start": "00:00:00,000", "end": "00:00:02,000",
                 "text": "a"},
                {"index": 2, "start": "00:00:02,000", "end": "00:00:04,000",
                 "text": "b"}]
        faults, _ = studio.shotlist_pacing(data, cues, 12.0,
                                           prof(max={"CMP": 0}))
        self.assertTrue(any("never used" in f for f in faults))


class MotionTableTests(unittest.TestCase):
    def table(self, **spec):
        return prof(**spec).slot_values()["MOTION_SECTION"]

    def test_banned_type_leaves_the_table_but_its_motions_stay_reachable(self):
        t = self.table(max={"CMP": 0})
        self.assertNotIn("| CMP", t)
        self.assertIn("SCN tall subject", t)       # PU/PD keep a row
        self.assertIn("| INF diagram | ZI", t)     # INF untouched

    def test_no_graphics_removes_every_diagram_row(self):
        t = self.table(graphics_max=0)
        for code in ("INF", "CMP", "PROC", "HYB", "OVR"):
            self.assertNotIn(f"| {code}", t)
        self.assertIn("| SCN character beat | ZI", t)
        self.assertIn("SCN panoramic", t)            # PV lost OVR

    def test_host_note_on_the_rows(self):
        t = self.table(host_ref="CH_H", host={"INF": 30, "CMP": 0})
        self.assertIn("the host may share the frame", t)
        self.assertIn("never with the host", t)

    def test_unlimited_channel_keeps_the_table_verbatim(self):
        self.assertEqual(
            briefs.adapt_motion_table(briefs.STANDARD_SLOTS["MOTION_SECTION"],
                                      None),
            briefs.STANDARD_SLOTS["MOTION_SECTION"])


class SettingsAndChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        self.chan = db.create_own_channel(self.conn, "C")
        self.pid = db.create_production(self.conn, "P", "general", None, None)
        db.update_production(self.conn, self.pid, own_channel_id=self.chan)

    def tearDown(self):
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def eff(self):
        return settings.for_production(
            self.conn, db.get_production(self.conn, self.pid))

    def test_defaults_change_nothing(self):
        e = self.eff()
        self.assertIsNone(e["brief_types"])
        self.assertIsNone(e["brief_motion"])

    def test_channel_spec_reaches_the_production(self):
        db.update_own_channel(self.conn, self.chan, brief_types=json.dumps(
            {"max": {"INF": 40}, "host_ref": "CH_H", "host": {"INF": 100}}))
        e = self.eff()
        self.assertEqual(e["brief_types"]["max"], {"INF": 40})
        self.assertEqual(e["brief_types"]["host"], {"INF": 100})

    def test_channels_page_shows_the_saved_values(self):
        db.update_own_channel(self.conn, self.chan, brief_types=json.dumps(
            {"graphics_max": 20, "max": {"CMP": 0}, "host_ref": "CH_ZED",
             "host": {"INF": 30}}))
        html = create_app(self.cfg).test_client().get("/my-channels").get_data(
            as_text=True)
        for bit in ('name="type_max_graphics"', 'value="20"', "CH_ZED",
                    'name="type_host_INF"', 'name="types_form"'):
            self.assertIn(bit, html)

    def test_channel_form_saves_and_clears_the_spec(self):
        client = create_app(self.cfg).test_client()
        form = {"id": str(self.chan), "types_form": "1",
                "type_max_graphics": "20", "type_max_CMP": "0",
                "type_host_ref": "CH_H", "type_host_INF": "30"}
        client.post("/my-channels/brief", data=form)
        row = db.get_own_channel(self.conn, self.chan)
        spec = json.loads(row["brief_types"])
        self.assertEqual(spec["graphics_max"], 20)
        self.assertEqual(spec["max"], {"CMP": 0})
        self.assertEqual(spec["host"], {"INF": 30})
        client.post("/my-channels/brief",
                    data={"id": str(self.chan), "types_form": "1"})
        self.assertIsNone(db.get_own_channel(self.conn, self.chan)["brief_types"])


if __name__ == "__main__":
    unittest.main()
