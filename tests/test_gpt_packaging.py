"""Codex queue, local bridge boundary and importing finished packaging.

No image engine, browser or paid API is called by these tests.
"""
import datetime as dt
import json
import os
import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from tests import wr_tmp
from whisperradar import db, gpt_packaging as gp, packaging, studio, thumbnails
from whisperradar.config import load_config
from whisperradar.webapp import create_app

ROOT = Path(__file__).resolve().parents[1]


class GPTPackagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = self.root / "wr.db"
        self.cfg.studio_dir = self.root / "studio"
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        cid = db.create_own_channel(self.conn, "The Nature Made Us")
        db.update_own_channel(self.conn, cid, style="Live channel style", bible="Live Bible")
        self.pid = db.create_production(self.conn, "Ancient food", "general", None, None)
        db.update_production(self.conn, self.pid, status="ready", own_channel_id=cid)
        self.pdir = studio.prod_dir(self.cfg, self.pid)
        self.pdir.mkdir(parents=True, exist_ok=True)
        (self.pdir / "script.md").write_text("Final script about preserving food.", encoding="utf-8")
        (self.pdir / "style.md").write_text("Old seeded style", encoding="utf-8")
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.root / "codex")})
        self.env.start()
        self.generated = self.root / "codex" / "generated_images" / "sample.png"
        self.generated.parent.mkdir(parents=True)
        Image.new("RGB", (640, 360), "orange").save(self.generated)
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.env.stop()
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def result(self):
        return {"title": "How Did Ancient Humans Preserve Food?",
                "titles": [{"text": text, "why": "Test angle"} for text in ("How Did Ancient Humans Preserve Food?", "Food Before Fridges", "Keeping Food Through Winter", "Ancient Food Storage")],
                "description": "What did early humans do before refrigerators? " * 7,
                "thumbnails": [{"path": str(self.generated), "text": text,
                                "prompt": "Test prompt"}
                               for text in ("NO FRIDGE?", "FOOD FOR LATER?", "BEAT THE ROT?", "SAVE THE FOOD?")]}

    def job(self):
        gp.queue(self.cfg, self.pid)
        return gp.claim(self.cfg)

    def test_button_on_finished_and_thumbnail_pages(self):
        for url in ("/finished", f"/studio/{self.pid}/thumbnails"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Generate with GPT Pro", response.data)

    def test_queue_page_lists_jobs_without_claiming_or_requeueing(self):
        job = gp.queue(self.cfg, self.pid)
        page = self.client.get("/gpt-pro/queue")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Ancient food", page.data)
        self.assertIn(b"queued", page.data)
        self.assertIn(b"Run now", page.data)
        self.assertEqual(gp.latest(self.cfg, self.pid)["id"], job["id"])
        self.assertEqual(gp.latest(self.cfg, self.pid)["status"], "queued")
        claim = gp.claim(self.cfg)
        self.assertNotIn(claim["claim_token"].encode(), self.client.get("/gpt-pro/queue").data)

    def test_duplicate_clicks_keep_one_job(self):
        first = gp.queue(self.cfg, self.pid)
        self.assertEqual(gp.queue(self.cfg, self.pid)["id"], first["id"])
        self.assertEqual(gp.latest(self.cfg, self.pid)["status"], "queued")
        self.assertIn(b"Queued for GPT Pro", self.client.get("/finished").data)

    def test_unfinished_production_cannot_be_queued(self):
        db.update_production(self.conn, self.pid, status="active")
        with self.assertRaisesRegex(ValueError, "Finish"):
            gp.queue(self.cfg, self.pid)

    def test_published_production_cannot_generate(self):
        db.update_production(self.conn, self.pid, status="published")
        with self.assertRaisesRegex(ValueError, "published"):
            gp.queue(self.cfg, self.pid)
        for url in ("/finished?status=published", f"/studio/{self.pid}/thumbnails"):
            page = self.client.get(url).data
            self.assertRegex(page, rb'<button[^>]*disabled[^>]*>Generate with GPT Pro</button>')
        self.client.post(f"/studio/{self.pid}/gpt-pro/generate")
        self.assertIsNone(gp.latest(self.cfg, self.pid))

    def test_published_after_queue_is_not_claimed(self):
        gp.queue(self.cfg, self.pid)
        db.update_production(self.conn, self.pid, status="published")
        self.assertIsNone(gp.claim(self.cfg))
        self.assertEqual(gp.latest(self.cfg, self.pid)["status"], "failed")

    def test_published_after_claim_cannot_import(self):
        job = self.job()
        db.update_production(self.conn, self.pid, status="published")
        with self.assertRaisesRegex(ValueError, "published"):
            gp.complete(self.cfg, job["id"], job["claim_token"], self.result())
        self.assertFalse((self.pdir / "title.txt").exists())

    def test_context_uses_live_channel_and_production_override(self):
        self.assertEqual(gp.context(self.cfg, self.pid)["visual_style"], "Live channel style")
        db.update_production(self.conn, self.pid, style_override=1)
        self.assertEqual(gp.context(self.cfg, self.pid)["visual_style"], "Old seeded style")

    def test_claim_is_serialized_and_token_is_not_in_public_status(self):
        gp.queue(self.cfg, self.pid)
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = list(pool.map(lambda _: gp.claim(self.cfg), range(2)))
        self.assertEqual(sum(x is not None for x in jobs), 1)
        self.assertNotIn("claim_token", gp.latest(self.cfg, self.pid))

    def test_worker_endpoint_requires_local_agent_header(self):
        self.assertEqual(self.client.post("/gpt-pro/jobs/claim").status_code, 403)
        hdr = {"X-WhisperRadar-Agent": "codex-packaging"}
        self.assertIn(self.client.post("/gpt-pro/jobs/claim", headers=hdr,
                      environ_base={"REMOTE_ADDR": "192.168.1.9"}).status_code, (401, 403))
        self.assertEqual(self.client.post("/gpt-pro/jobs/claim", headers=hdr).json,
                         {"job": None})

    def test_browser_worker_claim_resume_and_import(self):
        gp.queue(self.cfg, self.pid)
        token = re.search(rb'name="csrf" value="([^"]+)"',
                          self.client.get("/gpt-pro/worker").data).group(1).decode()
        page = self.client.post("/gpt-pro/worker", data={"action": "claim",
                                                         "csrf": token})
        self.assertIn(b"production-context", page.data)
        self.assertIn(b"Final script about preserving food.", page.data)
        self.assertIn(b"Packaging result JSON", self.client.get("/gpt-pro/worker").data)
        page = self.client.post("/gpt-pro/worker", data={"action": "complete",
                               "csrf": token, "result": json.dumps(self.result())})
        self.assertIn(b"Completed", page.data)
        self.assertEqual(gp.latest(self.cfg, self.pid)["status"], "complete")
        self.assertIn(b"Claim next queued job", self.client.get("/gpt-pro/worker").data)

    def test_worker_post_without_csrf_token_is_rejected(self):
        gp.queue(self.cfg, self.pid)
        self.assertEqual(self.client.post("/gpt-pro/worker",
                           data={"action": "claim"}).status_code, 403)
        self.assertEqual(gp.latest(self.cfg, self.pid)["status"], "queued")

    def test_browser_worker_rejects_proxy_and_remote_access(self):
        response = self.client.get("/gpt-pro/worker", headers={
            "X-Forwarded-For": "192.168.1.9"})
        self.assertIn(response.status_code, (302, 401, 403))
        if response.status_code == 302:
            self.assertIn("login", response.location)

    def test_import_outputs_and_complete_is_idempotent(self):
        job = self.job()
        result = self.result()
        saved = gp.complete(self.cfg, job["id"], job["claim_token"], result)
        self.assertEqual(gp.complete(self.cfg, job["id"], job["claim_token"], result), saved)
        self.assertEqual((self.pdir / "description.txt").read_text().strip(), result["description"].strip())
        self.assertEqual(len(saved["thumbnails"]), 4)
        for path in saved["thumbnails"]:
            with Image.open(path) as image:
                self.assertEqual(image.size, (1280, 720))
        self.assertTrue((self.pdir / "title.txt").exists())
        self.assertEqual((self.pdir / "titles.txt").read_text().splitlines(), [t["text"] for t in result["titles"]])
        kit = packaging.load_kit(self.pdir)
        self.assertEqual(packaging.assemble_description(kit, [{"start": 0}]), result["description"].strip())
        page = self.client.get("/finished")
        self.assertIn(b"4 thumbnails", page.data)
        self.assertIn(b"kit ready", page.data)
        page = self.client.get(f"/studio/{self.pid}/thumbnails")
        self.assertIn(b"Choose this one", page.data)
        self.assertNotIn(b"Save changes and re-compose", page.data)

    def test_existing_files_are_backed_up_and_precomposed_images_survive_save(self):
        (self.pdir / "description.txt").write_text("My edited description", encoding="utf-8")
        job = self.job()
        gp.complete(self.cfg, job["id"], job["claim_token"], self.result())
        backup = self.pdir / "thumbnails" / "jobs" / job["id"] / "previous-description.txt"
        self.assertEqual(backup.read_text(), "My edited description")
        data = thumbnails.load_thumbs(self.pdir)
        final = self.pdir / data["concepts"][0]["final"]
        before = final.read_bytes()
        self.client.post(f"/studio/{self.pid}/thumbnails/save", data={
            "text_" + data["concepts"][0]["id"]: "Different text"})
        self.assertEqual(final.read_bytes(), before)
        self.assertEqual(thumbnails.load_thumbs(self.pdir)["concepts"][0]["text"], "NO FRIDGE?")

    def test_changed_script_rejects_result(self):
        job = self.job()
        (self.pdir / "script.md").write_text("A changed final script", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed"):
            gp.complete(self.cfg, job["id"], job["claim_token"], self.result())
        self.assertFalse((self.pdir / "description.txt").exists())

    def test_arbitrary_paths_and_incomplete_results_rejected_before_writing(self):
        job = self.job()
        result = self.result()
        result["thumbnails"][0]["path"] = str(self.pdir / "script.md")
        with self.assertRaisesRegex(ValueError, "Codex-generated"):
            gp.complete(self.cfg, job["id"], job["claim_token"], result)
        result = self.result()
        result["thumbnails"].pop()
        with self.assertRaisesRegex(ValueError, "four"):
            gp.complete(self.cfg, job["id"], job["claim_token"], result)
        self.assertFalse((self.pdir / "title.txt").exists())

    def test_invalid_title_options_rejected_before_writing(self):
        job = self.job()
        for mode in ("missing", "duplicate", "long", "recommendation"):
            result = self.result()
            if mode == "missing":
                result["titles"].pop()
            elif mode == "duplicate":
                result["titles"][3] = result["titles"][0]
            elif mode == "long":
                result["titles"][3]["text"] = "x" * 101
            else:
                result["title"] = "Outside the options"
            with self.assertRaises(ValueError):
                gp.complete(self.cfg, job["id"], job["claim_token"], result)
            self.assertFalse((self.pdir / "titles.txt").exists())

    def test_failed_and_abandoned_jobs_can_be_retried_explicitly(self):
        job = self.job()
        gp.fail(self.cfg, job["id"], job["claim_token"], "Image tool unavailable")
        next_job = self.job()
        self.assertNotEqual(job["id"], next_job["id"])
        conn = db.connect(self.cfg.db_path)
        conn.execute("UPDATE gpt_packaging_jobs SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                     (next_job["id"],))
        conn.commit()
        conn.close()
        self.assertEqual(gp.latest(self.cfg, self.pid)["status"], "failed")
        self.assertEqual(gp.queue(self.cfg, self.pid)["status"], "queued")

    def _age_claim(self, jid, seconds):
        aged = (dt.datetime.now(dt.timezone.utc)
                - dt.timedelta(seconds=seconds)).isoformat(timespec="seconds")
        conn = db.connect(self.cfg.db_path)
        conn.execute("UPDATE gpt_packaging_jobs SET updated_at=? WHERE id=?",
                     (aged, jid))
        conn.commit()
        conn.close()

    def test_page_poll_keeps_lease_alive(self):
        job = self.job()
        self._age_claim(job["id"], gp.LEASE_SECONDS - 60)
        self.assertIsNotNone(gp.worker_job(self.cfg, job["id"], job["claim_token"]))
        self.assertEqual(gp.latest(self.cfg, self.pid)["status"], "running")

    def test_abandoned_claim_still_expires(self):
        job = self.job()
        self._age_claim(job["id"], gp.LEASE_SECONDS + 60)
        self.assertIsNone(gp.worker_job(self.cfg, job["id"], job["claim_token"]))
        self.assertEqual(gp.latest(self.cfg, self.pid)["status"], "failed")

    def test_complete_description_skips_chapter_and_hashtag_faults(self):
        job = self.job()
        gp.complete(self.cfg, job["id"], job["claim_token"], self.result())
        kit = packaging.load_kit(self.pdir)
        self.assertTrue(kit["description_complete"])
        self.assertEqual(packaging.local_faults(kit, [{"start": 0}]), [])

    def test_recommended_thumbnail_is_clamped(self):
        job = self.job()
        result = self.result()
        result["recommended_thumbnail"] = 9
        gp.complete(self.cfg, job["id"], job["claim_token"], result)
        self.assertEqual(thumbnails.load_thumbs(self.pdir)["recommended"], 4)


if __name__ == "__main__":
    unittest.main()
