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
from whisperradar import db, gpt_packaging as gp, packaging, prodfiles, studio, thumbnails
from whisperradar.config import load_config
from whisperradar.webapp import create_app

ROOT = Path(__file__).resolve().parents[1]
KEYWORDS = ['ancient food preservation', 'prehistoric food storage', 'hunter gatherer food',
            'Stone Age food', 'food before refrigerators', 'early human diet',
            'history of food preservation', 'preserving meat', 'drying food',
            'smoking food', 'ancient food storage', 'preventing food spoilage',
            'prehistoric survival', 'ancient human survival', 'traditional food preservation',
            'food preservation methods', 'storing food without electricity',
            'prehistoric daily life', 'hunter gatherer survival', 'early human food storage']



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
        for name, value in (("discover", "codex"), ("preflight", None), ("start", None)):
            mock = patch("whisperradar.codex_worker." + name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def tearDown(self):
        self.env.stop()
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def result(self):
        return {"first_comment": "Which food would have been hardest to keep fresh without a refrigerator?", "keywords": ", ".join(KEYWORDS), "title": "How Did Ancient Humans Preserve Food?",
                "titles": [{"text": text, "why": "Test angle"} for text in ("How Did Ancient Humans Preserve Food?", "Food Before Fridges")],
                "description": "What did early humans do before refrigerators? " * 7,
                "thumbnails": [{"path": str(self.generated), "text": text,
                                "prompt": "Test prompt"}
                               for text in ("NO FRIDGE?", "FOOD FOR LATER?")]}

    def job(self):
        gp.queue(self.cfg, self.pid)
        job = gp.claim(self.cfg)
        self.approve_job(job)
        return gp.claim(self.cfg)

    def approve_job(self, job):
        result = self.result()
        proposal = {"pairs": [dict(title=t["text"], why=t["why"], text=i["text"],
            idea="A distinct faithful concept", prompt="Test prompt") for t, i in zip(result["titles"], result["thumbnails"])],
            "research": "Inspected sample references; CTR unavailable"}
        gp.propose(self.cfg, job["id"], job["claim_token"], proposal)
        gp.approve(self.cfg, job["id"])

    def test_youtube_queued_production_can_prepare_packaging(self):
        db.update_production(self.conn, self.pid, status="youtube_queued")
        job = self.job()
        self.assertEqual(job["context"]["production_id"], self.pid)
        gp.complete(self.cfg, job["id"], job["claim_token"], self.result())

    def test_single_pair_allowed_and_four_new_images_rejected(self):
        gp.queue(self.cfg, self.pid)
        job = gp.claim(self.cfg)
        r = self.result()
        pair = dict(title=r["title"], why="script fit", text=r["thumbnails"][0]["text"], idea="concept", prompt="prompt")
        with self.assertRaisesRegex(ValueError, "one or two"):
            gp.propose(self.cfg, job["id"], job["claim_token"], {"pairs": [pair]*4, "research": "notes"})
        gp.propose(self.cfg, job["id"], job["claim_token"], {"pairs": [pair], "research": "notes"})
        gp.approve(self.cfg, job["id"])
        job = gp.claim(self.cfg)
        r["titles"] = r["titles"][:1]
        r["thumbnails"] = r["thumbnails"][:1]
        saved = gp.complete(self.cfg, job["id"], job["claim_token"], r)
        self.assertEqual(len(saved["thumbnails"]), 1)

    def test_import_must_preserve_approved_headlines_and_pair_recommendation(self):
        job = self.job()
        for mode in ("headline", "recommendation"):
            r = self.result()
            if mode == "headline":
                r["thumbnails"][0]["text"] = "NOT APPROVED"
            else:
                r["recommended_thumbnail"] = 2
            with self.assertRaises(ValueError):
                gp.complete(self.cfg, job["id"], job["claim_token"], r)
            self.assertFalse((self.pdir / "packaging").exists())

    def test_unapproved_images_are_rejected(self):
        gp.queue(self.cfg, self.pid)
        job = gp.claim(self.cfg)
        with self.assertRaisesRegex(ValueError, "Approve headline"):
            gp.complete(self.cfg, job["id"], job["claim_token"], self.result())
        self.assertFalse((self.pdir / "packaging").exists())

    def test_proposal_waits_for_human_and_rejects_changed_content(self):
        gp.queue(self.cfg, self.pid)
        job = gp.claim(self.cfg)
        result = self.result()
        proposal = {"pairs": [dict(title=t["text"], why=t["why"], text=i["text"],
                    idea="concept", prompt="prompt") for t, i in zip(result["titles"], result["thumbnails"])],
                    "research": "Reference URLs and no CTR evidence"}
        gp.propose(self.cfg, job["id"], job["claim_token"], proposal)
        self.assertIsNone(gp.claim(self.cfg))
        self.assertEqual(gp.queue(self.cfg, self.pid)["id"], job["id"])
        page = self.client.get("/gpt-pro/queue")
        self.assertIn(b"Approve these headlines", page.data)
        self.assertEqual(self.client.post(f"/gpt-pro/jobs/{job['id']}/approve").status_code, 403)
        (self.pdir / "script.md").write_text("Changed")
        with self.assertRaisesRegex(ValueError, "changed"):
            gp.approve(self.cfg, job["id"])

    def test_legacy_root_files_remain_readable_but_new_outputs_are_grouped(self):
        legacy = self.pdir / "thumbnails"
        legacy.mkdir()
        (legacy / "thumbs.json").write_text(json.dumps({"concepts": [{"id": str(i)} for i in range(4)]}))
        self.assertEqual(len(thumbnails.load_thumbs(self.pdir)["concepts"]), 4)
        (self.pdir / "title.txt").write_text("Old title")
        job = self.job()
        gp.complete(self.cfg, job["id"], job["claim_token"], self.result())
        self.assertEqual((self.pdir / "title.txt").read_text(), "Old title")
        self.assertTrue((self.pdir / "packaging/title.txt").is_file())
        self.assertTrue((self.pdir / "packaging/packaging.json").is_file())
        self.assertEqual(len(thumbnails.load_thumbs(self.pdir)["concepts"]), 2)

    def test_button_on_finished_and_thumbnail_pages(self):
        for url in ("/finished", f"/studio/{self.pid}/thumbnails"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Generate with GPT Pro", response.data)

    def test_missing_runtime_does_not_leave_queued_work(self):
        with patch("whisperradar.codex_worker.discover", side_effect=ValueError("Runtime missing")):
            response = self.client.post(f"/studio/{self.pid}/gpt-pro/generate")
        self.assertIn("Runtime%20missing", response.location)
        self.assertIsNone(gp.latest(self.cfg, self.pid))

    def test_generate_starts_local_worker(self):
        with patch("whisperradar.codex_worker.start") as start:
            response = self.client.post(f"/studio/{self.pid}/gpt-pro/generate")
        self.assertEqual(response.status_code, 302)
        start.assert_called_once_with(self.cfg, "codex")
        self.assertEqual(gp.latest(self.cfg, self.pid)["status"], "queued")

    def test_queue_page_lists_jobs_without_claiming_or_requeueing(self):
        job = gp.queue(self.cfg, self.pid)
        page = self.client.get("/gpt-pro/queue")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Ancient food", page.data)
        self.assertIn(b"queued", page.data)
        self.assertIn(b"No scheduled task", page.data)
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
        self.assertFalse(prodfiles.path(self.pdir, "title.txt").exists())

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
        with self.client.session_transaction() as session:
            claim = session["gpt_packaging_claim"]
        self.approve_job({"id": claim["id"], "claim_token": claim["token"]})
        fresh = gp.claim(self.cfg)
        with self.client.session_transaction() as session:
            session["gpt_packaging_claim"] = {"id": fresh["id"], "token": fresh["claim_token"]}
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

    def test_fewer_than_twenty_keywords_rejected_before_output_is_written(self):
        job=self.job();result=self.result();result['keywords']=', '.join(KEYWORDS[:19])
        with self.assertRaisesRegex(ValueError,'at least 20'):
            gp.complete(self.cfg,job['id'],job['claim_token'],result)
        self.assertFalse((self.pdir/'packaging'/'keywords.txt').exists())
        self.assertEqual(gp.latest(self.cfg,self.pid)['status'],'running')

    def test_case_and_whitespace_duplicates_do_not_count_towards_twenty(self):
        job=self.job();result=self.result()
        result['keywords']=', '.join(KEYWORDS[:19]+['  ANCIENT  FOOD PRESERVATION '])
        with self.assertRaisesRegex(ValueError,'at least 20'):
            gp.complete(self.cfg,job['id'],job['claim_token'],result)

    def test_keywords_exactly_500_characters_are_accepted(self):
        job=self.job();result=self.result()
        text=', '.join(KEYWORDS)
        result['keywords']=text + 'x'*(500-len(text))
        saved=gp.complete(self.cfg,job['id'],job['claim_token'],result)
        self.assertEqual(len(saved['keywords']),500)
        self.assertEqual(len(saved['keywords'].split(', ')),20)

    def test_keywords_501_characters_rejected_without_writing_outputs(self):
        job=self.job();result=self.result()
        text=', '.join(KEYWORDS)
        result['keywords']=text + 'x'*(501-len(text))
        with self.assertRaisesRegex(ValueError,'500 characters'):
            gp.complete(self.cfg,job['id'],job['claim_token'],result)
        self.assertFalse((self.pdir/'packaging'/'keywords.txt').exists())
        self.assertEqual(gp.latest(self.cfg,self.pid)['status'],'running')

    def test_first_comment_saved_separately_and_available_on_finished_page(self):
        job=self.job();result=self.result()
        saved=gp.complete(self.cfg,job['id'],job['claim_token'],result)
        target=self.pdir/'packaging'/'first_comment.txt'
        self.assertEqual(target.read_text(encoding='utf-8').strip(),result['first_comment'])
        self.assertEqual(saved['first_comment_file'],str(target))
        self.assertFalse((self.pdir/'first_comment.txt').exists())
        self.assertEqual(packaging.load_kit(self.pdir)['pinned_comment'],result['first_comment'])
        self.assertIn(b'download>first comment</a>',self.client.get('/finished').data)
        self.assertNotIn(result['first_comment'],prodfiles.path(self.pdir,'description.txt').read_text(encoding='utf-8'))

    def test_missing_first_comment_rejected_before_output_writes(self):
        job=self.job();result=self.result();result.pop('first_comment')
        with self.assertRaisesRegex(ValueError,'first comment'):
            gp.complete(self.cfg,job['id'],job['claim_token'],result)
        self.assertFalse((self.pdir/'packaging'/'first_comment.txt').exists())

    def test_existing_pinned_comment_input_is_supported(self):
        job=self.job();result=self.result();result['pinned_comment']=result.pop('first_comment')
        saved=gp.complete(self.cfg,job['id'],job['claim_token'],result)
        self.assertEqual(saved['first_comment'],result['pinned_comment'])

    def test_keywords_saved_as_plain_text_inside_packaging(self):
        job=self.job();result=self.result()
        result['keywords']=', '.join(KEYWORDS)
        saved=gp.complete(self.cfg,job['id'],job['claim_token'],result)
        target=self.pdir/'packaging'/'keywords.txt'
        self.assertEqual(target.read_text(encoding='utf-8').strip(),result['keywords'])
        self.assertFalse((self.pdir/'keywords.txt').exists())
        self.assertEqual(saved['keywords_file'],str(target))
        self.assertEqual(packaging.load_kit(self.pdir)['tags'],result['keywords'].split(', '))
        self.assertIn(b'download>keywords</a>',self.client.get('/finished').data)
        self.assertNotIn(result['keywords'],prodfiles.path(self.pdir,'description.txt').read_text(encoding='utf-8'))

    def test_import_outputs_and_complete_is_idempotent(self):
        job = self.job()
        result = self.result()
        saved = gp.complete(self.cfg, job["id"], job["claim_token"], result)
        self.assertEqual(gp.complete(self.cfg, job["id"], job["claim_token"], result), saved)
        self.assertEqual(prodfiles.path(self.pdir, "description.txt").read_text().strip(), result["description"].strip())
        self.assertEqual(len(saved["thumbnails"]), 2)
        for path in saved["thumbnails"]:
            with Image.open(path) as image:
                self.assertEqual(image.size, (1280, 720))
        self.assertTrue(prodfiles.path(self.pdir, "title.txt").exists())
        self.assertEqual(prodfiles.path(self.pdir, "titles.txt").read_text().splitlines(), [t["text"] for t in result["titles"]])
        kit = packaging.load_kit(self.pdir)
        self.assertEqual(packaging.assemble_description(kit, [{"start": 0}]), result["description"].strip())
        page = self.client.get("/finished")
        self.assertIn(b"2 thumbnails", page.data)
        self.assertIn(b"kit ready", page.data)
        for name in ("title.txt", "titles.txt", "description.txt"):
            url = f"/studio/file/{self.pid}/packaging/{name}"
            self.assertIn(url.encode(), page.data)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            response.close()
        page = self.client.get(f"/studio/{self.pid}/thumbnails")
        self.assertIn(b"Choose this one", page.data)
        self.assertNotIn(b"Save changes and re-compose", page.data)

    def test_existing_files_are_backed_up_and_precomposed_images_survive_save(self):
        (self.pdir / "description.txt").write_text("My edited description", encoding="utf-8")
        old_kit = {"title": "My previous kit", "description": "Keep my old kit"}
        packaging.save_kit(self.pdir, old_kit)
        job = self.job()
        gp.complete(self.cfg, job["id"], job["claim_token"], self.result())
        backup = thumbnails.thumbs_dir(self.pdir) / "jobs" / job["id"] / "previous-description.txt"
        self.assertEqual(backup.read_text(), "My edited description")
        self.assertEqual(json.loads((backup.parent / "previous-publish_kit.json").read_text()), old_kit)
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
        self.assertFalse(prodfiles.path(self.pdir, "description.txt").exists())

    def test_arbitrary_paths_and_incomplete_results_rejected_before_writing(self):
        job = self.job()
        result = self.result()
        result["thumbnails"][0]["path"] = str(self.pdir / "script.md")
        with self.assertRaisesRegex(ValueError, "Codex-generated"):
            gp.complete(self.cfg, job["id"], job["claim_token"], result)
        result = self.result()
        result["thumbnails"].pop()
        with self.assertRaisesRegex(ValueError, "matching"):
            gp.complete(self.cfg, job["id"], job["claim_token"], result)
        self.assertFalse(prodfiles.path(self.pdir, "title.txt").exists())

    def test_invalid_title_options_rejected_before_writing(self):
        job = self.job()
        for mode in ("missing", "duplicate", "long", "recommendation"):
            result = self.result()
            if mode == "missing":
                result["titles"].pop()
            elif mode == "duplicate":
                result["titles"][1] = result["titles"][0]
            elif mode == "long":
                result["titles"][1]["text"] = "x" * 101
            else:
                result["title"] = "Outside the options"
            with self.assertRaises(ValueError):
                gp.complete(self.cfg, job["id"], job["claim_token"], result)
            self.assertFalse(prodfiles.path(self.pdir, "titles.txt").exists())

    def test_failed_error_can_be_cleared_from_the_finished_page(self):
        job = self.job()
        gp.fail(self.cfg, job["id"], job["claim_token"], "Image tool unavailable")
        html = self.client.get("/finished").get_data(as_text=True)
        self.assertIn("Image tool unavailable", html)
        self.assertIn(f"/studio/{self.pid}/gpt-pro/clear-error", html)
        self.assertIn("Retry GPT Pro", html)
        r = self.client.post(f"/studio/{self.pid}/gpt-pro/clear-error")
        self.assertEqual(r.status_code, 302)
        html = self.client.get("/finished").get_data(as_text=True)
        self.assertNotIn("Image tool unavailable", html)
        self.assertNotIn("clear-error", html)
        self.assertIsNone(gp.latest(self.cfg, self.pid))
        self.assertTrue(self.job())                   # and it can be started again

    def test_clearing_never_touches_active_or_complete_jobs(self):
        queued = gp.queue(self.cfg, self.pid)
        self.assertEqual(gp.clear_error(self.cfg, self.pid), 0)
        self.assertEqual(gp.latest(self.cfg, self.pid)["id"], queued["id"])

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
        result["title"] = result["titles"][1]["text"]
        gp.complete(self.cfg, job["id"], job["claim_token"], result)
        self.assertEqual(thumbnails.load_thumbs(self.pdir)["recommended"], 2)


if __name__ == "__main__":
    unittest.main()
