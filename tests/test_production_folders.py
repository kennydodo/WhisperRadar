"""New productions get a folder under the Settings location, named by title;
a typed channel that does not exist is created and the production added."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402,F401

from whisperradar import db, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class ProductionFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = t / "wr.db"
        self.cfg.studio_dir = t / "studio"
        self.root = t / "Productions"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def set_root(self):
        db.set_setting(self.conn, "productions_root", str(self.root))

    def new(self, **form):
        r = self.client.post("/studio/new", data=form)
        self.assertEqual(r.status_code, 302, r.data[:200])
        return r.headers["Location"]

    def prod(self, pid=1):
        return db.get_production(self.conn, pid)

    def test_folder_named_after_title_is_created(self):
        self.set_root()
        self.new(title='Why: cats/dogs? "rule"')
        wd = Path(self.prod()["work_dir"])
        self.assertEqual(wd.parent, self.root.resolve())
        self.assertTrue(wd.is_dir())
        self.assertEqual(wd.name, "Why cats dogs rule")

    def test_same_title_gets_a_suffix(self):
        self.set_root()
        self.new(title="Same")
        self.new(title="Same")
        self.assertEqual(Path(self.prod(2)["work_dir"]).name, "Same (2)")

    def test_no_location_keeps_default_and_explicit_folder_wins(self):
        self.new(title="A")
        self.assertFalse(self.prod()["work_dir"])
        self.assertTrue((self.cfg.studio_dir / "1").is_dir())
        self.set_root()
        mine = Path(self.tmp.name) / "mine"
        self.new(title="B", work_dir=str(mine))
        self.assertEqual(Path(self.prod(2)["work_dir"]), mine.resolve())

    def test_missing_channel_is_created_and_reused(self):
        self.new(title="One", channel_name="Brand New", genre="nature")
        ch = db.get_own_channel(self.conn, "Brand New")
        self.assertIsNotNone(ch)
        self.assertEqual(self.prod()["own_channel_id"], ch["id"])
        self.new(title="Two", channel_name="brand new")
        self.assertEqual(self.prod(2)["own_channel_id"], ch["id"])
        self.assertEqual(len(db.list_own_channels(self.conn)), 1)

    def test_safe_folder_name(self):
        self.assertEqual(studio.safe_folder_name("  a<b>c.  "), "a b c")
        self.assertEqual(studio.safe_folder_name("???"), "production")


if __name__ == "__main__":
    unittest.main()
