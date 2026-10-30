import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from whisperradar import db, openings, handover, external_prompts as ep, studio
from whisperradar.config import load_config
from whisperradar.webapp import create_app
from tests import wr_tmp


class OpeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(Path(__file__).resolve().parents[1] / 'config.yaml')
        self.cfg.db_path = Path(self.tmp.name) / 'wr.db'
        self.cfg.studio_dir = Path(self.tmp.name) / 'studio'
        self.conn = db.connect(self.cfg.db_path)
        db.init_db(self.conn)
        self.channel = db.create_own_channel(self.conn, 'The Ai Guy Channel')
        self.pid = db.create_production(self.conn, 'Why local models run slowly', 'AI')
        db.update_production(self.conn, self.pid, own_channel_id=self.channel)
        self.client = create_app(self.cfg).test_client()

    def tearDown(self):
        self.conn.close()
        wr_tmp.cleanup(self.tmp)

    def prod(self):
        return db.get_production(self.conn, self.pid)

    def test_defaults_are_channel_specific_and_migration_is_repeatable(self):
        db.init_db(self.conn)
        self.assertEqual(openings.defaults('Alicia Invest Replica'), ['purpose'])
        self.assertEqual(openings.defaults('To Live and More'), ['purpose'])
        self.assertEqual(openings.defaults('POV Finance Replica'), ['story'])
        self.assertEqual(openings.defaults('The Nature Made Us'), ['discovery'])
        self.assertEqual(openings.defaults('Steadier Mind'), ['reflect'])
        self.assertEqual(openings.channel_ids(db.get_own_channel(self.conn, self.channel)), ['explain', 'demo', 'compare'])

    def test_library_create_edit_duplicate_and_escape(self):
        response = self.client.post('/opening-styles/save', data=dict(name='<b>Custom</b>', instructions='Start with the mechanism.', example='Example'))
        self.assertEqual(response.status_code, 302)
        custom = [s for s in openings.catalog(self.conn) if not s['builtin']][0]
        self.assertIn(b'&lt;b&gt;Custom&lt;/b&gt;', self.client.get('/opening-styles').data)
        self.client.post('/opening-styles/save', data=dict(id=custom['id'], name='Edited', instructions='Start with a result.'))
        self.assertEqual([s for s in openings.catalog(self.conn) if not s['builtin']][0]['name'], 'Edited')
        response = self.client.post('/opening-styles/save', data=dict(id='explain', name='Bad', instructions='Overwrite standard'))
        self.assertIn('error=', response.location)
        self.assertEqual(self.client.post('/opening-styles/save', data=dict(name='My copy', instructions=openings.STANDARD[0]['instructions'])).status_code, 302)

    def test_channel_multiselect_is_validated_and_persists(self):
        url = f'/my-channels/{self.channel}/opening'
        self.client.post(url, data={'styles': ['explain', 'compare', 'explain']})
        self.assertEqual(openings.channel_ids(db.get_own_channel(self.conn, self.channel)), ['explain', 'compare'])
        self.assertIn('error=', self.client.post(url, data={'styles': ['missing']}).location)
        self.assertIn('error=', self.client.post(url, data={}).location)
        self.assertEqual(openings.channel_ids(db.get_own_channel(self.conn, self.channel)), ['explain', 'compare'])

    def test_get_does_not_freeze_but_first_prompt_does(self):
        self.assertEqual(self.client.get(f'/studio/{self.pid}/opening').status_code, 200)
        self.assertIsNone(self.prod()['opening_config'])
        handover.instruction(self.cfg, self.prod(), 'script')
        saved = self.prod()['opening_config']
        self.client.post(f'/my-channels/{self.channel}/opening', data={'styles': ['purpose']})
        self.assertEqual(self.prod()['opening_config'], saved)
        self.assertIn('[explain]', handover.instruction(self.cfg, self.prod(), 'script_judge'))

    def test_catalog_edits_do_not_change_frozen_production(self):
        sid = openings.save_custom(self.conn, 'Custom', 'Original instructions')
        db.update_own_channel(self.conn, self.channel, opening_styles=json.dumps([sid]))
        self.assertIn('Original instructions', handover.instruction(self.cfg, self.prod(), 'script'))
        openings.save_custom(self.conn, 'Custom', 'New instructions', style_id=sid)
        self.assertIn('Original instructions', handover.instruction(self.cfg, self.prod(), 'script'))
        self.client.post(f'/studio/{self.pid}/opening', data={'action': 'refresh'})
        self.assertIn('New instructions', handover.instruction(self.cfg, self.prod(), 'script'))

    def test_production_override_custom_and_reset(self):
        url = f'/studio/{self.pid}/opening'
        self.client.post(url, data={'styles': ['compare', 'explain'], 'chosen': 'compare', 'custom': 'Use our supplied comparison.'})
        policy = handover.instruction(self.cfg, self.prod(), 'script')
        self.assertIn('[compare]', policy)
        self.assertNotIn('[explain]', policy)
        self.assertIn('Use our supplied comparison.', policy)
        self.assertIn('error=', self.client.post(url, data={'styles': ['compare'], 'chosen': 'demo'}).location)
        self.client.post(url, data={'action': 'refresh'})
        self.assertEqual(openings.resolve(self.conn, self.prod())['custom'], '')

    def test_writer_choice_is_recorded_and_metadata_never_enters_narration(self):
        text = openings.capture(self.cfg, self.pid, '<!-- opening-style:explain -->\nActual narration.')
        self.assertEqual(text, 'Actual narration.')
        self.assertEqual(openings.resolve(self.conn, self.prod())['chosen'], 'explain')
        policy = handover.instruction(self.cfg, self.prod(), 'script_judge')
        self.assertNotIn('[demo]', policy)
        self.assertIn('[explain]', policy)
        with self.assertRaises(ValueError):
            openings.capture(self.cfg, self.pid, '<!-- opening-style:story -->\nWrong approach.')
        with self.assertRaises(ValueError):
            openings.capture(self.cfg, self.pid, '<!-- opening-style:demo -->\nChanged approach.')
        self.assertEqual(openings.capture(self.cfg, self.pid, 'No metadata.'), 'No metadata.')

    def test_writer_and_judge_prompts_override_conflicting_style(self):
        pdir = studio.prod_dir(self.cfg, self.pid)
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / 'writing_style.md').write_text('Always state the purpose immediately after the hook.', encoding='utf-8')
        for text in (ep.script_writer_prompt(self.cfg, self.pid), ep.script_judge_prompt(self.cfg, self.pid, 'A complete script.')):
            self.assertIn('SELECTED OPENING APPROACH', text)
            self.assertIn('takes priority over conflicting opening instructions', text)
            self.assertIn('[explain]', text)
            self.assertNotIn('CLEAR PURPOSE AFTER THE HOOK', text)
        self.assertEqual(handover.instruction(self.cfg, self.prod(), 'plan'), '')

    def test_missing_resources_and_empty_custom_are_rejected(self):
        self.assertEqual(self.client.get('/studio/999999/opening').status_code, 404)
        self.assertEqual(self.client.get('/my-channels/999999/opening').status_code, 404)
        self.assertIn('error=', self.client.post('/opening-styles/save', data={'name': 'Empty'}).location)

    def test_channels_and_studio_link_to_choices(self):
        with mock.patch.object(studio, 'renderly_channel_list', return_value=([], '', False)):
            self.assertIn(f'/my-channels/{self.channel}/opening'.encode(), self.client.get('/my-channels').data)


if __name__ == '__main__':
    unittest.main()
