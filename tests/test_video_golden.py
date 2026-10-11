"""Golden tests: the Python planner must reproduce the timeline.json / build-report.json that the
C# ImgToVideo tool produced for the fixture projects in tests/fixtures/video/<case>/."""
import json
import os
import re
import tempfile
import unittest

from whisperradar.video import plan as plan_mod
from tests import video_golden_support as g


class GoldenPlanTests(unittest.TestCase):
    def run_case(self, case):
        golden_tl = g.load_json(case, "timeline.json")
        golden_report = g.load_json(case, "build-report.json")
        audio_seconds = golden_tl["audio"]["duration_frames"] / golden_tl["fps"]
        with tempfile.TemporaryDirectory() as tmp:
            folder = g.make_project(case, tmp, golden_report["project"])
            result = plan_mod.plan_project(folder, audio_seconds=audio_seconds)
            self.assertTrue(result.success, [(i.code, i.message) for i in result.issues])
            with open(result.timeline_path, encoding="utf-8") as f:
                tl = json.load(f)
            with open(result.report_path, encoding="utf-8") as f:
                report = json.load(f)
        return tl, golden_tl, report, golden_report

    def check(self, case):
        tl, golden_tl, report, golden_report = self.run_case(case)
        # The goldens predate two C# changes: timeline.json used to omit an empty "sounds" list
        # (more_to_live), and the MANIFEST_TIMING_FIXED seconds were formatted with the machine culture
        # ("0,5s", "281,07s"); the current C# source formats it invariantly, as the port does.
        golden_tl.setdefault("sounds", [])
        for issue in golden_report["issues"]:
            if issue["code"] == "SHOT_TIMING":
                issue["message"] = re.sub(r"\((\d+),(\d+) s\)", r"(\1.\2 s)", issue["message"])
            if issue["code"] == "SHOT_HOLD_LONG":
                issue["message"] = re.sub(r"holds (\d+),(\d+)s", r"holds \1.\2s", issue["message"])
            if issue["code"] == "MANIFEST_TIMING_FIXED":
                issue["message"] = re.sub(r"(\d),(\d)", r"\1.\2", issue["message"])
        diff = g.first_difference(g.normalize_paths(tl), g.normalize_paths(golden_tl))
        self.assertIsNone(diff, f"timeline.json differs: {diff}")
        diff = g.first_difference(g.normalize_paths(report), g.normalize_paths(golden_report))
        self.assertIsNone(diff, f"build-report.json differs: {diff}")

    def test_fees(self):
        self.check("fees")

    def test_animals(self):
        self.check("animals")

    def test_more_to_live(self):
        self.check("more_to_live")


if __name__ == "__main__":
    unittest.main()
