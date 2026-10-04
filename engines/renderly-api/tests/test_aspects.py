"""The backend's aspect-ratio contract (21:9 added for the wide shotlist work).

Validation happens before any DB lookup or Gemini call, so a bad ratio is a
422 and a good one is never a 422 - both checkable without spending a
generation. Runs against the throwaway DB configured by base.py.
"""
import unittest
from typing import get_args

import base  # noqa: F401  (sys.path + isolated DATABASE_URL)
from fastapi.testclient import TestClient
from pydantic import ValidationError

from config import ASPECT_RATIOS
from main import app
from routes.generate import AspectRatio, GenerateRequest

ORIGINAL = ("1:1", "16:9", "9:16", "4:3", "3:4")
ALL = ORIGINAL + ("21:9",)


class AspectRatioConfig(unittest.TestCase):
    def test_the_original_ratios_are_still_there(self):
        for ratio in ORIGINAL:
            self.assertIn(ratio, ASPECT_RATIOS)

    def test_21_9_was_added(self):
        self.assertIn("21:9", ASPECT_RATIOS)
        self.assertEqual(tuple(ASPECT_RATIOS), ALL)


class AspectRatioRequestModel(unittest.TestCase):
    def test_the_literal_accepts_exactly_the_supported_set(self):
        self.assertEqual(set(get_args(AspectRatio)), set(ALL))

    def test_every_supported_ratio_round_trips(self):
        for ratio in ALL:
            req = GenerateRequest(prompt="TEST", aspect_ratio=ratio)
            self.assertEqual(req.aspect_ratio, ratio)

    def test_an_unsupported_ratio_is_rejected(self):
        for bad in ("7:3", "21x9", "16/9", "", "wide"):
            with self.assertRaises(ValidationError, msg=bad):
                GenerateRequest(prompt="TEST", aspect_ratio=bad)


class AspectRatioApiBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._client = TestClient(app)
        cls._client.__enter__()
        cls.client = cls._client

    @classmethod
    def tearDownClass(cls):
        cls._client.__exit__(None, None, None)

    def test_21_9_is_accepted_before_the_channel_is_looked_up(self):
        # A 422 can only mean the ratio was refused - the missing channel would
        # be a 404/500, and no generation is ever attempted for it.
        r = self.client.post("/api/channels/999999/generate",
                             json={"prompt": "TEST", "aspect_ratio": "21:9"})
        self.assertNotEqual(r.status_code, 422)

    def test_an_unsupported_ratio_is_a_422(self):
        r = self.client.post("/api/channels/999999/generate",
                             json={"prompt": "TEST", "aspect_ratio": "7:3"})
        self.assertEqual(r.status_code, 422)

    def test_the_default_ratio_is_16_9(self):
        self.assertEqual(GenerateRequest(prompt="TEST").aspect_ratio, "16:9")


if __name__ == "__main__":
    unittest.main()
