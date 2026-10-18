import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from whisperradar import script_fixes as sf, script_readiness as sr
from tests.test_external_prompts import Base


class FixTests(unittest.TestCase):
    def setUp(self):
        available = patch('whisperradar.cli_llm.resolve', return_value='fake-cli.exe')
        available.start()
        self.addCleanup(available.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.script = self.folder / 'script.md'
        self.original = 'A guaranteed outcome.\nKeep this sentence exactly.\nSubscribe.\n'
        self.script.write_bytes(self.original.encode())
        self.report = sr.ensure(self.folder)

    def result(self, replacement='A possible outcome.'):
        return {'changes': [{'line': 1, 'original': 'A guaranteed outcome.',
                             'replacement': replacement, 'reason': 'Qualify the claim.'}], 'notes': ''}

    def wait(self):
        for _ in range(200):
            job = sf.load(self.folder)
            if job['status'] != 'running':
                return job
            time.sleep(.01)
        self.fail('Worker did not finish')

    def start(self, result=None):
        with patch.object(sf, '_invoke', return_value=result or self.result()):
            sf.start(self.folder, self.report['script_hash'], 'all', 'codex', None, 1)
            return self.wait()

    def test_preview_does_not_edit_accept_backs_up_and_rescans(self):
        job = self.start()
        self.assertEqual(job['status'], 'ready')
        self.assertEqual(self.script.read_text(), self.original)
        sf.decide(self.folder, job['id'], True)
        self.assertEqual(self.script.read_text(), self.original.replace('A guaranteed outcome.', 'A possible outcome.'))
        self.assertEqual(next((self.folder / 'versions').glob('*.md')).read_text(), self.original)
        self.assertFalse(sr.pending(self.folder))
        with self.assertRaises(ValueError):
            sf.decide(self.folder, job['id'], True)

    def test_reject_and_stale_preview_leave_script_unchanged(self):
        job = self.start()
        self.script.write_text(self.original + 'A new ending.\n')
        with self.assertRaises(ValueError):
            sf.decide(self.folder, job['id'], True)
        sf.decide(self.folder, job['id'], False)
        self.assertEqual(self.script.read_text(), self.original + 'A new ending.\n')

    def test_reject_unselected_duplicate_or_inexact_changes(self):
        for field, value in [('line', 2), ('original', 'wrong'), ('line', True)]:
            result = self.result()
            result['changes'][0][field] = value
            with self.assertRaises(ValueError):
                sf.validate(result, self.original, {1})
        result = self.result()
        result['changes'] *= 2
        with self.assertRaises(ValueError):
            sf.validate(result, self.original, {1})

    def test_all_excludes_ctas_and_passes_complete_source(self):
        with patch.object(sf, '_invoke', return_value=self.result()) as invoke:
            sf.start(self.folder, self.report['script_hash'], 'all', 'codex', None, 1)
            self.wait()
        data = json.loads(invoke.call_args.args[1].split('SOURCE DATA:\n')[1])
        self.assertEqual(data['script'], self.original)
        self.assertEqual([a['line'] for a in data['allowed_lines']], [1])
        self.assertTrue(all(f['severity'] == 'warning' for f in data['selected_findings']))

    def test_individual_cta_can_be_selected(self):
        index = next(i for i, f in enumerate(self.report['findings']) if f['category'] == 'CTA')
        with patch.object(sf, '_invoke', return_value={'changes': [], 'notes': 'Keep this CTA.'}) as invoke:
            sf.start(self.folder, self.report['script_hash'], str(index), 'codex', None, 1)
            self.wait()
        self.assertIn('"category": "CTA"', invoke.call_args.args[1])

    def test_no_edits_with_explicit_false_alarm_verdict_clears_flag(self):
        result = {'changes': [], 'notes': 'Context makes this acceptable.', 'assessments': [
            {'finding': 0, 'verdict': 'not_an_issue', 'reason': 'Acceptable in context.'}]}
        job = self.start(result)
        self.assertEqual(job['status'], 'ready')
        self.assertFalse(sr.pending(self.folder))
        self.assertEqual(self.script.read_text(), self.original)

    def test_invalid_verdict_cannot_clear_other_findings(self):
        result = {'changes': [], 'notes': '', 'assessments': [
            {'finding': 99, 'verdict': 'not_an_issue', 'reason': 'Invalid target.'}]}
        self.assertEqual(self.start(result)['status'], 'failed')
        self.assertTrue(sr.pending(self.folder))

    def test_invalid_start_and_unavailable_provider(self):
        for hashed, selection, provider in [('stale', 'all', 'script'),
                                             (self.report['script_hash'], '-1', 'script'),
                                             (self.report['script_hash'], 'all', 'invalid')]:
            with self.assertRaises(ValueError):
                sf.start(self.folder, hashed, selection, provider, None, 1)
        with patch('whisperradar.cli_llm.resolve', return_value=None):
            with self.assertRaises(ValueError):
                sf.start(self.folder, self.report['script_hash'], 'all', 'claude', None, 1)

    def test_duplicate_running_job_and_failure(self):
        gate = threading.Event()
        def invoke(*_):
            gate.wait(3)
            raise RuntimeError('Sign in and retry.')
        with patch.object(sf, '_invoke', side_effect=invoke):
            sf.start(self.folder, self.report['script_hash'], 'all', 'codex', None, 1)
            with self.assertRaises(ValueError):
                sf.start(self.folder, self.report['script_hash'], 'all', 'codex', None, 1)
            gate.set()
            self.assertEqual(self.wait()['status'], 'failed')
        self.assertEqual(self.script.read_text(), self.original)

    def test_preserves_bom_crlf_and_unaffected_lines(self):
        raw = b'\xef\xbb\xbf' + self.original.replace('\n', '\r\n').encode()
        self.script.write_bytes(raw)
        job = self.start()
        sf.decide(self.folder, job['id'], True)
        self.assertEqual(self.script.read_bytes(), raw.replace(b'A guaranteed outcome.', b'A possible outcome.'))

    def test_provider_trailing_newline_does_not_add_blank_line(self):
        job = self.start(self.result('A possible outcome.\r\n'))
        sf.decide(self.folder, job['id'], True)
        self.assertEqual(self.script.read_text(), self.original.replace('A guaranteed outcome.', 'A possible outcome.'))

    def test_cli_commands_and_structured_results_without_real_calls(self):
        for provider in ('codex', 'claude'):
            def run(cmd, **kwargs):
                self.assertEqual(kwargs['input'], 'source prompt')
                self.assertEqual(kwargs['timeout'], 300)
                if provider == 'codex':
                    Path(cmd[cmd.index('-o') + 1]).write_text(json.dumps(self.result()))
                    self.assertIn('read-only', cmd)
                    self.assertIn('--ignore-user-config', cmd)
                else:
                    self.assertEqual(cmd[cmd.index('--tools') + 1], '')
                from types import SimpleNamespace
                return SimpleNamespace(returncode=0, stdout=json.dumps({'structured_output': self.result()}))
            with patch.object(sf.shutil, 'which', return_value='fake-cli.exe'), patch('whisperradar.cli_llm.check', return_value={'signed_in': True}), patch.object(sf.subprocess, 'run', side_effect=run):
                self.assertEqual(sf._invoke(provider, 'source prompt', None, 1), self.result())

    def test_old_api_selection_is_rejected_without_calling_provider(self):
        with patch.object(sf, '_invoke') as invoke:
            with self.assertRaisesRegex(ValueError, 'only Codex CLI or Claude Code'):
                sf.start(self.folder, self.report['script_hash'], 'all', 'script', None, 1)
            invoke.assert_not_called()
        with patch.object(sf.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                sf._invoke('script', 'prompt', None, 1)
            run.assert_not_called()

    def test_finds_bundled_codex_without_path(self):
        bundled = self.folder / 'OpenAI' / 'Codex' / 'bin' / 'installed-version' / 'codex.exe'
        bundled.parent.mkdir(parents=True)
        bundled.write_bytes(b'fake executable')
        with patch.object(sf.shutil, 'which', return_value=None), patch.object(sf.Path, 'home', return_value=self.folder), patch.dict(sf.os.environ, {'LOCALAPPDATA': str(self.folder)}):
            self.assertEqual(sf.executable('codex'), str(bundled))
            self.assertTrue(sf.available()['codex'])

    def test_finds_claude_native_install_without_path(self):
        binary = self.folder / '.local' / 'bin' / 'claude.exe'
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'fake executable')
        with patch.object(sf.shutil, 'which', return_value=None), patch.object(sf.Path, 'home', return_value=self.folder):
            self.assertEqual(sf.executable('claude'), str(binary))


class FixRouteTests(Base):
    def test_no_change_review_explains_remaining_flags_and_hides_accept(self):
        from whisperradar.webapp import create_app
        from whisperradar import cli_llm
        cli_llm.save(self.cfg, {'default': 'codex', 'codex_enabled': 'on', 'claude_enabled': 'on', 'show_fixes': 'on'})
        (self.pdir / 'script.md').write_text('Always succeeds.\n', encoding='utf-8')
        report = sr.ensure(self.pdir)
        sr._write(self.pdir / sf.FILE, {'id': 'no-edits', 'status': 'ready', 'provider': 'codex',
                  'script_hash': report['script_hash'], 'changes': [], 'notes': 'Evidence needed.'})
        page = create_app(self.cfg).test_client().get(f'/studio/{self.pid}?stage=script')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Review completed', page.data)
        self.assertIn(b'there is nothing to accept', page.data)
        self.assertIn(b'Dismiss review', page.data)
        self.assertNotIn(b'>Accept changes</button>', page.data)
        self.assertIn(b'class="review-actions"', page.data)
        sr.record_review(self.pdir, report['script_hash'], [{'finding': 0, 'verdict': 'not_an_issue', 'reason': 'Acceptable in context.'}])
        reviewed = create_app(self.cfg).test_client().get(f'/studio/{self.pid}?stage=script')
        self.assertIn(b'Review complete', reviewed.data)
        self.assertIn(b'no unresolved warnings', reviewed.data)
        self.assertNotIn(b'Script needs review before audio', reviewed.data)

    def test_controls_preview_and_accept_routes(self):
        from whisperradar.webapp import create_app
        from whisperradar import cli_llm
        cli_llm.save(self.cfg, {'default': 'codex', 'codex_enabled': 'on', 'claude_enabled': 'on', 'show_fixes': 'on'})
        client = create_app(self.cfg).test_client()
        script = self.pdir / 'script.md'
        script.write_text('Always succeeds.\nUnchanged.\n', encoding='utf-8')
        report = sr.ensure(self.pdir)
        result = {'changes': [{'line': 1, 'original': 'Always succeeds.',
                              'replacement': 'May succeed.', 'reason': '<unsafe>'}], 'notes': 'Review this.'}
        with patch('whisperradar.cli_llm.resolve', return_value='fake-cli.exe'), patch.object(sf, '_invoke', return_value=result):
            response = client.post(f'/studio/{self.pid}/script/warnings/fix', data={
                'script_hash': report['script_hash'], 'selection': '0', 'fix_provider': 'codex'})
            self.assertEqual(response.status_code, 302)
            for _ in range(200):
                job = sf.load(self.pdir)
                if job['status'] != 'running':
                    break
                time.sleep(.01)
        page = client.get(f'/studio/{self.pid}?stage=script')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Fix all warnings', page.data)
        self.assertNotIn(b'Configured script LLM', page.data)
        self.assertIn(b'Accept changes', page.data)
        self.assertIn(b'&lt;unsafe&gt;', page.data)
        self.assertEqual(client.get(f'/studio/{self.pid}/script/warnings/fix/status').json['status'], 'ready')
        client.post(f'/studio/{self.pid}/script/warnings/decide', data={'job_id': job['id'], 'action': 'accept'})
        self.assertEqual(script.read_text(), 'May succeed.\nUnchanged.\n')
        self.assertEqual(client.get('/studio/999999/script/warnings/fix/status').status_code, 404)
