import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from whisperradar import script_readiness as sr
from tests.test_external_prompts import Base


class ScanTests(unittest.TestCase):
    def test_ads_segues_and_ctas_are_separate(self):
        report = sr.scan("Sponsored by Example. Watch this next video. Subscribe.")
        categories = {f["category"] for f in report["findings"]}
        self.assertTrue({"Advertisement", "Promotional segue", "CTA"} <= categories)
        self.assertTrue(all(f["severity"] == "info" for f in report["findings"] if f["category"] == "CTA"))

    def test_numbering_and_absolutes(self):
        report = sr.scan("Weapon two: fire. Weapon four: care. Predators never cross fire.")
        self.assertTrue(any(f["category"] == "Numbering" for f in report["findings"]))
        self.assertTrue(any(f["category"] == "Strong factual claim" for f in report["findings"]))

    def test_ordinal_weapon_gap(self):
        self.assertTrue(any(f["category"] == "Numbering" for f in sr.scan("The second human weapon. This is the fourth weapon.")["findings"]))

    def test_acknowledgement_invalidated_by_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "script.md"
            p.write_text("This is guaranteed.", encoding="utf-8")
            report = sr.ensure(tmp)
            self.assertTrue(sr.pending(tmp))
            self.assertFalse(sr.acknowledge(tmp, "stale"))
            self.assertTrue(sr.acknowledge(tmp, report["script_hash"]))
            self.assertFalse(sr.pending(tmp))
            p.write_text("This is always guaranteed.", encoding="utf-8")
            self.assertTrue(sr.pending(tmp))

    def test_clean_and_cta_only_do_not_pause(self):
        self.assertEqual(sr.scan("A calm story about a person.")["warning_count"], 0)
        self.assertEqual(sr.scan("Subscribe and share this.")["warning_count"], 0)

    def test_confirmed_false_alarm_clears_warning_but_edits_expire_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'script.md'
            path.write_text('Never discard it without checking.\n', encoding='utf-8')
            report = sr.ensure(tmp)
            sr.record_review(tmp, report['script_hash'], [{'finding': 0, 'verdict': 'not_an_issue', 'reason': 'Instruction, not an absolute factual claim.'}])
            self.assertFalse(sr.pending(tmp))
            self.assertEqual(sr.ensure(tmp)['reviewed_count'], 1)
            path.write_text('Never discard it without checking. Another line.\n', encoding='utf-8')
            self.assertTrue(sr.pending(tmp))

    def test_unresolved_review_keeps_red_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'script.md').write_text('Guaranteed returns.\n', encoding='utf-8')
            report = sr.ensure(tmp)
            sr.record_review(tmp, report['script_hash'], [{'finding': 0, 'verdict': 'unresolved', 'reason': 'Needs evidence.'}])
            self.assertTrue(sr.pending(tmp))


class WorkflowTests(Base):
    def test_manual_save_scans_and_acknowledgement_is_versioned(self):
        __import__('whisperradar.cli_llm', fromlist=['save']).save(self.cfg, {'default': 'codex', 'codex_enabled': 'on', 'claude_enabled': 'on', 'show_fixes': 'on'})
        app = __import__("whisperradar.webapp", fromlist=["create_app"]).create_app(self.cfg)
        client = app.test_client()
        client.post(f"/studio/{self.pid}/script/save", data={"script": "Sponsored by Example."})
        report = sr.ensure(self.pdir)
        self.assertTrue(sr.pending(self.pdir, self.cfg))
        page = client.get(f"/studio/{self.pid}?stage=script")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Ignore warnings for this version", page.data)
        client.post(f"/studio/{self.pid}/script/warnings/ignore", data={"script_hash": "old"})
        self.assertTrue(sr.pending(self.pdir, self.cfg))
        client.post(f"/studio/{self.pid}/script/warnings/ignore", data={"script_hash": report["script_hash"]})
        self.assertFalse(sr.pending(self.pdir, self.cfg))

    def test_review_off_hides_the_box_and_does_not_hold_audio(self):
        app = __import__("whisperradar.webapp", fromlist=["create_app"]).create_app(self.cfg)
        client = app.test_client()
        client.post(f"/studio/{self.pid}/script/save", data={"script": "Sponsored by Example."})
        self.assertFalse(sr.review_enabled(self.cfg))
        self.assertFalse(sr.pending(self.pdir, self.cfg))
        self.assertTrue(sr.pending(self.pdir))            # the scan itself still runs
        page = client.get(f"/studio/{self.pid}?stage=script")
        self.assertNotIn(b"Script needs review before audio", page.data)
        self.assertNotIn(b"Ignore warnings for this version", page.data)

    def test_browser_save_scans(self):
        from whisperradar.webstages import save_script
        save_script(self.cfg, self.pid, "Watch this next video.", "test", False)
        self.assertTrue(sr.pending(self.pdir))

    def test_audio_pauses_before_hook(self):
        from whisperradar import autorun
        __import__('whisperradar.cli_llm', fromlist=['save']).save(self.cfg, {'default': 'codex', 'codex_enabled': 'on', 'claude_enabled': 'on', 'show_fixes': 'on'})
        (self.pdir / "script.md").write_text("A guaranteed result.", encoding="utf-8")
        self.cfg.studio_tts_command = "unused-command"
        with patch("whisperradar.studio.run_hook") as hook:
            with self.assertRaises(autorun._Paused):
                autorun._run_audio(self.cfg, self.pid)
            hook.assert_not_called()
