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

    def test_codes_are_initials_and_unique(self):
        c = db.create_own_channel(self.conn, "Tiny Name Mix Unit")  # TNMU clash
        codes = db.channel_codes(db.list_own_channels(self.conn))
        self.assertEqual(codes[self.a], "TNMU")
        self.assertEqual(codes[self.b], "TLM")
        self.assertEqual(len(set(codes.values())), 3)
        self.assertNotEqual(codes[c], codes[self.a])
        a1 = self.make(self.a)
        self.assertEqual(db.production_label(db.get_production(self.conn, a1), codes), "TNMU-1")
        loose = db.create_production(self.conn, "No channel", "general", None, None)
        self.assertEqual(db.production_label(db.get_production(self.conn, loose), codes), f"#{loose}")

    def test_pages_show_label_and_keep_internal_number(self):
        a1 = self.make(self.a, "Alpha")
        b1 = self.make(self.b, "Beta")
        client = create_app(self.cfg).test_client()
        text = client.get("/in-production").get_data(as_text=True)
        self.assertIn("TNMU-1", text)
        self.assertIn("TLM-1", text)
        self.assertIn(f"Internal number #{b1}", text)
        page = client.get(f"/studio/{b1}").get_data(as_text=True)
        self.assertIn("TLM-1", page)
        self.assertIn(f'href="/studio/{b1}', page)


if __name__ == "__main__":
    unittest.main()
