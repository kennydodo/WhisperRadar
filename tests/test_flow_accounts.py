"""Offline coverage for opt-in Flow account ownership, queueing and UI."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from whisperradar import db, flow_accounts as fa, studio
from whisperradar.config import load_config
from whisperradar.webapp import create_app
from tests import wr_tmp

ROOT = Path(__file__).resolve().parents[1]

class FlowAccountsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / 'config.yaml')
        self.cfg.db_path = Path(self.tmp.name) / 'wr.db'
        self.cfg.studio_dir = Path(self.tmp.name) / 'studio'
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        self.pid = db.create_production(self.conn, 'Test')
        self.pdir = self.cfg.studio_dir / str(self.pid)

    def tearDown(self):
        fa.LOCAL.active = None
        fa.LOCAL.legacy = False
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def enable(self, value=True):
        db.set_setting(self.conn, 'flow_multi_account', '1' if value else '0')

    def run_job(self, callback=lambda: None):
        @fa.isolated
        def job(cfg, pid_dir, pid):
            return callback()
        return job(self.cfg, self.pdir, self.pid)

    def test_off_uses_legacy_command_without_binding(self):
        self.assertFalse(fa.enabled(self.cfg))
        self.assertEqual(self.run_job(lambda: studio._flowbatch_cmd(['generate'])),
                         ['node', 'src/cli.js', 'generate'])
        self.assertIsNone(fa.read_owner(self.pdir))
        with self.assertRaises(ValueError):
            fa.choose(self.cfg, self.pdir, self.pid, '2')

    def test_selection_precedence_and_legacy_project(self):
        self.enable()
        db.set_setting(self.conn, 'flow_default_account', '2')
        self.assertEqual(fa.selection(self.cfg, self.pdir, self.pid), '2')
        channel = db.create_own_channel(self.conn, 'Channel', flow_account='1')
        db.update_production(self.conn, self.pid, own_channel_id=channel)
        self.assertEqual(fa.selection(self.cfg, self.pdir, self.pid), '1')
        fa.choose(self.cfg, self.pdir, self.pid, '2')
        self.assertEqual(fa.selection(self.cfg, self.pdir, self.pid), '2')
        (self.pdir / fa.OWNER_FILE).unlink()
        db.update_production(self.conn, self.pid, flow_project_url='https://flow.test/project/old')
        self.assertEqual(fa.selection(self.cfg, self.pdir, self.pid), '1')
        with self.assertRaises(ValueError):
            fa.choose(self.cfg, self.pdir, self.pid, '2')

    def test_account_two_is_bound_and_cannot_resume_when_disabled(self):
        self.enable()
        fa.choose(self.cfg, self.pdir, self.pid, '2')
        flags = self.run_job(fa.command_flags)
        self.assertIn('--runtime-dir', flags)
        self.assertIn(str(fa.root(self.cfg) / '2' / 'profile'), flags)
        self.assertTrue(fa.read_owner(self.pdir)['bound'])
        with self.assertRaises(ValueError):
            fa.choose(self.cfg, self.pdir, self.pid, '1')
        self.enable(False)
        with self.assertRaisesRegex(RuntimeError, 'Enable multiple'):
            self.run_job()

    def test_account_one_retains_legacy_runtime_and_nested_jobs(self):
        self.enable()
        @fa.isolated
        def inner(cfg, pdir):
            return fa.command_flags()
        flags = self.run_job(lambda: inner(self.cfg, self.pdir))
        self.assertNotIn('--runtime-dir', flags)
        self.assertIn('--profile', flags)
        self.assertIsNone(getattr(fa.LOCAL, 'active', None))

    def test_tier_is_per_job_without_writing_shared_config(self):
        self.enable()
        with mock.patch.object(studio.subprocess, 'run') as subprocess_run:
            def render():
                studio.set_flowbatch_tier(self.cfg, 4)
                return studio._flowbatch_cmd(['generate'])
            self.assertEqual(self.run_job(render)[-1], '4k')
            self.assertEqual(self.run_job(lambda: studio._flowbatch_cmd(['recover']))[-1], 'off')
            subprocess_run.assert_not_called()

    def test_account_two_does_not_inherit_global_project(self):
        self.enable()
        self.cfg.flowbatch_project_url = 'https://flow.test/project/legacy'
        fa.choose(self.cfg, self.pdir, self.pid, '2')
        self.assertEqual(self.run_job(lambda: studio.flow_project_url_for(self.cfg, self.pid)), (None, 'none'))

    def test_file_lock_excludes_same_account_allows_other_and_cancels_queue(self):
        errors = []
        done = threading.Event()
        cancelled = threading.Event()
        with fa.lease(self.cfg, '1'):
            with self.assertRaises(RuntimeError):
                with fa.lease(self.cfg, '1', wait=False): pass
            with fa.lease(self.cfg, '2', wait=False): pass
            def waiter():
                try:
                    with fa.lease(self.cfg, '1', cancel=cancelled.is_set):
                        errors.append('entered busy account')
                except studio.BatchCancelled: pass
                finally: done.set()
            thread = threading.Thread(target=waiter)
            thread.start()
            cancelled.set()
            self.assertTrue(done.wait(3))
            thread.join()
        self.assertEqual(errors, [])
        with fa.lease(self.cfg, '1', wait=False): pass

    def test_failure_releases_account_and_production(self):
        self.enable()
        def fail(): raise RuntimeError('fake browser failure')
        with self.assertRaisesRegex(RuntimeError, 'fake browser'):
            self.run_job(fail)
        self.run_job()
        with fa.lease(self.cfg, '1', wait=False): pass

    def test_controls_and_routes_require_opt_in(self):
        with mock.patch('whisperradar.ai33.warm_cache'):
            client = create_app(self.cfg).test_client()
        html = client.get('/my-channels').get_data(as_text=True)
        self.assertNotIn('name="flow_account"', html)
        self.assertEqual(client.post('/flow-accounts/2/login').status_code, 400)
        self.enable()
        channel = db.create_own_channel(self.conn, 'Channel')
        html = client.get('/my-channels').get_data(as_text=True)
        self.assertIn('name="flow_account"', html)
        response = client.post(f'/studio/{self.pid}/flow-account', data={'flow_account': '2'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(fa.selection(self.cfg, self.pdir, self.pid), '2')
        html = client.get('/settings').get_data(as_text=True)
        self.assertIn('/flow-accounts/2/login', html)
        html = client.get(f"/studio/{self.pid}").get_data(as_text=True)
        self.assertIn("Save Flow account", html)
        with mock.patch.object(studio, 'flowbatch_ready', return_value=True), mock.patch('whisperradar.webapp.threading.Thread') as thread:
            with fa.lease(self.cfg, '2'):
                response = client.post('/flow-accounts/2/login')
            self.assertIn('busy', response.location)
            thread.assert_not_called()

    def test_queued_production_runs_after_release_and_other_account_runs_now(self):
        self.enable()
        p2 = db.create_production(self.conn, 'Queued')
        p3 = db.create_production(self.conn, 'Parallel')
        dir2, dir3 = self.cfg.studio_dir / str(p2), self.cfg.studio_dir / str(p3)
        fa.choose(self.cfg, dir3, p3, '2')
        queued = threading.Event()
        entered = threading.Event()
        parallel = threading.Event()
        failures = []
        @fa.isolated
        def job(cfg, pid_dir, pid, log=None):
            (parallel if pid == p3 else entered).set()
        def worker(pid, pdir):
            try: job(self.cfg, pdir, pid, log=lambda _: queued.set())
            except Exception as exc: failures.append(exc)
        with fa.lease(self.cfg, '1'):
            waiting = threading.Thread(target=worker, args=(p2, dir2))
            waiting.start()
            self.assertTrue(queued.wait(3))
            self.assertFalse(entered.is_set())
            other = threading.Thread(target=worker, args=(p3, dir3))
            other.start()
            self.assertTrue(parallel.wait(3))
            other.join(3)
        waiting.join(3)
        self.assertTrue(entered.is_set())
        self.assertEqual(failures, [])

    def test_standalone_prepare_resolves_channel_and_old_sidecars_pin_account_one(self):
        self.enable()
        channel = db.create_own_channel(self.conn, 'Account 2 channel', flow_account='2')
        db.update_production(self.conn, self.pid, own_channel_id=channel)
        self.assertEqual(fa.selection(self.cfg, self.pdir), '2')
        self.pdir.mkdir(parents=True)
        (self.pdir / 'flow_prepare.json').write_text('{"projectUrl":"old-project"}')
        self.assertEqual(fa.selection(self.cfg, self.pdir, self.pid), '1')
        with self.assertRaises(ValueError): fa.choose(self.cfg, self.pdir, self.pid, '2')

    def test_uninitialised_database_defaults_off(self):
        empty = Path(self.tmp.name) / 'empty.db'
        empty.touch()
        self.cfg.db_path = empty
        self.assertFalse(fa.enabled(self.cfg))

    def test_outdated_external_engine_cannot_silently_share_runtime(self):
        self.enable()
        self.cfg.flowbatch_repo = str(Path(self.tmp.name) / 'old-engine')
        with self.assertRaisesRegex(RuntimeError, 'updated bundled'):
            self.run_job()
        self.assertIsNone(fa.read_owner(self.pdir))
