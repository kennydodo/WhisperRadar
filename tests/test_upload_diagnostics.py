"""Style & bible image upload: the confirmation check, its diagnostics, and
what a failed refresh leaves behind. Fake pages and transports only - no
browser, no network, no model call.

Run: python -m unittest tests.test_upload_diagnostics
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402
from whisperradar import channel_look, db, webchat as wc  # noqa: E402
from whisperradar.config import load_config  # noqa: E402
from whisperradar.webapp import create_app  # noqa: E402

FILES = [f"/tmp/f{i:02d}.jpg" for i in range(1, 11)]
QUIET = {"inputs": 1, "input_files": 0, "big": 0, "small": 0, "blob": 0,
         "remove": 0, "progress": 0, "error": False}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class ComposerPage:
    """A chat page that shows only image previews (no file names in text).
    `thumbs` = visible previews of 40 px or more now; `after` = how many more
    appear on the 4th tick; `diag` = what the attachment facts report."""

    def __init__(self, clock, thumbs=2, after=0, diag=None, body="New chat"):
        self.clock, self.thumbs, self.after = clock, thumbs, after
        self.later = dict(QUIET if diag is None else diag)
        self.diag = dict(QUIET)          # quiet when the baseline is taken
        self.body, self.ticks = body, 0

    def evaluate(self, js, arg=None):
        if js is wc._THUMB_COUNT_JS:
            return self.thumbs
        if js is wc._UPLOAD_DIAG_JS:
            return self.diag
        return self.body

    def wait_for_timeout(self, ms):
        self.clock.t += ms / 1000.0
        self.ticks += 1
        if self.ticks == 4:
            self.thumbs += self.after
            self.diag = dict(self.later)


def wait(page, clk, files=FILES, limit=20, log=None):
    seen = wc._name_counts(page, files)
    wc._wait_for_uploads(page, files, clk, log or (lambda m: None),
                         seen=seen, limit=limit)


def failure(page, clk, files=FILES, limit=20, log=None) -> str:
    with mock.patch.object(wc, "_fix_failed_uploads"):
        try:
            wait(page, clk, files, limit, log)
        except wc.WebChatError as e:
            return str(e)
    raise AssertionError("expected the upload check to fail")


class ConfirmationTests(unittest.TestCase):
    def test_image_only_previews_confirm_when_all_arrive(self):
        clk = Clock()
        page = ComposerPage(clk, thumbs=2, after=10)
        wait(page, clk)
        self.assertEqual(page.thumbs, 12)

    def test_images_already_in_the_conversation_do_not_count(self):
        # 30 old images are on the page before the upload; none are new
        clk = Clock()
        page = ComposerPage(clk, thumbs=30, after=0)
        msg = failure(page, clk)
        self.assertIn("never showed up", msg)
        self.assertIn("no new image previews", msg)

    def test_old_images_do_not_hide_missing_new_ones(self):
        clk = Clock()
        page = ComposerPage(clk, thumbs=30, after=7)
        msg = failure(page, clk)
        self.assertIn("only 7 of 10 image previews appeared", msg)

    def test_the_old_message_prefix_is_kept(self):
        clk = Clock()
        msg = failure(ComposerPage(clk), clk)
        self.assertTrue(msg.startswith(
            "the attached file(s) never showed up in the chat: f01.jpg"))

    def test_all_images_still_required_before_sending(self):
        clk = Clock()
        page = ComposerPage(clk, thumbs=0, after=9)       # 9 of 10
        with mock.patch.object(wc, "_fix_failed_uploads"):
            with self.assertRaises(wc.WebChatError):
                wait(page, clk)

    def test_the_time_limit_is_not_extended_by_diagnostics(self):
        clk = Clock()
        failure(ComposerPage(clk), clk, limit=20)
        self.assertLess(clk.t, 25)


class DiagnosisTests(unittest.TestCase):
    def why(self, now, thumbs_now=2, base=None, n=10):
        seen = {"__thumbs__": 2, "__diag__": dict(base or QUIET)}
        return wc._diagnose_upload(n, seen, thumbs_now, now)

    def now(self, **kw):
        d = dict(QUIET)
        d.update(kw)
        return d

    def test_an_error_on_the_page(self):
        self.assertIn("upload error", self.why(self.now(error=True)))

    def test_an_error_that_was_already_there_is_not_blamed(self):
        base = dict(QUIET, error=True)
        self.assertNotIn("upload error",
                         self.why(self.now(error=True), base=base))

    def test_uploads_still_in_progress(self):
        self.assertIn("unfinished", self.why(self.now(progress=3)))

    def test_only_some_previews(self):
        self.assertIn("only 4 of 10", self.why(self.now(), thumbs_now=6))

    def test_unrecognised_attachment_elements(self):
        w = self.why(self.now(blob=10, small=10, remove=10))
        self.assertIn("may not recognise", w)
        self.assertIn("blob images 10", w)

    def test_nothing_appeared(self):
        self.assertIn("may not have reached the page", self.why(self.now()))

    def test_unreadable_state(self):
        self.assertIn("could not be read", self.why({}))

    def test_no_claim_of_a_selector_regression_or_server_fault(self):
        for now in (self.now(), self.now(blob=3), self.now(progress=1),
                    self.now(error=True), {}):
            w = self.why(now).lower()
            self.assertNotIn("selector", w)
            self.assertNotIn("regression", w)

    def test_messages_through_the_wait(self):
        for diag, want in (
                (dict(QUIET, error=True), "upload error"),
                (dict(QUIET, progress=2), "unfinished"),
                (dict(QUIET, blob=10, small=10), "may not recognise"),
                (QUIET, "no new image previews")):
            clk = Clock()
            msg = failure(ComposerPage(clk, diag=diag), clk)
            self.assertIn(want, msg)
            self.assertIn("[page: inputs=", msg)

    def test_it_is_logged_once_halfway(self):
        clk, logs = Clock(), []
        failure(ComposerPage(clk), clk, limit=20, log=logs.append)
        halfway = [m for m in logs if "still waiting" in m]
        self.assertEqual(len(halfway), 1)

    def test_the_message_holds_numbers_not_chat_text(self):
        clk = Clock()
        page = ComposerPage(clk, body="SECRET PRIVATE CHAT TEXT")
        msg = failure(page, clk)
        self.assertNotIn("SECRET", msg)

    def test_unreadable_diagnostics_never_crash(self):
        class Broken(ComposerPage):
            def evaluate(self, js, arg=None):
                if js is wc._UPLOAD_DIAG_JS:
                    raise RuntimeError("page closed")
                return super().evaluate(js, arg)
        clk = Clock()
        msg = failure(Broken(clk), clk)
        self.assertIn("could not be read", msg)
        self.assertEqual(wc._upload_diagnostics(Broken(clk)), {})

    def test_non_image_uploads_are_unchanged(self):
        clk = Clock()
        page = ComposerPage(clk)
        counts = wc._name_counts(page, ["/tmp/narration.txt"])
        self.assertNotIn("__diag__", counts)
        with mock.patch.object(wc, "_fix_failed_uploads"):
            with self.assertRaises(wc.WebChatError) as cm:
                wc._wait_for_uploads(page, ["/tmp/narration.txt"], clk,
                                     lambda m: None, seen=counts, limit=6)
        self.assertNotIn("[page:", str(cm.exception))


class FailedTransport:
    def ask(self, site, prompt, files=None, ready=None):
        raise wc.WebChatError("the attached file(s) never showed up in the "
                              "chat: f01.jpg - test")


class KeptResultsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.add_channel(conn, "Kai", "UCkai")
        conn.commit()
        conn.close()
        d = channel_look.look_dir(self.cfg, "UCkai")
        (d / "frames").mkdir(parents=True)
        for i in range(4):
            (d / "frames" / f"f{i}.jpg").write_bytes(b"old")
        (d / "style.md").write_text("OLD STYLE\n", "utf-8")
        (d / "bible.md").write_text("OLD BIBLE\n", "utf-8")
        (d / "meta.json").write_text(json.dumps(
            {"channel": "Kai", "sent": ["f0.jpg"]}), "utf-8")
        self.d = d

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def _kept(self):
        self.assertEqual((self.d / "style.md").read_text("utf-8"),
                         "OLD STYLE\n")
        self.assertEqual((self.d / "bible.md").read_text("utf-8"),
                         "OLD BIBLE\n")
        self.assertEqual(len(list((self.d / "frames").glob("*.jpg"))), 4)
        self.assertEqual(
            (self.d / "frames" / "f0.jpg").read_bytes(), b"old")

    def _fake_frames(self, videos, n, dest, log, download, grab):
        dest.mkdir(parents=True, exist_ok=True)
        out = []
        for i in range(4):
            (dest / f"new{i}.jpg").write_bytes(b"new")
            out.append({"file": f"new{i}.jpg", "video_id": "v"})
        return out

    def test_a_failed_refresh_keeps_the_previous_style_bible_and_frames(self):
        vids = [{"video_id": "v", "title": "T"}] * 3
        with mock.patch.object(channel_look, "pick_videos",
                               return_value=vids), \
             mock.patch.object(channel_look, "pick_transcribed",
                               return_value=[]), \
             mock.patch.object(channel_look, "collect_frames",
                               side_effect=self._fake_frames):
            with self.assertRaises(wc.WebChatError):
                channel_look.run(self.cfg, "UCkai", "chatgpt",
                                 FailedTransport(), lambda m: None,
                                 thumbs=None)
        self._kept()

    def test_a_failed_regenerate_keeps_the_previous_results(self):
        with mock.patch.object(channel_look, "videos_by_id", return_value=[]):
            with self.assertRaises(wc.WebChatError):
                channel_look.regenerate(self.cfg, "UCkai", "chatgpt",
                                        FailedTransport(), lambda m: None)
        self._kept()


class PageFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.add_channel(conn, "Kai", "UCkai")
        conn.commit()
        conn.close()
        app = create_app(self.cfg)
        self.client = app.test_client()
        fn = app.view_functions["look_page"]
        names = fn.__code__.co_freevars
        self.job = fn.__closure__[names.index("sjob")].cell_contents

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def _set(self, kind, error, running=False):
        self.job.running, self.job.kind, self.job.error = running, kind, error

    def test_the_failure_is_shown_on_the_page(self):
        self._set("style & bible (Kai)", "the attached file(s) never "
                  "showed up in the chat: f01.jpg - only 7 of 10 image "
                  "previews appeared")
        html = self.client.get("/watched/UCkai/look").get_data(as_text=True)
        self.assertIn("only 7 of 10 image previews appeared", html)

    def test_it_says_the_old_results_were_kept_when_there_are_some(self):
        d = channel_look.look_dir(self.cfg, "UCkai")
        d.mkdir(parents=True)
        (d / "meta.json").write_text("{}", "utf-8")
        self._set("style & bible (Kai)", "boom")
        html = self.client.get("/watched/UCkai/look").get_data(as_text=True)
        self.assertIn("kept", html)

    def test_another_jobs_failure_is_not_shown_here(self):
        self._set("topics in web chat (x)", "unrelated failure")
        html = self.client.get("/watched/UCkai/look").get_data(as_text=True)
        self.assertNotIn("unrelated failure", html)

    def test_another_channels_failure_is_not_shown_here(self):
        self._set("style & bible (Someone Else)", "other channel failure")
        html = self.client.get("/watched/UCkai/look").get_data(as_text=True)
        self.assertNotIn("other channel failure", html)

    def test_no_failure_is_shown_while_a_job_is_running(self):
        self._set("style & bible (Kai)", "stale", running=True)
        html = self.client.get("/watched/UCkai/look").get_data(as_text=True)
        self.assertNotIn("stale", html)

    def test_the_status_poll_carries_the_error(self):
        self._set("style & bible (Kai)", "upload failed")
        data = self.client.get("/watched/UCkai/look/status").get_json()
        self.assertIn("upload failed", data.get("error") or "")


if __name__ == "__main__":
    unittest.main()
