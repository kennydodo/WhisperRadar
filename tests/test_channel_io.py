"""Save / load My Channels settings as JSON: export shape, round trip, and what
import refuses or drops.

Run: python -m unittest tests.test_channel_io
"""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import channel_io, db  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class ChannelIoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = load_config(None)
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        self.addCleanup(self.conn.close)
        self.oc = db.create_own_channel(
            self.conn, "Alpha", description="d", genre="Human & Animal",
            style="line one\nline two", per_day=3, default_upscale=2,
            script_min_rating=7.5, brief_motion="custom",
            brief_custom=json.dumps({
                "allowed": ["ST", "ZI"], "st_max_share": 40, "st_max_hold": 3,
                "code_max_share": None, "pan_max_share": None,
                "tilt_max_share": None, "rules": "x"}),
            brief_types=json.dumps({"max": {"INF": 5}}),
            renderly_channel_id="r-1", renderly_channel_name="Alpha")

    def test_export_omits_machine_fields_and_inherit(self):
        doc = channel_io.export_channels(self.conn)
        self.assertEqual(doc["format"], channel_io.FORMAT)
        ch = doc["channels"][0]
        for key in ("id", "renderly_channel_id", "renderly_channel_name",
                    "bible", "default_voice"):
            self.assertNotIn(key, ch)
        self.assertEqual(ch["name"], "Alpha")
        self.assertEqual(ch["style"], "line one\nline two")
        self.assertIsInstance(ch["brief_custom"], dict)   # real JSON, not a string

    def test_export_one_and_unknown(self):
        self.assertEqual(len(channel_io.export_channels(self.conn, "alpha")["channels"]), 1)
        self.assertEqual(channel_io.export_channels(self.conn, "nope")["channels"], [])

    def test_round_trip_into_empty_db(self):
        doc = json.loads(json.dumps(channel_io.export_channels(self.conn)))
        db.remove_own_channel(self.conn, self.oc)
        res = channel_io.import_channels(self.conn, self.cfg, doc)
        self.assertEqual(res["created"], ["Alpha"])
        row = db.get_own_channel(self.conn, "Alpha")
        self.assertEqual(row["per_day"], 3)
        self.assertEqual(row["style"], "line one\nline two")
        self.assertEqual(row["brief_motion"], "custom")
        self.assertEqual(json.loads(row["brief_types"])["max"]["INF"], 5)
        self.assertIsNone(row["renderly_channel_id"])

    def test_existing_kept_unless_overwrite(self):
        doc = channel_io.export_channels(self.conn)
        db.update_own_channel(self.conn, self.oc, per_day=9)
        res = channel_io.import_channels(self.conn, self.cfg, doc)
        self.assertEqual(res["skipped"], ["Alpha"])
        self.assertEqual(db.get_own_channel(self.conn, "Alpha")["per_day"], 9)
        res = channel_io.import_channels(self.conn, self.cfg, doc, overwrite=True)
        self.assertEqual(res["updated"], ["Alpha"])
        row = db.get_own_channel(self.conn, "Alpha")
        self.assertEqual(row["per_day"], 3)
        self.assertEqual(row["renderly_channel_id"], "r-1")   # link survives

    def test_overwrite_restores_inherit(self):
        doc = channel_io.export_channels(self.conn)
        db.update_own_channel(self.conn, self.oc, default_voice="somebody")
        channel_io.import_channels(self.conn, self.cfg, doc, overwrite=True)
        self.assertIsNone(db.get_own_channel(self.conn, "Alpha")["default_voice"])

    def test_bad_values_dropped_and_reported(self):
        doc = {"format": channel_io.FORMAT, "version": 1, "channels": [{
            "name": "Beta", "per_day": "lots", "script_min_rating": 99,
            "render_target": "vhs", "producer_llm_provider": "ghost",
            "watched_channels": ["UC_not_monitored"], "bogus_field": 1,
            "brief_types": "not a dict"}]}
        res = channel_io.import_channels(self.conn, self.cfg, doc)
        self.assertEqual(res["created"], ["Beta"])
        row = db.get_own_channel(self.conn, "Beta")
        self.assertIsNone(row["per_day"])
        self.assertEqual(row["script_min_rating"], 10.0)      # clamped
        self.assertIsNone(row["render_target"])
        self.assertIsNone(row["producer_llm_provider"])
        self.assertIsNone(row["watched_channels"])
        self.assertIsNone(row["brief_types"])
        self.assertTrue(len(res["notes"]) >= 5)

    def test_rejects_other_files(self):
        for bad in ({"hello": 1}, "text", [], {"format": channel_io.FORMAT,
                                               "channels": []}):
            res = channel_io.import_channels(self.conn, self.cfg, bad)
            self.assertTrue(res["error"], bad)
        newer = {"format": channel_io.FORMAT, "version": 99,
                 "channels": [{"name": "Z"}]}
        self.assertIn("version", channel_io.import_channels(
            self.conn, self.cfg, newer)["error"])
        self.assertIsNone(db.get_own_channel(self.conn, "Z"))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = load_config(None)
        cfg.db_path = Path(self.tmp.name) / "wr.db"
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        self.oc = db.create_own_channel(conn, "Gamma", per_day=4)
        conn.close()
        self.cfg = cfg
        self.client = create_app(cfg).test_client()

    def test_export_download_and_import_upload(self):
        r = self.client.get("/my-channels/export")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["Content-Disposition"])
        doc = json.loads(r.data)
        self.assertEqual(doc["channels"][0]["name"], "Gamma")
        one = self.client.get(f"/my-channels/export?id={self.oc}")
        self.assertIn("gamma", one.headers["Content-Disposition"].lower())
        doc["channels"][0]["name"] = "Delta"
        r = self.client.post("/my-channels/import", data={
            "channels_file": (io.BytesIO(json.dumps(doc).encode()), "c.json")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        conn = db.connect(self.cfg.db_path)
        try:
            self.assertEqual(db.get_own_channel(conn, "Delta")["per_day"], 4)
        finally:
            conn.close()

    def test_import_garbage_is_an_error_banner(self):
        r = self.client.post("/my-channels/import", data={
            "channels_file": (io.BytesIO(b"{nope"), "c.json")},
            content_type="multipart/form-data")
        self.assertIn("error=", r.headers["Location"])

    def test_page_shows_buttons(self):
        html = self.client.get("/my-channels").get_data(as_text=True)
        self.assertIn("/my-channels/import", html)
        self.assertIn(f"/my-channels/export?id={self.oc}", html)


if __name__ == "__main__":
    unittest.main()
