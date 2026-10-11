import io
import json
import tempfile
import unittest
from pathlib import Path

from whisperradar import db
from whisperradar.config import load_config
from whisperradar.webapp import create_app


class WatchedTransfer(unittest.TestCase):
    def test_export_then_import_on_a_fresh_db(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            def client(d):
                cfg = load_config()
                cfg.db_path = Path(d) / "x.db"
                return create_app(cfg).test_client(), cfg
            c1, cfg1 = client(a)
            conn = db.connect(cfg1.db_path); db.init_db(conn)
            db.add_channel(conn, "Alpha", "UC" + "a" * 22, "primary", "tech")
            conn.close()
            data = c1.get("/watched/export").data
            rows = json.loads(data)
            self.assertTrue(any(r["name"] == "Alpha" for r in rows))
            c2, cfg2 = client(b)
            r = c2.post("/watched/import", data={
                "watched_file": (io.BytesIO(data), "w.json")},
                content_type="multipart/form-data")
            self.assertEqual(r.status_code, 302)
            conn = db.connect(cfg2.db_path)
            names = [c["name"] for c in db.list_channels(conn)]
            conn.close()
            self.assertIn("Alpha", names)


if __name__ == "__main__":
    unittest.main()
