import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from whisperradar import webchat as wc, webstages, db
from whisperradar.config import load_config
from whisperradar.webapp import create_app
from tests import wr_tmp


class AccountTests(unittest.TestCase):
    def test_profiles_and_debugging_connections_are_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            pw = mock.Mock()
            with mock.patch.object(wc, 'cdp_alive', return_value=False), \
                    mock.patch.object(wc, 'profile_busy', return_value=False):
                for key in wc.DEEPSEEK_ACCOUNTS:
                    wc.launch_persistent(pw, wc.SITES[key], folder)
            paths = [call.args[0] for call in pw.chromium.launch_persistent_context.call_args_list]
            self.assertEqual(paths, [str(Path(folder)/key) for key in wc.DEEPSEEK_ACCOUNTS])
            endpoints = [wc.cdp_endpoint(k) for k in ('deepseek', *wc.DEEPSEEK_ACCOUNTS)]
            self.assertEqual(len(set(endpoints)), 3)

    def test_custom_site_reload_cannot_remove_account_profiles(self):
        wc.set_custom_sites([])
        for key in wc.DEEPSEEK_ACCOUNTS:
            self.assertIn(key, wc.BUILTIN_KEYS)
            self.assertIs(wc.SITES[key].prepare, wc.DEEPSEEK.prepare)

    def test_transport_keeps_options_and_chat_urls_separate(self):
        chat = mock.Mock()
        chat.ask.return_value = 'reply'
        transport = webstages.WebTransport(chat, lambda _: None, {
            'deepseek_writer': {'deepthink': False},
            'deepseek_judge': {'deepthink': True}})
        transport.ask('deepseek_writer', 'write', new_chat=True)
        transport.ask('deepseek_judge', 'review', new_chat=True)
        calls=chat.ask.call_args_list
        self.assertFalse(calls[0].kwargs['options']['deepthink'])
        self.assertTrue(calls[1].kwargs['options']['deepthink'])
        real=wc.WebChat('.')
        real.adopt('deepseek_writer', 'https://chat.deepseek.com/chat/s/writer')
        real.adopt('deepseek_judge', 'https://chat.deepseek.com/chat/s/judge')
        self.assertEqual(len(real.urls()), 2)
        self.assertNotEqual(*real.urls().values())

    def test_settings_show_both_sign_in_actions_and_forms_offer_accounts(self):
        tmp=tempfile.TemporaryDirectory()
        try:
            cfg=load_config(Path(__file__).resolve().parents[1]/'config.yaml')
            cfg.db_path=Path(tmp.name)/'wr.db'
            cfg.studio_dir=Path(tmp.name)/'studio'
            client=create_app(cfg).test_client()
            page=client.get('/settings').get_data(as_text=True)
            for key in wc.DEEPSEEK_ACCOUNTS:
                self.assertIn('/settings/webchat-sites/'+key+'/login', page)
            conn=db.connect(cfg.db_path)
            pid=db.create_production(conn, 'Separate accounts')
            conn.close()
            page=client.get(f'/studio/{pid}/plan').get_data(as_text=True)
            self.assertIn("ACCOUNTS.concat(SITES)", page)
            self.assertIn('deepseek_writer', page)
            self.assertIn('deepseek_judge', page)
        finally:
            wr_tmp.cleanup(tmp)


if __name__ == '__main__':
    unittest.main()
