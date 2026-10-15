"""ai33 polling rides out 'Task polling temporarily busy' (HTTP 503)."""
import unittest
from unittest import mock

from whisperradar import ai33


class _Cfg:
    studio_ai33_api_key = "k"
    studio_ai33_base_url = None
    db_path = None


def _busy():
    return ai33.TransientError(
        'HTTP 503 from x: {"code":"server_busy"}', http=True)


class PollBusyTests(unittest.TestCase):
    def _run(self, replies, **kw):
        calls = []

        def fake_request(url, key, **k):
            calls.append(url)
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with mock.patch.object(ai33, "_request", fake_request), \
                mock.patch.object(ai33.time, "sleep"):
            out = ai33.generate(_Cfg(), "hi", "v", log=lambda m: None, **kw)
        return out, calls

    def test_poll_503_does_not_fail_an_accepted_task(self):
        replies = [
            b'{"success": true, "task_id": "T1"}',
            b'{"status": "doing"}', _busy(), _busy(),
            b'{"status": "done", "output_uri": "https://cdn/x.mp3"}',
        ]
        url, calls = self._run(replies)
        self.assertEqual(url, "https://cdn/x.mp3")
        self.assertEqual(sum("text-to-speech" in c for c in calls), 1,
                         "must not resubmit")

    def test_gives_up_after_many_busy_answers_and_names_the_task(self):
        replies = [b'{"success": true, "task_id": "T9"}'] + \
            [_busy() for _ in range(ai33.MAX_POLL_HICCUPS)]
        with self.assertRaises(RuntimeError) as cm:
            self._run(replies)
        self.assertIn("--task-id T9", str(cm.exception))

    def test_busy_submit_is_retried(self):
        replies = [_busy(), b'{"success": true, "task_id": "T2"}',
                   b'{"status": "done", "output_uri": "https://cdn/y.mp3"}']
        url, _ = self._run(replies)
        self.assertEqual(url, "https://cdn/y.mp3")

    def test_dropped_connection_on_submit_is_not_resent(self):
        replies = [ai33.TransientError("cannot reach", http=False)]
        with self.assertRaises(ai33.TransientError):
            self._run(replies)

    def test_resume_skips_the_submit(self):
        replies = [b'{"status": "done", "output_uri": "https://cdn/z.mp3"}']
        url, calls = self._run(replies, task_id="T7")
        self.assertEqual(url, "https://cdn/z.mp3")
        self.assertFalse(any("text-to-speech" in c for c in calls))

    def test_other_errors_still_fail_at_once(self):
        replies = [b'{"success": true, "task_id": "T3"}',
                   RuntimeError("HTTP 401 from x: bad key")]
        with self.assertRaises(RuntimeError):
            self._run(replies)


if __name__ == "__main__":
    unittest.main()
