import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from whisperradar import planning_audit as audit
from tests.test_beats import beat, make_cues


class SourceTests(unittest.TestCase):
    def test_one_changed_amount_warns_even_when_the_words_match(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "script.md").write_text("This monthly payment is only $520 and continues for years.")
            r = audit.source_report(p, "This monthly payment is only $530 and continues for years.")
            self.assertIn("numerical mismatch", " ".join(r["warnings"]))
    def test_content_mismatch_is_detected_independently_of_timestamps(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "script.md").write_text("A crocodile lives in a river.")
            r = audit.source_report(p, "A person buys a car on credit.")
            self.assertTrue(r["warnings"])
            (p / "script.md").write_text("A PERSON buys a car, on credit!")
            self.assertEqual(audit.source_report(p, "A person buys a car on credit.")["warnings"], [])

    def test_elements_provenance_detects_stale_or_unknown_source(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "elements.md").write_text("One person.")
            self.assertIn("unverified", audit.source_report(p, "A new story.")["warnings"][0])
            (p / "elements_source.json").write_text(json.dumps({"fingerprint": audit.fingerprint("An old story.")}))
            self.assertIn("different narration", audit.source_report(p, "A new story.")["warnings"][0])


class NumericalTests(unittest.TestCase):
    def data(self, text=None):
        return {"shots": [{"cues": "1-2", "asset": "a.png",
                           **({"overlays": [{"text": text}]} if text else {})}],
                "images": [{"file": "a.png", "prompt": "A blank payment sheet."}]}

    def cues(self):
        return [{"index": 1, "start": "00:00:00,000", "end": "00:00:04,000", "text": "The total is $43"},
                {"index": 2, "start": "00:00:04,000", "end": "00:00:08,000", "text": ",680 over 84 months."}]

    def test_auto_values_count_without_manual_duplicates(self):
        r = audit.numerical_report(self.data(), self.cues(), auto=True, overlays_enabled=True)
        self.assertEqual(r["faults"], [])
        self.assertIn("$43,680", r["entries"][0]["expected"])
        self.assertIn("$43,680", " ".join(r["entries"][0]["automatic"]))

    def test_disabled_auto_needs_explicit_renderer_values(self):
        r = audit.numerical_report(self.data(), self.cues(), overlays_enabled=True)
        self.assertIn("$43,680", r["faults"][0])
        explicit = audit.numerical_report(self.data("$43,680 total · 84 months"), self.cues(), overlays_enabled=True)
        self.assertEqual(explicit["faults"], [])

    def test_prohibited_numeric_image_policy_is_not_overridden(self):
        r = audit.numerical_report(self.data(), self.cues(), overlays_enabled=False)
        self.assertEqual(r["faults"], [])
        self.assertTrue(r["entries"][0]["missing"])


class PacingTests(unittest.TestCase):
    def test_short_shots_are_reported_without_changing_channel_minimum(self):
        cues = make_cues(1)
        data = {"shots": [{"cues": str(i), "asset": str(i)} for i in range(1, 7)]}
        profile = SimpleNamespace(min_hold=None)
        r = audit.pacing_report(data, cues, profile)
        self.assertEqual(r["under_3s"], 6)
        self.assertTrue(r["warnings"])
        self.assertIsNone(profile.min_hold)
