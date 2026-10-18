import json
from types import SimpleNamespace
from unittest.mock import patch
from tests.test_external_prompts import Base
from whisperradar import cli_llm, db


class CliSettingsTests(Base):
    def test_defaults_and_settings_panel(self):
        from whisperradar.webapp import create_app
        data = cli_llm.load(self.cfg)
        self.assertEqual(data['default'], 'codex')
        self.assertTrue(data['codex']['enabled'])
        self.assertTrue(data['claude']['enabled'])
        page = create_app(self.cfg).test_client().get('/settings?tab=LLM&sub=codex')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Check GPT Codex connection', page.data)
        self.assertIn(b'Check Claude Code connection', page.data)
        self.assertNotIn(b'Codex LLM settings will appear', page.data)

    def test_save_persists_both_and_validates(self):
        cli_llm.save(self.cfg, {'default': 'claude', 'codex_enabled': 'on', 'claude_enabled': 'on',
                               'codex_model': 'test-codex', 'claude_model': 'sonnet'})
        data = cli_llm.load(self.cfg)
        self.assertEqual(data['default'], 'claude')
        self.assertEqual(data['codex']['model'], 'test-codex')
        self.assertEqual(data['claude']['model'], 'sonnet')
        with self.assertRaises(ValueError):
            cli_llm.save(self.cfg, {'default': 'script'})
        with self.assertRaises(ValueError):
            cli_llm.save(self.cfg, {'default': 'codex', 'codex_enabled': 'on', 'codex_path': 'missing.exe'})

    def test_subscription_checks_filter_account_details(self):
        for name, output in [('codex', 'Logged in using ChatGPT'),
                             ('claude', json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', 'email': 'private@example.com'}))]:
            with patch.object(cli_llm, 'resolve', return_value='fake.exe'), patch.object(cli_llm.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=output, stderr='')):
                result = cli_llm.check(self.cfg, name)
                self.assertTrue(result['signed_in'])
                self.assertNotIn('private@example.com', json.dumps(result))

    def test_api_auth_is_not_reported_as_subscription(self):
        with patch.object(cli_llm, 'resolve', return_value='fake.exe'), patch.object(cli_llm.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='Logged in using an API key', stderr='')):
            self.assertFalse(cli_llm.check(self.cfg, 'codex')['signed_in'])

    def test_script_fixes_use_saved_path_and_model_and_require_subscription(self):
        from whisperradar import script_fixes
        exe = self.pdir / 'codex.exe'
        exe.write_bytes(b'fake')
        cli_llm.save(self.cfg, {'default': 'codex', 'codex_enabled': 'on', 'claude_enabled': 'on',
                               'codex_path': str(exe), 'codex_model': 'test-model'})
        self.assertEqual(cli_llm.resolve(self.cfg, 'codex'), str(exe))
        def run(cmd, **kwargs):
            self.assertEqual(cmd[0], str(exe))
            self.assertEqual(cmd[cmd.index('--model') + 1], 'test-model')
            from pathlib import Path
            Path(cmd[cmd.index('-o') + 1]).write_text('{"changes":[],"notes":"","assessments":[]}', encoding='utf-8')
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        with patch.object(cli_llm, 'check', return_value={'signed_in': True}), patch.object(script_fixes.subprocess, 'run', side_effect=run):
            self.assertEqual(script_fixes._invoke('codex', 'test', self.cfg, self.pid)['changes'], [])
        with patch.object(cli_llm, 'check', return_value={'signed_in': False}), patch.object(script_fixes.subprocess, 'run') as process:
            with self.assertRaisesRegex(ValueError, 'subscription sign-in'):
                script_fixes._invoke('codex', 'test', self.cfg, self.pid)
            process.assert_not_called()

    def test_env_removes_api_keys(self):
        with patch.dict(cli_llm.os.environ, {'OPENAI_API_KEY': 'secret', 'ANTHROPIC_API_KEY': 'secret'}):
            env = cli_llm.environment()
            self.assertNotIn('OPENAI_API_KEY', env)
            self.assertNotIn('ANTHROPIC_API_KEY', env)

    def test_check_route_stores_sanitized_status(self):
        from whisperradar.webapp import create_app
        client = create_app(self.cfg).test_client()
        with patch.object(cli_llm, 'check', return_value={'installed': True, 'signed_in': True, 'message': 'Ready.'}):
            self.assertEqual(client.post('/settings/cli-llm/codex/check').status_code, 302)
        conn = db.connect(self.cfg.db_path)
        try:
            self.assertTrue(json.loads(db.get_setting(conn, 'cli_llm_checks'))['codex']['signed_in'])
        finally:
            conn.close()
        self.assertEqual(client.post('/settings/cli-llm/invalid/check').status_code, 404)
