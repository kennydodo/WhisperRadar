"""Production export / import: what travels, what does not, and that a bad zip is safe."""
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import db, prodtransfer  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")
        base = Path(tempfile.mkdtemp())
        self.cfg.db_path = base / "wr.db"
        self.cfg.studio_dir = base / "studio"
        self.root = base / "productions"
        self.root.mkdir()
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.set_setting(conn, "productions_root", str(self.root))
        self.chan = db.create_own_channel(conn, "Alicia")
        self.pdir = base / "src"
        self.pdir.mkdir()
        (self.pdir / ".whisperradar-production").write_text("")
        self.pid = db.create_production(conn, "Why we eat X", "general", None, str(self.pdir))
        db.update_production(conn, self.pid, stage="merge", own_channel_id=self.chan,
                             notes="n", voice="v1", extra_prompt="be brief")
        conn.close()
        for name, text in (("script.md", "S"), ("style.md", "ST"), ("subtitles.srt", "1\n"),
                           ("shotlist.json", "{}"), ("beats.json", "{}"),
                           ("flowbatch.json", "E:\\\\abs"), ("webchat_chats.json", "{}")):
            (self.pdir / name).write_text(text)
        (self.pdir / "audio.mp3").write_bytes(b"A" * 100)
        (self.pdir / "images").mkdir()
        (self.pdir / "images" / "S01.png").write_bytes(b"I")
        (self.pdir / "refs").mkdir()
        (self.pdir / "refs" / "CH_A.png").write_bytes(b"R")
        (self.pdir / "packaging").mkdir()
        (self.pdir / "packaging" / "titles.txt").write_text("t1\n")
        (self.pdir / "packaging" / "thumbnails").mkdir()
        (self.pdir / "packaging" / "thumbnails" / "t.png").write_bytes(b"T")

    def names(self, blob):
        return sorted(zipfile.ZipFile(io.BytesIO(blob)).namelist())

    def test_export_leaves_out_images_flow_files_and_chat_logs(self):
        blob, name = prodtransfer.export_production(self.cfg, self.pid)
        n = self.names(blob)
        self.assertEqual(name, "Why we eat X.wrprod.zip")
        for want in ("production.json", "script.md", "style.md", "subtitles.srt",
                     "shotlist.json", "beats.json", "packaging/titles.txt"):
            self.assertIn(want, n)
        for nope in ("flowbatch.json", "webchat_chats.json", "audio.mp3",
                     "images/S01.png", "refs/CH_A.png", "packaging/thumbnails/t.png"):
            self.assertNotIn(nope, n)

    def test_audio_and_refs_only_when_asked(self):
        blob, _ = prodtransfer.export_production(self.cfg, self.pid, audio=True, refs=True)
        n = self.names(blob)
        self.assertIn("audio.mp3", n)
        self.assertIn("refs/CH_A.png", n)
        self.assertNotIn("images/S01.png", n)

    def test_import_creates_a_new_production_in_the_channel_folder(self):
        blob, _ = prodtransfer.export_production(self.cfg, self.pid)
        pid = prodtransfer.import_production(self.cfg, blob)
        self.assertNotEqual(pid, self.pid)
        conn = db.connect(self.cfg.db_path)
        row = db.get_production(conn, pid)
        conn.close()
        self.assertEqual(row["title"], "Why we eat X")
        self.assertEqual(row["own_channel_id"], self.chan)
        self.assertEqual(row["notes"], "n")
        self.assertEqual(row["extra_prompt"], "be brief")
        self.assertEqual(row["stage"], "refs")          # images are not exported
        new = Path(row["work_dir"])
        self.assertEqual(new.parent.name, "Alicia")
        self.assertEqual((new / "script.md").read_text(), "S")
        self.assertEqual((new / "packaging" / "titles.txt").read_text(), "t1\n")
        self.assertFalse((new / "images").exists())
        self.assertFalse((new / "flowbatch.json").exists())

    def test_a_production_before_the_image_stages_keeps_its_stage(self):
        conn = db.connect(self.cfg.db_path)
        db.update_production(conn, self.pid, stage="script")
        conn.close()
        blob, _ = prodtransfer.export_production(self.cfg, self.pid)
        pid = prodtransfer.import_production(self.cfg, blob, title="Copy")
        conn = db.connect(self.cfg.db_path)
        row = db.get_production(conn, pid)
        conn.close()
        self.assertEqual((row["stage"], row["title"]), ("script", "Copy"))

    def test_unsafe_and_unknown_entries_are_ignored(self):
        blob, _ = prodtransfer.export_production(self.cfg, self.pid)
        src = zipfile.ZipFile(io.BytesIO(blob))
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as zf:
            for n in src.namelist():
                zf.writestr(n, src.read(n))
            zf.writestr("../escape.txt", "x")
            zf.writestr("/abs.txt", "x")
            zf.writestr("images/evil.png", "x")
            zf.writestr("out/run.mp4", "x")
        pid = prodtransfer.import_production(self.cfg, out.getvalue())
        conn = db.connect(self.cfg.db_path)
        new = Path(db.get_production(conn, pid)["work_dir"])
        conn.close()
        self.assertFalse((new.parent / "escape.txt").exists())
        self.assertFalse((new / "images").exists())
        self.assertFalse((new / "out").exists())
        self.assertTrue((new / "script.md").exists())

    def test_a_foreign_zip_is_refused(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as zf:
            zf.writestr("hello.txt", "x")
        with self.assertRaises(prodtransfer.TransferError):
            prodtransfer.import_production(self.cfg, out.getvalue())
        with self.assertRaises(prodtransfer.TransferError):
            prodtransfer.import_production(self.cfg, b"not a zip")

    def test_routes_round_trip(self):
        client = create_app(self.cfg).test_client()
        page = client.get(f"/studio/{self.pid}")
        self.assertIn("Export this production", page.get_data(as_text=True))
        resp = client.get(f"/studio/{self.pid}/export?audio=1")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("audio.mp3", self.names(resp.data))
        resp = client.post("/studio/import", data={
            "export_file": (io.BytesIO(resp.data), "x.zip"), "own_channel_id": str(self.chan)},
            content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/studio/", resp.headers["Location"])
        self.assertIn("Import a production", client.get("/in-production").get_data(as_text=True))
        resp = client.post("/studio/import", data={}, content_type="multipart/form-data")
        self.assertIn("error=", resp.headers["Location"])


if __name__ == "__main__":
    unittest.main()
