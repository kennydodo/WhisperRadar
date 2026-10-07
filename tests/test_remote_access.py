"""Phone access: the password gate for non-local requests, and the helpers
`serve --remote` relies on.

Run: python -m unittest tests.test_remote_access
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

from whisperradar import db, remote  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402

PHONE = {"REMOTE_ADDR": "100.101.102.103"}      # a tailnet address


class RemoteGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(wr_tmp.cleanup, self.tmp)
        cfg = load_config(ROOT / "config.yaml")
        cfg.db_path = Path(self.tmp.name) / "wr.db"
        cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(cfg.db_path)
        db.init_db(conn)
        conn.close()
        remote._failures.clear()
        self.cfg = cfg

    def client(self, password="s3cret"):
        env = {remote.PASSWORD_ENV: password} if password else {}
        patcher = mock.patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        if not password:
            os.environ.pop(remote.PASSWORD_ENV, None)
        return create_app(self.cfg).test_client()

    def test_this_pc_needs_no_password(self):
        c = self.client()
        self.assertEqual(c.get("/my-channels").status_code, 200)

    def test_remote_without_a_password_set_is_refused(self):
        c = self.client(password=None)
        r = c.get("/my-channels", environ_overrides=PHONE)
        self.assertEqual(r.status_code, 403)
        self.assertIn(b"WR_PASSWORD", r.data)
        r = c.get("/login", environ_overrides=PHONE)
        self.assertEqual(r.status_code, 403)

    def test_remote_is_sent_to_the_login_page(self):
        c = self.client()
        r = c.get("/my-channels", environ_overrides=PHONE)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login?next=", r.headers["Location"])
        r = c.post("/studio/1/images/stop", environ_overrides=PHONE)
        self.assertEqual(r.status_code, 401)

    def test_wrong_password_then_right_password(self):
        c = self.client()
        r = c.post("/login", data={"password": "nope", "next": "/my-channels"},
                   environ_overrides=PHONE)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(c.get("/my-channels",
                               environ_overrides=PHONE).status_code, 302)
        r = c.post("/login", data={"password": "s3cret", "next": "/my-channels"},
                   environ_overrides=PHONE)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], "/my-channels")
        self.assertEqual(c.get("/my-channels",
                               environ_overrides=PHONE).status_code, 200)

    def test_logout_closes_the_session(self):
        c = self.client()
        c.post("/login", data={"password": "s3cret"}, environ_overrides=PHONE)
        self.assertEqual(c.get("/my-channels",
                               environ_overrides=PHONE).status_code, 200)
        c.post("/logout", environ_overrides=PHONE)
        self.assertEqual(c.get("/my-channels",
                               environ_overrides=PHONE).status_code, 302)

    def test_changing_the_password_ends_old_sessions(self):
        c = self.client()
        c.post("/login", data={"password": "s3cret"}, environ_overrides=PHONE)
        with mock.patch.dict(os.environ, {remote.PASSWORD_ENV: "changed"}):
            self.assertEqual(c.get("/my-channels",
                                   environ_overrides=PHONE).status_code, 302)

    def test_a_local_proxy_cannot_skip_the_password(self):
        c = self.client()
        for header in ("X-Forwarded-For", "Tailscale-User-Login", "X-Real-IP"):
            r = c.get("/my-channels", headers={header: "100.64.0.9"})
            self.assertEqual(r.status_code, 302, header)

    def test_next_cannot_leave_the_site(self):
        c = self.client()
        for bad in ("//evil.example", "https://evil.example", "/\\evil"):
            r = c.post("/login", data={"password": "s3cret", "next": bad},
                       environ_overrides=PHONE)
            self.assertEqual(r.headers["Location"], "/", bad)

    def test_too_many_wrong_passwords_lock_the_client_out(self):
        c = self.client()
        for _ in range(remote.MAX_FAILURES):
            c.post("/login", data={"password": "x"}, environ_overrides=PHONE)
        r = c.post("/login", data={"password": "s3cret"},
                   environ_overrides=PHONE)
        self.assertEqual(r.status_code, 429)

    def test_static_files_load_on_the_login_page(self):
        c = self.client()
        self.assertEqual(c.get("/static/mobile.css",
                               environ_overrides=PHONE).status_code, 200)

    def test_every_page_links_the_phone_stylesheet(self):
        c = self.client()
        for path in ("/", "/watched", "/studio", "/finished", "/my-channels",
                     "/settings"):
            html = c.get(path).get_data(as_text=True)
            self.assertIn("mobile.css", html, path)


class HelperTests(unittest.TestCase):
    def test_loopback_hosts(self):
        for host in ("127.0.0.1", "::1", "localhost"):
            self.assertTrue(remote.is_loopback_host(host), host)
        for host in ("0.0.0.0", "100.64.0.1", "192.168.1.5"):
            self.assertFalse(remote.is_loopback_host(host), host)

    def test_tailscale_addresses_come_from_the_cli_and_must_be_tailnet_ips(self):
        out = mock.Mock(stdout="100.101.102.103\n192.168.0.4\nnot-an-ip\n")
        with mock.patch.object(remote.shutil, "which", return_value="/x/tailscale"), \
                mock.patch.object(remote.subprocess, "run", return_value=out):
            self.assertEqual(remote.tailscale_addresses(), ["100.101.102.103"])

    def test_no_tailscale_means_no_addresses(self):
        with mock.patch.object(remote.shutil, "which", return_value=None), \
                mock.patch.object(remote.subprocess, "run",
                                  side_effect=OSError("missing")):
            self.assertEqual(remote.tailscale_addresses(), [])

    def test_without_the_cli_the_adapter_list_is_used(self):
        ipconfig = mock.Mock(stdout=(
            "Ethernet adapter Ethernet:\n   IPv4 Address. . . : 192.168.1.20\n"
            "Unknown adapter Tailscale:\n   IPv4 Address. . . : 100.88.12.34\n"
            "   Subnet Mask . . . : 255.255.255.255\n"))

        def fake_run(cmd, **kw):
            if cmd[0] == "ipconfig":
                return ipconfig
            raise OSError("no such command")

        with mock.patch.object(remote.shutil, "which", return_value=None), \
                mock.patch.object(remote.subprocess, "run", side_effect=fake_run):
            self.assertEqual(remote.tailscale_addresses(), ["100.88.12.34"])


if __name__ == "__main__":
    unittest.main()
