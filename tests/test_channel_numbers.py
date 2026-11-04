"""Each own channel numbers its productions from 1 (channel A #1, channel B #1).
The production id stays the key; the channel number is for display."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

from whisperradar import db  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class ChannelNumberTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        self.a = db.create_own_channel(self.conn, "The Nature Made Us")
        self.b = db.create_own_channel(self.conn, "To Live and More")

    def tearDown(self):
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def make(self, channel, title="T"):
        pid = db.create_production(self.conn, title, "general", None, None)
        db.update_production(self.conn, pid, own_channel_id=channel)
        return pid

    def no(self, pid):
        return db.get_production(self.conn, pid)["channel_no"]

    def test_each_channel_counts_from_one(self):
        a1, a2, b1, a3 = (self.make(self.a), self.make(self.a), self.make(self.b), self.make(self.a))
        self.assertEqual([self.no(x) for x in (a1, a2, a3)], [1, 2, 3])
        self.assertEqual(self.no(b1), 1)
        self.assertNotEqual(a1, b1)

    def test_deleted_numbers_are_not_reused(self):
        a1, a2 = self.make(self.a), self.make(self.a)
        db.delete_production(self.conn, a2)
        self.assertEqual(self.no(self.make(self.a)), 3)

    def test_moving_to_another_channel_takes_the_next_number_there(self):
        a1, a2 = self.make(self.a), self.make(self.a)
        b1 = self.make(self.b)
        db.update_production(self.conn, a2, own_channel_id=self.b)
        self.assertEqual(self.no(a2), 2)
        self.assertEqual(self.no(a1), 1)
        db.update_production(self.conn, a2, title="renamed")
        self.assertEqual(self.no(a2), 2)

    def test_existing_productions_are_numbered_by_creation_order(self):
        a1, a2, b1 = self.make(self.a), self.make(self.a), self.make(self.b)
        self.conn.execute("UPDATE productions SET channel_no = NULL")
        self.conn.execute("DROP TABLE channel_counters")  # as before this feature existed
        self.conn.commit()
        db.init_db(self.conn)
        self.assertEqual([self.no(a1), self.no(a2), self.no(b1)], [1, 2, 1])
        db.init_db(self.conn)  # idempotent
        self.assertEqual([self.no(a1), self.no(a2), self.no(b1)], [1, 2, 1])

    def test_codes_are_stored_unique_and_survive_a_rename(self):
        codes = db.channel_codes(db.list_own_channels(self.conn))
        self.assertEqual(codes[self.a], "NATURE")
        self.assertEqual(codes[self.b], "TLM")
        c = db.create_own_channel(self.conn, "Tiny Name Mix Unit")
        d = db.create_own_channel(self.conn, "Tiny Nice Mix Unit")
        got = db.channel_codes(db.list_own_channels(self.conn))
        self.assertEqual(got[c], "TNMU")
        self.assertEqual(got[d], "TNMU2")
        e = db.create_own_channel(self.conn, "Simply AI Explained", code="ai")
        self.assertEqual(db.channel_codes(db.list_own_channels(self.conn))[e], "AI")
        a1 = self.make(self.a)
        db.update_own_channel(self.conn, self.a, name="Nature Made Us 2")
        codes = db.channel_codes(db.list_own_channels(self.conn))
        self.assertEqual(db.production_label(db.get_production(self.conn, a1), codes), "NATURE-1")
        loose = db.create_production(self.conn, "No channel", "general", None, None)
        self.assertEqual(db.production_label(db.get_production(self.conn, loose), codes), f"#{loose}")

    def test_code_rules(self):
        with self.assertRaises(ValueError):
            db.update_own_channel(self.conn, self.b, code="nature")   # taken
        with self.assertRaises(ValueError):
            db.update_own_channel(self.conn, self.b, code="two words long!")
        with self.assertRaises(ValueError):
            db.create_own_channel(self.conn, "Third", code="TLM")
        db.update_own_channel(self.conn, self.b, code=" tl ")
        self.assertEqual(db.get_own_channel(self.conn, self.b)["code"], "TL")

    def test_existing_channels_get_codes_once_on_upgrade(self):
        self.conn.execute("DROP INDEX idx_own_channels_code")
        self.conn.execute("UPDATE own_channels SET code = NULL")
        self.conn.commit()
        db.init_db(self.conn)
        self.assertEqual(db.get_own_channel(self.conn, self.a)["code"], "NATURE")
        db.update_own_channel(self.conn, self.a, code="WILD")
        db.init_db(self.conn)
        self.assertEqual(db.get_own_channel(self.conn, self.a)["code"], "WILD")

    def test_channel_form_saves_code_and_reports_a_clash(self):
        client = create_app(self.cfg).test_client()
        r = client.post("/my-channels/edit", data={"id": self.b, "code": "nature"})
        self.assertIn("already+used", r.headers["Location"].replace("%20", "+"))
        self.assertEqual(db.get_own_channel(self.conn, self.b)["code"], "TLM")
        client.post("/my-channels/edit", data={"id": self.b, "code": "tlm9"})
        self.assertEqual(db.get_own_channel(self.conn, self.b)["code"], "TLM9")

    def test_pages_show_label_and_keep_internal_number(self):
        a1 = self.make(self.a, "Alpha")
        b1 = self.make(self.b, "Beta")
        client = create_app(self.cfg).test_client()
        text = client.get("/in-production").get_data(as_text=True)
        self.assertIn("NATURE-1", text)
        self.assertIn("TLM-1", text)
        self.assertIn(f"Internal number #{b1}", text)
        page = client.get(f"/studio/{b1}").get_data(as_text=True)
        self.assertIn("TLM-1", page)
        self.assertIn(f'href="/studio/{b1}', page)


if __name__ == "__main__":
    unittest.main()
