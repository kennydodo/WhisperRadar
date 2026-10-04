"""Settings normalization and the upscale API contract, against a throwaway DB."""
import unittest

import base  # noqa: F401  (sys.path + isolated DATABASE_URL)
from fastapi.testclient import TestClient

from routes.settings import normalize_level
from main import app


class NormalizeLevel(unittest.TestCase):
    def test_old_4x_value_migrates_to_the_4k_tier(self):
        self.assertEqual(normalize_level(4), 3)

    def test_known_levels_pass_through(self):
        self.assertEqual(normalize_level(0), 0)
        self.assertEqual(normalize_level(2), 2)

    def test_out_of_range_and_garbage_are_clamped(self):
        self.assertEqual(normalize_level(9), 3)
        self.assertEqual(normalize_level(-5), 0)
        self.assertEqual(normalize_level("x"), 0)
        self.assertEqual(normalize_level(None), 0)


class SettingsApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._client = TestClient(app)
        cls._client.__enter__()
        cls.client = cls._client

    @classmethod
    def tearDownClass(cls):
        cls._client.__exit__(None, None, None)

    def test_settings_shape(self):
        data = self.client.get("/api/settings").json()
        self.assertIsInstance(data["upscale_level"], int)
        self.assertIsInstance(data["auto_download"], bool)
        self.assertIn("download_dir", data)

    def test_roundtrip_and_legacy_migration(self):
        r = self.client.put("/api/settings", json={"upscale_level": 4})
        self.assertEqual(r.status_code, 200)
        data = self.client.get("/api/settings").json()
        self.assertEqual(data["upscale_level"], 3)  # stored 4x -> 4K tier

        self.client.put("/api/settings", json={"upscale_level": 1, "auto_download": False})
        data = self.client.get("/api/settings").json()
        self.assertEqual(data["upscale_level"], 1)
        self.assertIs(data["auto_download"], False)


class UpscaleTierContract(unittest.TestCase):
    """The API speaks HD/2K/4K, and the legacy scale request keeps working."""

    @classmethod
    def setUpClass(cls):
        cls._client = TestClient(app)
        cls._client.__enter__()
        cls.client = cls._client

    @classmethod
    def tearDownClass(cls):
        cls._client.__exit__(None, None, None)

    def test_tier_values_validate(self):
        # Validation runs before the DB lookup, so a 422 can only mean the tier
        # name was rejected; anything else proves it was accepted.
        for tier in ("HD", "2K", "4K"):
            r = self.client.post("/api/generations/999999/upscale", json={"tier": tier})
            self.assertNotEqual(r.status_code, 422, tier)

    def test_pre_rename_1k_tier_is_rejected(self):
        r = self.client.post("/api/generations/999999/upscale", json={"tier": "1K"})
        self.assertEqual(r.status_code, 422)

    def test_unknown_tier_is_rejected(self):
        r = self.client.post("/api/generations/999999/upscale", json={"tier": "8K"})
        self.assertEqual(r.status_code, 422)

    def test_legacy_scale_request_is_still_accepted(self):
        for body in ({"scale": 2}, {"scale": 4}):
            r = self.client.post("/api/generations/999999/upscale", json=body)
            self.assertNotEqual(r.status_code, 422, body)


if __name__ == "__main__":
    unittest.main()
