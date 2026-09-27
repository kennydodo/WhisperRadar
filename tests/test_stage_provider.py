"""Per-stage LLM override: a stage's pick wins there; otherwise the default.

Run: python -m unittest discover -s tests
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import autorun, db, studio, webapp  # noqa: E402
from whisperradar.config import load_config  # noqa: E402

DEFAULT = "deepseek-v4.1-flash"
CHOSEN = "mimo-v2.6-flash"
NAMES = {DEFAULT, CHOSEN, "glm-5.3-flash"}


class StageProviderTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(tempfile.mkdtemp()) / "wr.db"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.set_setting(conn, "llm_default", DEFAULT)
        self.pid = db.create_production(conn, "T", "general")
        conn.close()

    def _conn(self):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        return conn

    def test_store_and_clear_round_trip(self):
        conn = self._conn()
        try:
            db.set_stage_provider(conn, self.pid, "shots", CHOSEN)
            prod = db.get_production(conn, self.pid)
            self.assertEqual(db.stage_provider(prod, "shots"), CHOSEN)
            self.assertIsNone(db.stage_provider(prod, "script"))
            db.set_stage_provider(conn, self.pid, "shots", None)
            prod = db.get_production(conn, self.pid)
            self.assertIsNone(db.stage_provider(prod, "shots"))
        finally:
            conn.close()

    def test_resolution_order(self):
        with mock.patch("whisperradar.studio.provider_ready", return_value=True):
            # nothing saved -> default
            self.assertEqual(autorun._stage_provider(self.cfg, self.pid, "shots"),
                             DEFAULT)
            # explicit override wins
            self.assertEqual(
                autorun._stage_provider(self.cfg, self.pid, "shots",
                                        override=CHOSEN), CHOSEN)
            # a saved pick wins over the default ...
            conn = self._conn()
            db.set_stage_provider(conn, self.pid, "shots", CHOSEN)
            conn.close()
            self.assertEqual(autorun._stage_provider(self.cfg, self.pid, "shots"),
                             CHOSEN)
        # ... but ONLY while it is ready: a deleted provider must not come back
        with mock.patch("whisperradar.studio.provider_ready", return_value=False):
            self.assertEqual(autorun._stage_provider(self.cfg, self.pid, "shots"),
                             DEFAULT)

    def test_remember_stores_non_default_and_clears_default(self):
        with mock.patch("whisperradar.studio.provider_ready", return_value=True):
            webapp._remember_stage_provider(self.cfg, self.pid, "script", CHOSEN)
            conn = self._conn()
            prod = db.get_production(conn, self.pid)
            self.assertEqual(db.stage_provider(prod, "script"), CHOSEN)
            conn.close()
            # picking the Default LLM clears the override again
            webapp._remember_stage_provider(self.cfg, self.pid, "script", DEFAULT)
            conn = self._conn()
            prod = db.get_production(conn, self.pid)
            self.assertIsNone(db.stage_provider(prod, "script"))
            conn.close()

    def test_remember_ignores_unknown_provider(self):
        with mock.patch("whisperradar.studio.provider_ready", return_value=False):
            webapp._remember_stage_provider(self.cfg, self.pid, "shots", "ghost")
        conn = self._conn()
        prod = db.get_production(conn, self.pid)
        self.assertIsNone(db.stage_provider(prod, "shots"))
        conn.close()

    def test_stage_params_uses_the_stage_pick(self):
        conn = self._conn()
        db.set_stage_provider(conn, self.pid, "shots", CHOSEN)
        conn.close()
        with mock.patch("whisperradar.studio.provider_ready", return_value=True):
            params = autorun._stage_params(self.cfg, self.pid, "shots",
                                           lambda m: None)
        self.assertEqual(params["provider"], CHOSEN)
        # an explicit run-level override still wins
        with mock.patch("whisperradar.studio.provider_ready", return_value=True):
            params = autorun._stage_params(self.cfg, self.pid, "shots",
                                           lambda m: None, provider="glm-5.3-flash")
        self.assertEqual(params["provider"], "glm-5.3-flash")


class RoutePersistsPick(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(tempfile.mkdtemp()) / "wr.db"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.set_setting(conn, "llm_default", DEFAULT)
        self.pid = db.create_production(conn, "T", "general")
        conn.close()
        self.app = webapp.create_app(self.cfg)
        self.app.testing = True
        self.client = self.app.test_client()

    def test_autorun_without_override_keeps_the_stage_pick(self):
        """The auto-run modal's "Use each stage's own pick" default (empty
        provider field) must NOT force the global default onto a stage that
        has its own saved pick - that was the actual bug: the modal used to
        always pre-select and submit a provider, silently overriding every
        per-stage choice on every run."""
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.set_stage_provider(conn, self.pid, "shots", CHOSEN)
        conn.close()
        seen = {}

        def fake_run_stage(cfg, pid, stage, params=None):
            seen[stage] = (params or {}).get("provider")
            return "ok"

        def fake_stage_action(cfg, pid, stage):
            return {"stage": stage, "action": "run", "detail": ""}

        with mock.patch("whisperradar.studio.provider_ready", return_value=True), \
                mock.patch.object(autorun, "stage_action",
                                  side_effect=fake_stage_action), \
                mock.patch.object(autorun, "run_stage",
                                  side_effect=fake_run_stage):
            resp = self.client.post(f"/studio/{self.pid}/auto-run", data={})
            self.assertEqual(resp.status_code, 302)
            for _ in range(100):
                if not self.client.get("/studio/job").get_json()["running"]:
                    break
                time.sleep(0.05)
        # shots keeps its own saved pick, the other text stages keep the
        # global default - none of them were forced onto one provider
        self.assertEqual(seen.get("shots"), CHOSEN)
        self.assertEqual(seen.get("style"), DEFAULT)
        self.assertEqual(seen.get("script"), DEFAULT)

    def test_style_route_remembers_the_pick(self):
        with mock.patch("whisperradar.studio.provider_ready", return_value=True), \
                mock.patch.object(autorun, "run_stage", return_value="ok"):
            resp = self.client.post(f"/studio/{self.pid}/style/generate",
                                    data={"provider": CHOSEN})
            self.assertEqual(resp.status_code, 302)
            for _ in range(100):
                if not self.client.get("/studio/job").get_json()["running"]:
                    break
                time.sleep(0.05)
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        prod = db.get_production(conn, self.pid)
        self.assertEqual(db.stage_provider(prod, "style"), CHOSEN)
        conn.close()


if __name__ == "__main__":
    unittest.main()
