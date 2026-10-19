"""Exercise generic Send selection on local composer pages, without a service."""
import unittest

from tests.test_webchat_slider import _browser
from whisperradar import webchat


class SendControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.pw, cls.browser = _browser()
        except Exception as exc:
            raise unittest.SkipTest(f"no browser: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.site = webchat.site_from_def({
            "key": "test", "name": "Test", "url": "https://example.test",
            "box": "textarea"})

    def tearDown(self):
        self.page.close()

    def composer(self, buttons):
        self.page.set_content(
            '<form><textarea>Draft prompt</textarea>' + buttons + '</form>'
            '<script>window.clicked=[];document.querySelectorAll("button")'
            '.forEach(b=>b.onclick=e=>{e.preventDefault();clicked.push(b.id)})'
            '</script>')

    def test_disabled_send_does_not_click_voice_or_other_last_button(self):
        self.composer('<button id=send disabled aria-label="Send prompt"></button>'
                      '<button id=voice aria-label="Start voice mode"></button>'
                      '<button id=other type=button>More</button>')
        self.assertFalse(self.page.evaluate(self.site.send_js))
        self.assertEqual(self.page.evaluate('clicked'), [])

    def test_enabled_send_wins_over_microphone(self):
        self.composer('<button id=send aria-label="Send prompt"></button>'
                      '<button id=mic aria-label="Dictate"></button>')
        self.assertTrue(self.page.evaluate(self.site.send_js))
        self.assertEqual(self.page.evaluate('clicked'), ['send'])

    def test_voice_submit_and_mixed_attributes_are_excluded(self):
        self.composer('<button id=mic type=submit data-testid="send-button" '
                      'aria-label="Use microphone"></button>')
        self.assertFalse(self.page.evaluate(self.site.send_js))
        self.assertEqual(self.page.evaluate('clicked'), [])

    def test_unlabelled_real_submit_is_supported(self):
        self.composer('<button id=submit type=submit></button>'
                      '<button id=voice type=button aria-label="Voice"></button>')
        self.assertTrue(self.page.evaluate(self.site.send_js))
        self.assertEqual(self.page.evaluate('clicked'), ['submit'])

    def test_hidden_send_is_not_clicked(self):
        self.composer('<button id=send style="display:none" '
                      'aria-label="Send prompt"></button>'
                      '<button id=mic type=button aria-label="Microphone"></button>')
        self.assertFalse(self.page.evaluate(self.site.send_js))
        self.assertEqual(self.page.evaluate('clicked'), [])


if __name__ == '__main__':
    unittest.main()
