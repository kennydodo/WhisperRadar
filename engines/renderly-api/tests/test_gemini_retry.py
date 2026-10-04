"""Gemini 503 'high demand' is retried; quota errors are not."""
import unittest
from unittest import mock

import base  # noqa: F401
from services import gemini_client


class FakeResponse:
    class _Inline:
        data = b"img"

    class _Part:
        inline_data = None

    def __init__(self):
        part = self._Part()
        part.inline_data = self._Inline()
        self.parts = [part]


class Boom(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def client_failing(times, error):
    calls = {"n": 0}

    def generate_content(**kwargs):
        calls["n"] += 1
        if calls["n"] <= times:
            raise error
        return FakeResponse()

    client = mock.Mock()
    client.models.generate_content = generate_content
    return client, calls


class Retry(unittest.TestCase):
    def run_with(self, client):
        with mock.patch.object(gemini_client, "get_client", return_value=client), \
                mock.patch.object(gemini_client.time, "sleep") as sleep:
            return gemini_client.generate_image("p"), sleep

    def test_503_then_success(self):
        client, calls = client_failing(2, Boom(503, "503 UNAVAILABLE high demand"))
        data, sleep = self.run_with(client)
        self.assertEqual(data, b"img")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleep.call_count, 2)

    def test_503_gives_up_after_all_retries(self):
        client, calls = client_failing(99, Boom(503, "503 UNAVAILABLE"))
        with self.assertRaises(Boom):
            self.run_with(client)
        self.assertEqual(calls["n"], 1 + len(gemini_client.TRANSIENT_RETRY_DELAYS))

    def test_quota_is_not_retried(self):
        client, calls = client_failing(99, Boom(429, "429 RESOURCE_EXHAUSTED"))
        with self.assertRaises(gemini_client.QuotaExceededError):
            self.run_with(client)
        self.assertEqual(calls["n"], 1)

    def test_other_errors_not_retried(self):
        client, calls = client_failing(99, Boom(400, "400 INVALID_ARGUMENT"))
        with self.assertRaises(Boom):
            self.run_with(client)
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
