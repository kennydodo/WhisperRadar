"""The web chat browser keeps working while its window is minimised or covered.

Run: python -m unittest tests.test_webchat_background
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import webchat as wc  # noqa: E402


class LaunchFlagTests(unittest.TestCase):
    def _chrome_args(self):
        seen = {}

        def fake_popen(args, **kw):
            seen["args"] = args
            proc = mock.Mock()
            proc.poll.return_value = 1       # exits at once: start_chrome returns
            return proc

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(wc, "cdp_alive", return_value=False), \
                mock.patch.object(wc, "find_chrome", return_value="chrome.exe"), \
                mock.patch.object(wc.subprocess, "Popen", fake_popen):
            wc.start_chrome("zai", d)
        return seen["args"]

    def test_chrome_started_for_a_chat_ignores_minimising_and_covering(self):
        args = self._chrome_args()
        for flag in wc.BACKGROUND_SAFE_ARGS:
            self.assertIn(flag, args)
        feats = [a for a in args if a.startswith("--disable-features=")]
        self.assertEqual(len(feats), 1)    # Chrome honours only one
        self.assertIn("CalculateNativeWinOcclusion", feats[0])
        self.assertIn("ChromeWhatsNewUI", feats[0])

    def test_driven_window_gets_the_same_flags(self):
        captured = {}
        pw = mock.Mock()
        pw.chromium.launch_persistent_context.side_effect = (
            lambda path, **kw: captured.update(kw) or "ctx")
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(wc, "cdp_alive", return_value=False), \
                mock.patch.object(wc, "profile_busy", return_value=False):
            wc.launch_persistent(pw, wc.SITES["zai"], d)
        args = captured["args"]
        for flag in wc.BACKGROUND_SAFE_ARGS:
            self.assertIn(flag, args)
        self.assertEqual(
            [a for a in args if a.startswith("--disable-features=")],
            ["--disable-features=" + wc.BACKGROUND_DISABLED_FEATURES])


class KeepActiveTests(unittest.TestCase):
    def test_page_is_told_once_to_stay_focused_and_active(self):
        chat = wc.WebChat(tempfile.gettempdir())
        cdp = mock.Mock()
        ctx = mock.Mock()
        ctx.new_cdp_session.return_value = cdp
        page = mock.Mock()
        chat._keep_active(ctx, page)
        chat._keep_active(ctx, page)         # second call does nothing
        self.assertEqual(ctx.new_cdp_session.call_count, 1)
        sent = [c.args[0] for c in cdp.send.call_args_list]
        self.assertEqual(sent, ["Emulation.setFocusEmulationEnabled",
                                "Page.setWebLifecycleState"])

    def test_a_cdp_failure_is_harmless(self):
        chat = wc.WebChat(tempfile.gettempdir())
        ctx = mock.Mock()
        ctx.new_cdp_session.side_effect = RuntimeError("no cdp")
        chat._keep_active(ctx, mock.Mock())  # must not raise


if __name__ == "__main__":
    unittest.main()
