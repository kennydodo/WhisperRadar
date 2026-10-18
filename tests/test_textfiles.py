import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from whisperradar import textfiles
from tests.test_external_prompts import Base


class NewlineTests(unittest.TestCase):
    def test_windows_and_malformed_newlines_preserve_paragraphs(self):
        expected = 'First.\n\nSecond.\n'
        for text in ('First.\r\n\r\nSecond.\r\n', 'First.\r\r\n\r\r\nSecond.\r\r\n', expected):
            self.assertEqual(textfiles.normalize_newlines(text), expected)

    def test_repeated_save_never_grows_spacing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'script.md'
            expected = 'First.\n\nSecond.\n'
            path.write_bytes(expected.replace('\n', '\r\r\n').encode())
            for _ in range(3):
                browser = textfiles.read_text(path).replace('\n', '\r\n')
                textfiles.write_text(path, browser)
                self.assertEqual(path.read_bytes(), expected.encode())


class FormNewlineTests(Base):
    def test_script_and_shotlist_form_roundtrips_preserve_spacing(self):
        from whisperradar.webapp import create_app
        client = create_app(self.cfg).test_client()
        script = 'First paragraph.\n\nSecond paragraph.'
        shots = json.dumps({'images': []}, indent=2)
        with patch('whisperradar.autorun.coverage_gap', return_value='test plan incomplete'):
            for _ in range(3):
                client.post(f'/studio/{self.pid}/script/save', data={'script': script.replace('\n', '\r\n')})
                client.post(f'/studio/{self.pid}/shotlist/save', data={'shotlist': shots.replace('\n', '\r\n')})
                self.assertEqual((self.pdir / 'script.md').read_bytes(), (script + '\n').encode())
                self.assertEqual((self.pdir / 'shotlist.json').read_bytes(), (shots + '\n').encode())
