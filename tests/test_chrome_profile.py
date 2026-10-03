"""Chrome Efficiency mode (Memory Saver + Energy Saver) is switched off in the
image engines' own automation profiles before they launch Chrome.

Run: python -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import chrome_profile, db, settings, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402


class TurnOffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.profile = Path(self.tmp.name) / "profile"
        self.profile.mkdir()
        self.state = self.profile / "Local State"

    def tearDown(self):
        self.tmp.cleanup()

    def _load(self):
        return json.loads(self.state.read_text(encoding="utf-8"))

    def test_sets_both_switches_off_and_keeps_everything_else(self):
        self.state.write_text(json.dumps(
            {"browser": {"x": 1}, "performance_tuning": {
                "high_efficiency_mode": {"state": 1},
                "battery_saver_mode": {"state": 2}}}), encoding="utf-8")
        self.assertEqual(chrome_profile.turn_off_efficiency_mode(self.profile),
                         "changed")
        d = self._load()
        self.assertEqual(d["browser"], {"x": 1})
        pt = d["performance_tuning"]
        self.assertEqual(pt["high_efficiency_mode"], {"state": 0})
        self.assertEqual(pt["battery_saver_mode"]["state"], 0)

    def test_second_call_changes_nothing(self):
        self.state.write_text("{}", encoding="utf-8")
        chrome_profile.turn_off_efficiency_mode(self.profile)
        before = self.state.read_text(encoding="utf-8")
        self.assertEqual(chrome_profile.turn_off_efficiency_mode(self.profile),
                         "already off")
        self.assertEqual(self.state.read_text(encoding="utf-8"), before)

    def test_the_shape_chrome_keeps_counts_as_already_off(self):
        # what Chrome itself wrote back into a real automation profile
        self.state.write_text(json.dumps(
            {"performance_tuning": {"battery_saver_mode": {"state": 0},
                                    "high_efficiency_mode": {"state": 0}}}),
            encoding="utf-8")
        self.assertEqual(chrome_profile.turn_off_efficiency_mode(self.profile),
                         "already off")

    def test_a_missing_local_state_is_created(self):
        self.assertEqual(chrome_profile.turn_off_efficiency_mode(self.profile),
                         "changed")
        self.assertEqual(self._load()["performance_tuning"]
                         ["battery_saver_mode"]["state"], 0)

    def test_no_profile_folder_is_left_alone(self):
        gone = Path(self.tmp.name) / "nope"
        self.assertEqual(chrome_profile.turn_off_efficiency_mode(gone),
                         "no profile")
        self.assertFalse(gone.exists())

    def test_a_profile_in_use_is_not_written(self):
        self.state.write_text("{}", encoding="utf-8")
        with mock.patch.object(chrome_profile, "_profile_in_use",
                               lambda p: True):
            self.assertEqual(
                chrome_profile.turn_off_efficiency_mode(self.profile),
                "in use")
        self.assertEqual(self.state.read_text(encoding="utf-8"), "{}")

    def test_broken_json_is_reported_not_overwritten(self):
        self.state.write_text("{not json", encoding="utf-8")
        out = chrome_profile.turn_off_efficiency_mode(self.profile)
        self.assertTrue(out.startswith("error"))
        self.assertEqual(self.state.read_text(encoding="utf-8"), "{not json")

    def test_no_temp_files_are_left_behind(self):
        chrome_profile.turn_off_efficiency_mode(self.profile)
        self.assertEqual(sorted(p.name for p in self.profile.iterdir()),
                         ["Local State"])


class ProfileResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_flowbatch_reads_settings_and_the_local_override(self):
        cfgdir = self.base / "config"
        cfgdir.mkdir()
        self.assertEqual(chrome_profile.flowbatch_profile(self.base),
                         self.base / "profile")
        (cfgdir / "settings.json").write_text(
            json.dumps({"paths": {"profileDir": "profile-75"}}),
            encoding="utf-8")
        self.assertEqual(chrome_profile.flowbatch_profile(self.base),
                         self.base / "profile-75")
        (cfgdir / "settings.local.json").write_text(
            json.dumps({"paths": {"profileDir": "mine"}}), encoding="utf-8")
        self.assertEqual(chrome_profile.flowbatch_profile(self.base),
                         self.base / "mine")


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = base / "wr.db"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        conn.close()
        self.repo = base / "fb"
        (self.repo / "profile").mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("WHISPERRADAR_NO_CHROME_PREFS", None)
        self.dirs = mock.patch.multiple(
            studio, flowbatch_dir=lambda c: self.repo)
        self.dirs.start()

    def tearDown(self):
        self.dirs.stop()
        self.env.stop()
        self.tmp.cleanup()

    def _set(self, value):
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        db.set_setting(conn, "chrome_efficiency_off", value)
        conn.close()

    def _off(self, profile):
        f = profile / "Local State"
        if not f.exists():
            return False
        pt = json.loads(f.read_text(encoding="utf-8")).get(
            "performance_tuning", {})
        return pt.get("battery_saver_mode", {}).get("state") == 0

    def test_defaults_to_on_and_hits_the_flowbatch_profile(self):
        said = []
        self.assertFalse(self._off(self.repo / "profile"))
        self.assertEqual(
            chrome_profile.apply(self.cfg, "flowbatch", said.append),
            "changed")
        self.assertTrue(self._off(self.repo / "profile"))
        self.assertEqual(len(said), 1)
        self.assertIn("Efficiency mode turned off", said[0])

    def test_only_flowbatch_opens_a_browser(self):
        for engine in ("renderly", "api", ""):
            self.assertIsNone(chrome_profile.apply(self.cfg, engine))
        self.assertFalse(self._off(self.repo / "profile"))

    def test_the_setting_off_touches_nothing(self):
        self._set("0")
        self.assertIsNone(chrome_profile.apply(self.cfg, "flowbatch"))
        self.assertFalse(self._off(self.repo / "profile"))

    def test_an_already_off_profile_says_nothing(self):
        chrome_profile.apply(self.cfg, "flowbatch")
        said = []
        self.assertEqual(
            chrome_profile.apply(self.cfg, "flowbatch", said.append),
            "already off")
        self.assertEqual(said, [])

    def test_the_test_suite_guard_blocks_edits(self):
        os.environ["WHISPERRADAR_NO_CHROME_PREFS"] = "1"
        self.assertIsNone(chrome_profile.apply(self.cfg, "flowbatch"))
        self.assertFalse(self._off(self.repo / "profile"))


class SettingTests(unittest.TestCase):
    def test_setting_exists_defaults_on_and_is_in_production_images(self):
        entry = settings.SPEC_BY_KEY["chrome_efficiency_off"]
        self.assertEqual((entry["type"], entry["default"]), ("bool", True))
        group = dict(settings.GROUPS)["Production & images"]
        self.assertIn("chrome_efficiency_off", group)


class LaunchHookTests(unittest.TestCase):
    """Every place an engine opens Chrome applies the profile edit first."""

    def setUp(self):
        self.cfg = load_config(ROOT / "config.yaml")
        self.tmp = tempfile.TemporaryDirectory()
        self.pdir = Path(self.tmp.name)
        self.calls = []
        self.patch = mock.patch.object(
            chrome_profile, "apply",
            lambda cfg, engine, say=None: self.calls.append(engine))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_flowbatch_prepare(self):
        with mock.patch.object(studio, "flowbatch_dir",
                               lambda c: self.pdir), \
                mock.patch("subprocess.run", side_effect=OSError("no node")):
            studio.run_flowbatch_prepare(self.cfg, self.pdir,
                                         self.pdir / "job.json")
        self.assertEqual(self.calls, ["flowbatch"])


if __name__ == "__main__":
    unittest.main()
