"""Auto Run transport: webchat (default) drives script/shotlist/plan/thumbnails
through the chat-site jobs; api keeps the provider path. The switch is applied
by run_pipeline via an explicit transport arg, so the runners keep their API
behaviour when called directly."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_external_prompts import Base  # noqa: E402
from whisperradar import autorun, db, settings, webstages  # noqa: E402
from whisperradar import plan as packplan, thumbnails as th  # noqa: E402


class TransportSettingTests(Base):
    def test_default_is_webchat(self):
        conn = db.connect(self.cfg.db_path)
        try:
            self.assertEqual(settings.SPEC_BY_KEY["autorun_transport"]["default"],
                             "webchat")
            self.assertEqual(settings.load(conn)["autorun_transport"], "webchat")
        finally:
            conn.close()
        self.assertEqual(autorun._auto_transport(self.cfg), "webchat")

    def test_api_when_configured(self):
        conn = db.connect(self.cfg.db_path)
        db.set_setting(conn, "autorun_transport", "api")
        conn.close()
        self.assertEqual(autorun._auto_transport(self.cfg), "api")


class StageParamsTests(Base):
    def test_transport_passed_for_script_and_shots(self):
        p = autorun._stage_params(self.cfg, self.pid, "script", lambda m: None,
                                 transport="webchat")
        self.assertEqual(p.get("transport"), "webchat")
        self.assertIn("log", p)
        self.assertIn("cancel", p)
        # style has no web-chat job: no transport key
        s = autorun._stage_params(self.cfg, self.pid, "style", lambda m: None,
                                  transport="webchat")
        self.assertNotIn("transport", s)


class ScriptShotsRoutingTests(Base):
    def test_script_webchat_calls_script_job_not_api(self):
        with mock.patch.object(webstages, "script_job") as sj, \
             mock.patch.object(autorun.studio, "llm_generate",
                               side_effect=AssertionError("api used")):
            autorun._run_script(self.cfg, self.pid, transport="webchat",
                                log=lambda m: None, cancel=lambda: False)
        sj.assert_called_once()
        self.assertEqual(sj.call_args[0][1], self.pid)

    def test_shots_webchat_calls_shotlist_job(self):
        with mock.patch.object(webstages, "shotlist_job") as lj, \
             mock.patch.object(autorun.studio, "llm_generate",
                               side_effect=AssertionError("api used")):
            autorun._run_shots(self.cfg, self.pid, transport="webchat",
                               log=lambda m: None, cancel=lambda: False)
        lj.assert_called_once()


class PlanThumbsRoutingTests(Base):
    def test_auto_plan_webchat_uses_plan_job(self):
        logs = []
        with mock.patch.object(packplan, "plan_job") as pj, \
             mock.patch.object(packplan, "load_plan", side_effect=[
                 {}, {"title": "T", "status": "ready", "applied": False}]), \
             mock.patch.object(packplan, "apply_plan") as ap, \
             mock.patch.object(packplan, "run_plan_api",
                               side_effect=AssertionError("api used")):
            autorun._auto_plan(self.cfg, self.pid, None, logs.append,
                               transport="webchat")
        pj.assert_called_once()
        ap.assert_called_once()

    def test_auto_thumbnails_webchat_uses_concepts_job(self):
        pdir = autorun.studio.prod_dir(self.cfg, self.pid)
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "script.md").write_text("coins " * 30, "utf-8")
        with mock.patch.object(th, "concepts_job") as cj, \
             mock.patch.object(th, "run_concepts_api",
                               side_effect=AssertionError("api used")):
            autorun._auto_thumbnails(self.cfg, self.pid, None,
                                     lambda m: None, transport="webchat")
        cj.assert_called_once()


class WebchatPairTests(Base):
    def test_pair_comes_from_settings_and_last_options(self):
        import json
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.set_setting(conn, "webchat_last_options",
                       json.dumps({"zai": {"model": "5.3"}}))
        conn.close()
        w, j, o = autorun._webchat_pair(self.cfg)
        self.assertEqual((w, j), ("zai", "deepseek"))     # the defaults
        self.assertEqual(o["zai"]["model"], "5.3")
        conn = db.connect(self.cfg.db_path)
        settings.save(conn, {"autorun_writer": "deepseek",
                             "autorun_judge": "zai"})
        conn.close()
        w, j, _o = autorun._webchat_pair(self.cfg)
        self.assertEqual((w, j), ("deepseek", "zai"))

    def test_unknown_site_falls_back(self):
        conn = db.connect(self.cfg.db_path)
        db.set_setting(conn, "autorun_writer", "gone")
        conn.close()
        self.assertEqual(autorun._webchat_pair(self.cfg)[0], "zai")


if __name__ == "__main__":
    unittest.main()
