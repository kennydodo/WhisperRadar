"""Chat sites the user adds in Settings > Web chat LLMs."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402,F401

from whisperradar import db, webchat  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402

SITE = {"name": "My Chat", "url": "https://chat.example/", "box": "textarea",
        "reply": ".ai", "send": "#go", "generating": "#stop",
        "login_part": "login", "sent_part": "/c/",
        "models": [{"label": "Big", "open": "#m", "item": "Big model"},
                   {"label": "Small", "open": "#m", "item": "Small model"}],
        "level_label": "Thinking",
        "levels": [{"label": "Low", "item": "Low"},
                   {"label": "High", "item": "High"}],
        "toggles": [{"label": "DeepThink", "text": "DeepThink"},
                    {"label": "Web search", "text": "Search"}]}


class FakePage:
    def __init__(self):
        self.calls = []

    def evaluate(self, js, arg=None):
        self.calls.append(arg)
        return "opened" if (isinstance(arg, dict) and "open" in arg) \
            else "clicked"

    def wait_for_timeout(self, ms):
        pass


class Base(unittest.TestCase):
    def tearDown(self):
        webchat.set_custom_sites([])


class DefinitionTests(Base):
    def test_sanitize_makes_key_ids_and_port(self):
        defs, problems = webchat.sanitize_sites([SITE])
        self.assertEqual(problems, [])
        d = defs[0]
        self.assertEqual(d["key"], "my-chat")
        self.assertEqual([m["id"] for m in d["models"]], ["big", "small"])
        self.assertEqual(d["toggles"][1]["id"], "web-search")
        self.assertGreaterEqual(d["port"], 9230)

    def test_name_and_address_are_enough(self):
        defs, problems = webchat.sanitize_sites(
            [{"name": "Plain", "url": "https://plain.example/chat"}])
        self.assertEqual(problems, [])
        webchat.set_custom_sites(defs)
        site = webchat.SITES["plain"]
        self.assertEqual(site.box, "textarea")
        self.assertIn("assistant", site.reply)
        self.assertIn("Stop", site.generating_js)
        self.assertIn("/chat", site.sent_js)       # start page = not started

    def test_bad_or_builtin_entries_are_rejected(self):
        defs, problems = webchat.sanitize_sites(
            [{"name": "x", "url": "chat.example"},        # no https://
             dict(SITE, name="zai"),                      # builtin key
             dict(SITE, name="Two"), dict(SITE, name="Two")])
        self.assertEqual([d["key"] for d in defs], ["two"])
        self.assertEqual(len(problems), 3)

    def test_registering_adds_and_removes_but_keeps_builtins(self):
        webchat.set_custom_sites([SITE])
        self.assertIn("my-chat", webchat.SITES)
        self.assertIn("my-chat", webchat.CDP_PORTS)
        webchat.set_custom_sites([])
        self.assertNotIn("my-chat", webchat.SITES)
        self.assertIn("zai", webchat.SITES)
        self.assertIn("deepseek", webchat.SITES)

    def test_prepare_applies_model_level_and_switches(self):
        webchat.set_custom_sites([SITE])
        page = FakePage()
        msg = webchat.SITES["my-chat"].prepare(
            page, model="small", level="high",
            toggles={"deepthink": True, "web-search": False})
        args = [a for a in page.calls if a is not None]
        self.assertIn("Small model", args)
        self.assertIn("High", args)
        self.assertIn({"text": "DeepThink", "want": True}, args)
        self.assertIn({"text": "Search", "want": False}, args)
        self.assertIn("Small", msg)
        self.assertIn("Web search off", msg)

    def test_no_model_asked_leaves_the_site_alone(self):
        webchat.set_custom_sites([SITE])
        page = FakePage()
        webchat.SITES["my-chat"].prepare(page, model=None, level=None,
                                         toggles={})
        self.assertFalse([a for a in page.calls
                          if isinstance(a, str)
                          or (isinstance(a, dict) and "open" in a)])

    def test_unfindable_picker_is_reported_not_fatal(self):
        webchat.set_custom_sites([SITE])

        class NoPicker(FakePage):
            def evaluate(self, js, arg=None):
                return "missing-picker" if (isinstance(arg, dict)
                                            and "open" in arg) else "missing"
        msg = webchat.SITES["my-chat"].prepare(NoPicker(), model="big")
        self.assertIn("picker not found", msg)


class AppTests(Base):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = t / "wr.db"
        self.cfg.studio_dir = t / "studio"
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        super().tearDown()
        self.tmp.cleanup()

    def save(self, sites):
        return self.client.post("/settings/webchat-sites",
                                data={"webchat_json": json.dumps(sites)})

    def test_saving_a_new_site_opens_its_sign_in_browser(self):
        from unittest import mock
        import time
        with mock.patch.object(webchat, "start_chrome") as chrome:
            r = self.save([{"name": "Fresh", "url": "https://fresh.example/"}])
            time.sleep(0.3)
            self.assertIn("browser%20window", r.headers["Location"])
            chrome.assert_called_once()
            self.assertEqual(chrome.call_args[0][0], "fresh")
            chrome.reset_mock()
            self.save([{"name": "Fresh", "url": "https://fresh.example/"}])
            time.sleep(0.3)
            chrome.assert_not_called()          # only NEW sites open it
            self.client.post("/settings/webchat-sites/fresh/login")
            time.sleep(0.3)
            chrome.assert_called_once()

    def test_save_persists_and_activates(self):
        r = self.save([SITE])
        self.assertEqual(r.status_code, 302)
        self.assertIn("msg=", r.headers["Location"])
        self.assertIn("my-chat", webchat.SITES)
        conn = db.connect(self.cfg.db_path)
        saved = json.loads(db.get_setting(conn, "webchat_sites"))
        conn.close()
        self.assertEqual(saved[0]["key"], "my-chat")
        page = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("Web chat LLMs", page)
        self.assertIn("My Chat", page)

    def test_bad_json_and_problems_are_reported(self):
        r = self.client.post("/settings/webchat-sites",
                             data={"webchat_json": "{nope"})
        self.assertIn("error=", r.headers["Location"])
        r = self.save([{"name": "x", "url": "not a url"}])
        self.assertIn("error=", r.headers["Location"])

    def test_run_forms_offer_the_new_site(self):
        self.save([SITE])
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        pid = db.create_production(conn, "T")
        conn.close()
        for url in (f"/studio/{pid}/kit", f"/studio/{pid}/plan",
                    f"/studio/{pid}/thumbnails", f"/studio/{pid}"):
            html = self.client.get(url).get_data(as_text=True)
            self.assertIn('"key": "my-chat"', html, url)

    def test_form_fields_reach_the_run_as_options(self):
        import time
        from unittest import mock
        from whisperradar import webstages
        self.save([SITE])
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        pid = db.create_production(conn, "T")
        conn.close()
        seen = {}
        with mock.patch.object(
                webstages, "script_job",
                side_effect=lambda *a, **k: seen.setdefault("a", a)):
            r = self.client.post(f"/studio/{pid}/webchat/script", data={
                "writer": "my-chat", "judge": "deepseek",
                "wc_my-chat_model": "small", "wc_my-chat_level": "high",
                "wc_my-chat_deepthink": "on", "wc_my-chat_web-search": "off"})
            time.sleep(0.5)
        self.assertEqual(r.status_code, 302)
        opts = seen["a"][-1]
        self.assertEqual(opts["my-chat"], {
            "model": "small", "level": "high",
            "toggles": {"deepthink": True, "web-search": False}})
        self.assertIn("zai", opts)

    def test_find_models_reads_and_saves_the_menu(self):
        import contextlib
        import time
        from unittest import mock
        from whisperradar import webstages
        self.save([{"name": "Fresh", "url": "https://fresh.example/"}])

        class Chat:
            def detect_models(self, key):
                return {"open": "#pick", "current": "Auto",
                        "items": ["Auto", "Think harder", "Fast"]}

        @contextlib.contextmanager
        def fake_transport(cfg, log, options=None):
            yield mock.Mock(chat=Chat())

        with mock.patch.object(webchat, "start_chrome"), \
             mock.patch.object(webstages, "web_transport", fake_transport):
            r = self.client.post("/settings/webchat-sites/fresh/models")
            time.sleep(0.6)
        self.assertIn("msg=", r.headers["Location"])
        ui = {u["key"]: u for u in webchat.custom_sites_ui()}
        self.assertEqual([m["label"] for m in ui["fresh"]["models"]],
                         ["Auto", "Think harder", "Fast"])
        self.assertEqual(ui["fresh"]["models"][0]["open"], "#pick")
        page = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("Think harder", page)
        # and the run forms offer them
        conn = db.connect(self.cfg.db_path)
        pid = db.create_production(conn, "T")
        conn.close()
        html = self.client.get(f"/studio/{pid}/plan").get_data(as_text=True)
        self.assertIn("Think harder", html)
        self.assertIn("zai_model", html)

    def test_run_forms_show_only_the_chosen_sites_controls(self):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        pid = db.create_production(conn, "T")
        conn.close()
        html = self.client.get(f"/studio/{pid}/plan").get_data(as_text=True)
        self.assertIn("BUILTIN", html)
        self.assertIn("sync()", html)

    def test_unknown_site_test_is_refused(self):
        r = self.client.post("/settings/webchat-sites/nope/test")
        self.assertIn("error=", r.headers["Location"])


if __name__ == "__main__":
    unittest.main()
