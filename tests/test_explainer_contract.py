"""Focused tests for the explainer engine (Phase 2 contract).

Conventions (AGENTS.md): unittest, no paid APIs, render tests use real ffmpeg.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from whisperradar.video import parsing  # noqa: E402
from whisperradar.video.explainer import schema as sc  # noqa: E402
from whisperradar.video.explainer import timing as tg  # noqa: E402
from whisperradar.video.explainer import validate as va  # noqa: E402
from whisperradar.video.explainer.validate import ExplainerError  # noqa: E402

SRT = """1
00:00:00,000 --> 00:00:05,000
one

2
00:00:05,000 --> 00:00:09,000
two

3
00:00:09,000 --> 00:00:13,000
three
"""


def _cues():
    return parsing.parse_srt(SRT)


def _doc_dict(**overrides):
    doc = {
        "schema_version": 1,
        "theme": "finance_dark",
        "canvas": {"width": 640, "height": 360},
        "scenes": [{
            "id": "s1", "shot_id": "S01_01", "cue_range": [1, 2],
            "objects": [
                {"id": "t1", "type": "text", "text": "Hello", "box": [0.1, 0.1, 0.8, 0.2],
                 "style": "heading"},
                {"id": "c1", "type": "counter", "from": 0, "to": 400,
                 "format": "currency_usd", "box": [0.1, 0.5, 0.3, 0.1]},
            ],
            "events": [
                {"target": "t1", "action": "fade_in",
                 "at": {"cue": 1, "edge": "start"}, "duration_ms": 300},
                {"target": "c1", "action": "count",
                 "at": {"cue": 2, "edge": "start"}, "duration_ms": 900},
            ],
        }],
    }
    doc.update(overrides)
    return doc


class ValidatorTests(unittest.TestCase):
    def test_valid_doc_parses(self):
        doc = va.parse_doc(_doc_dict())
        self.assertEqual(len(doc.scenes), 1)
        self.assertEqual(doc.canvas, (640, 360))
        self.assertEqual(doc.scenes[0].cue_range, (1, 2))

    def test_wrong_schema_version_rejected(self):
        with self.assertRaises(ExplainerError):
            va.parse_doc(_doc_dict(schema_version=9))

    def test_v1_document_still_parses(self):
        doc = va.parse_doc(_doc_dict())
        self.assertEqual(doc.style, sc.STYLE_MOTION)   # default

    def test_whiteboard_style_selects_light_theme(self):
        data = _doc_dict()
        data["style"] = "whiteboard"
        data["theme"] = None       # no explicit theme: whiteboard gets light
        doc = va.parse_doc(data)
        self.assertEqual(doc.style, sc.STYLE_WHITEBOARD)
        self.assertEqual(doc.theme.name, "whiteboard_light")

    def test_whiteboard_style_keeps_explicit_theme(self):
        data = _doc_dict()
        data["style"] = "whiteboard"
        data["theme"] = "finance_dark"   # user picked it explicitly: honored
        doc = va.parse_doc(data)
        self.assertEqual(doc.style, sc.STYLE_WHITEBOARD)
        self.assertEqual(doc.theme.name, "finance_dark")

    def test_unknown_style_rejected(self):
        data = _doc_dict()
        data["style"] = "chalkboard"
        with self.assertRaises(ExplainerError) as ctx:
            va.parse_doc(data)
        self.assertTrue(any("style must be one of" in e
                            for e in ctx.exception.errors))

    def test_duplicate_object_id(self):
        data = _doc_dict()
        data["scenes"][0]["objects"].append(
            {"id": "t1", "type": "label", "text": "x", "box": [0, 0, 0.2, 0.1]})
        with self.assertRaises(ExplainerError) as ctx:
            va.parse_doc(data)
        self.assertTrue(any("duplicate object id" in e for e in ctx.exception.errors))

    def test_unknown_action(self):
        data = _doc_dict()
        data["scenes"][0]["events"][0]["action"] = "explode"
        with self.assertRaises(ExplainerError) as ctx:
            va.parse_doc(data)
        self.assertTrue(any("unknown action" in e for e in ctx.exception.errors))

    def test_unknown_target(self):
        data = _doc_dict()
        data["scenes"][0]["events"][0]["target"] = "ghost"
        with self.assertRaises(ExplainerError) as ctx:
            va.parse_doc(data)
        self.assertTrue(any("target must be an object id" in e
                            for e in ctx.exception.errors))

    def test_action_type_mismatch(self):
        data = _doc_dict()
        data["scenes"][0]["events"][0]["target"] = "c1"   # count cannot target counter... fade_in can
        data["scenes"][0]["events"][0]["action"] = "draw"  # draw -> path only
        with self.assertRaises(ExplainerError) as ctx:
            va.parse_doc(data)
        self.assertTrue(any("cannot target" in e for e in ctx.exception.errors))

    def test_box_out_of_canvas(self):
        data = _doc_dict()
        data["scenes"][0]["objects"][0]["box"] = [0.9, 0.1, 0.5, 0.2]
        with self.assertRaises(ExplainerError) as ctx:
            va.parse_doc(data)
        self.assertTrue(any("leaves the canvas" in e for e in ctx.exception.errors))

    def test_asset_traversal_blocked(self):
        data = _doc_dict()
        data["scenes"][0]["objects"].append(
            {"id": "img", "type": "image", "asset": "../secrets.png",
             "box": [0, 0.8, 0.2, 0.1]})
        with self.assertRaises(ExplainerError) as ctx:
            va.parse_doc(data, project_folder=tempfile.gettempdir())
        self.assertTrue(any("inside the project folder" in e
                            for e in ctx.exception.errors))

    def test_duplicate_property_events_rejected(self):
        data = _doc_dict()
        data["scenes"][0]["events"].append(
            {"target": "c1", "action": "count",
             "at": {"cue": 2, "edge": "start"}, "duration_ms": 100})
        doc = va.parse_doc(data)
        self.assertTrue(va.event_property_conflicts(doc))

    def test_cue_range_validation(self):
        data = _doc_dict()
        data["scenes"][0]["cue_range"] = [3, 2]
        with self.assertRaises(ExplainerError):
            va.parse_doc(data)


class TimingTests(unittest.TestCase):
    def test_cue_to_frames(self):
        doc = va.parse_doc(_doc_dict())
        compiled = tg.compile_doc(doc, _cues(), 30.0)
        cs = compiled[0]
        self.assertEqual(cs.start_frame, 0)
        self.assertEqual(cs.end_frame, 270)      # cue 2 end 9.0s * 30
        count = [e for e in cs.events if e.event.action == "count"][0]
        self.assertEqual(count.start_frame, 150)  # cue 2 start 5.0s * 30
        self.assertEqual(count.duration_frames, 27)  # 900ms -> 27 frames

    def test_negative_offset_supported(self):
        data = _doc_dict()
        data["scenes"][0]["cue_range"] = [1, 3]
        data["scenes"][0]["events"].append(
            {"target": "t1", "action": "fade_out",
             "at": {"cue": 3, "edge": "end"}, "offset_ms": -500,
             "duration_ms": 300})
        doc = va.parse_doc(data)
        compiled = tg.compile_doc(doc, _cues(), 30.0)
        fade = [e for e in compiled[0].events if e.event.action == "fade_out"][0]
        self.assertEqual(fade.start_frame, 375)   # 13.0s - 0.5s = 12.5 * 30
        self.assertEqual(fade.end_frame, 384)

    def test_event_past_scene_end_is_rejected(self):
        data = _doc_dict()
        data["scenes"][0]["cue_range"] = [1, 1]   # scene ends at 5 s
        data["scenes"][0]["events"][1]["at"] = {"cue": 2, "edge": "end"}
        doc = va.parse_doc(data)
        with self.assertRaises(ExplainerError) as ctx:
            tg.compile_doc(doc, _cues(), 30.0)
        self.assertTrue(any("past the scene end" in e
                            for e in ctx.exception.errors))

    def test_event_before_scene_start_is_rejected(self):
        data = _doc_dict()
        data["scenes"][0]["cue_range"] = [2, 3]   # scene starts at 5 s
        data["scenes"][0]["events"][0]["offset_ms"] = -10_000
        doc = va.parse_doc(data)
        with self.assertRaises(ExplainerError) as ctx:
            tg.compile_doc(doc, _cues(), 30.0)
        self.assertTrue(any("before the scene begins" in e
                            for e in ctx.exception.errors))

    def test_overlapping_scene_cue_ranges_rejected(self):
        data = _doc_dict()
        data["scenes"].append({
            "id": "s2", "shot_id": "S02_01", "cue_range": [2, 3],
            "objects": [{"id": "t2", "type": "label", "text": "S2",
                         "box": [0.1, 0.1, 0.5, 0.2]}],
            "events": []})
        doc = va.parse_doc(data)
        with self.assertRaises(ExplainerError) as ctx:
            tg.compile_doc(doc, _cues(), 30.0)
        self.assertTrue(any("must not overlap" in e
                            for e in ctx.exception.errors))

    def test_first_scene_pinned_to_zero_keeps_event_offsets(self):
        # cue 1 starts at 5 s in this SRT variant: the first scene is pinned
        # to frame 0, and its events must land relative to the FINAL range,
        # not the original cue start.
        srt_late = SRT.replace("00:00:00,000 --> 00:00:05,000",
                               "00:00:05,000 --> 00:00:10,000")
        srt_late = srt_late.replace(
            "00:00:05,000 --> 00:00:09,000", "00:00:10,000 --> 00:00:14,000")
        srt_late = srt_late.replace(
            "00:00:09,000 --> 00:00:13,000", "00:00:14,000 --> 00:00:18,000")
        # renumber times: scene uses cues [1,2]; cue1 now 5-10s, cue2 10-14s
        doc = va.parse_doc(_doc_dict())
        compiled = tg.compile_doc(doc, parsing.parse_srt(srt_late), 30.0,
                                  total_seconds=18.0)
        cs = compiled[0]
        self.assertEqual(cs.start_frame, 0)      # pinned
        self.assertEqual(cs.end_frame, 540)      # holds to the audio end (18 s)
        count = [e for e in cs.events if e.event.action == "count"][0]
        # cue 2 starts at 10.0s -> local 300 (NOT clamped early by the shift)
        self.assertEqual(count.start_frame, 300)

    def test_absent_cue_is_error(self):
        data = _doc_dict()
        data["scenes"][0]["cue_range"] = [1, 99]
        doc = va.parse_doc(data)
        with self.assertRaises(ExplainerError):
            tg.compile_doc(doc, _cues(), 30.0)

    def test_gap_fill_makes_scenes_contiguous(self):
        data = _doc_dict()
        data["scenes"].append({
            "id": "s2", "shot_id": "S02_01", "cue_range": [3, 3],
            "objects": [{"id": "t2", "type": "label", "text": "S2",
                         "box": [0.1, 0.1, 0.5, 0.2]}],
            "events": []})
        doc = va.parse_doc(data)
        compiled = tg.compile_doc(doc, _cues(), 30.0, total_seconds=14.0)
        self.assertEqual(compiled[0].end_frame, compiled[1].start_frame)
        self.assertEqual(compiled[1].end_frame, 420)  # 14.0s * 30

    def test_awkward_fps(self):
        doc = va.parse_doc(_doc_dict())
        compiled = tg.compile_doc(doc, _cues(), 23.976)
        cs = compiled[0]
        self.assertEqual(cs.end_frame, round(9.0 * 23.976))


class FormatterTests(unittest.TestCase):
    def test_currency(self):
        self.assertEqual(tg.__dict__ and
                         __import__("whisperradar.video.explainer.evaluator",
                                    fromlist=["formatter"]).formatter(
                             sc.FMT_CURRENCY_USD, 2400), "$2,400")

    def test_multiplier(self):
        fmt = __import__("whisperradar.video.explainer.evaluator",
                         fromlist=["formatter"]).formatter
        self.assertEqual(fmt(sc.FMT_MULTIPLIER, 6.2), "6.2x")

    def test_percent(self):
        fmt = __import__("whisperradar.video.explainer.evaluator",
                         fromlist=["formatter"]).formatter
        self.assertEqual(fmt(sc.FMT_PERCENT, 75), "75%")


if __name__ == "__main__":
    unittest.main()
