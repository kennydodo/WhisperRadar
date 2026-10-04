"""The favorites picker also lists the account's cloned voices.

GET /v3/favorites only returns starred voices, and voice clones are rarely
starred, so favorites mode now merges in GET /v3/voices?provider=clone
(ai33.cloned_voices), deduped by voice_id. A failed clone fetch must not
blank the favorites list, and favorites+clones both empty still falls back
to the shortlist/catalog. No network: ai33._request is patched.

Run: python -m unittest discover -s tests
"""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import ai33  # noqa: E402

STARRED = {"success": True, "favorites": [
    {"voice_id": "elevenlabs_abc", "provider": "v3",
     "voice_data": {"voice_id": "elevenlabs_abc", "name": "Mark"}},
    {"voice_id": "clone_2674277", "provider": "v3",
     "voice_data": {"voice_id": "clone_2674277", "name": "Stickly"}},
]}
CLONES = {"success": True, "data": [
    {"voice_id": "clone_2674277", "name": "Stickly"},
    {"voice_id": "clone_2674274", "name": "Animal Channel"},
]}
EMPTY = {"success": True, "favorites": []}


def make_request(favorites=STARRED, clones=CLONES, fail_clones=False):
    def request(url, api_key, **kw):
        if "/v3/favorites" in url:
            return json.dumps(favorites).encode()
        if "provider=clone" in url:
            if fail_clones:
                raise RuntimeError("HTTP 500 from clone catalog")
            return json.dumps({**clones,
                               "pagination": {"has_more": False}}).encode()
        return json.dumps({"data": [],
                           "pagination": {"has_more": False}}).encode()
    return request


class FavoritesClonesTests(unittest.TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(
            db_path=None, studio_ai33_api_key="k", studio_ai33_base_url=None,
            studio_ai33_voice_source="favorites", studio_ai33_voices=None)
        for cache in (ai33._favorites_cache, ai33._clones_cache,
                      ai33._voices_cache, ai33._curated_cache):
            cache["at"] = 0.0
            cache["data"] = []
        self._real_request = ai33._request
        ai33._request = make_request()

    def tearDown(self):
        ai33._request = self._real_request

    def test_config_favorites_mode_includes_clones(self):
        out = ai33.voices(self.cfg)
        ids = [v["voice_id"] for v in out]
        # starred clone_2674277 is deduped against the clone catalog
        self.assertEqual(ids, ["elevenlabs_abc", "clone_2674277",
                               "clone_2674274"])
        by = {v["voice_id"]: v for v in out}
        self.assertEqual(by["clone_2674274"]["provider"], "clone")
        self.assertEqual(by["clone_2674274"]["name"], "Animal Channel")

    def test_explicit_source_includes_clones(self):
        self.cfg.studio_ai33_voice_source = None
        out = ai33.voices(self.cfg, source="favorites")
        self.assertEqual(len(out), 3)

    def test_clone_failure_keeps_favorites(self):
        ai33._request = make_request(fail_clones=True)
        out = ai33.voices(self.cfg)
        self.assertEqual([v["voice_id"] for v in out],
                         ["elevenlabs_abc", "clone_2674277"])

    def test_both_empty_falls_back_to_catalog(self):
        ai33._request = make_request(favorites=EMPTY, clones={"data": []})
        self.assertEqual(ai33.voices(self.cfg), [])


if __name__ == "__main__":
    unittest.main()
