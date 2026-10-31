"""Remotion backend: validation, cache keys, cache hits, Stop. No Node needed (a fake renderer stands in)."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from whisperradar.video import parsing  # noqa: E402
from whisperradar.video.explainer import prepare, recipes, remotion_backend as rb  # noqa: E402
from whisperradar.video.explainer import validate as va  # noqa: E402
from whisperradar.video.explainer.timing import compile_doc  # noqa: E402
from whisperradar.video.explainer.validate import ExplainerError  # noqa: E402

SRT = "".join(f"{i}\n00:00:{(i - 1) * 8:02d},000 --> 00:00:{i * 8:02d},000\nline {i}\n\n"
              for i in range(1, 5))
FAKE = """import sys, time
a = sys.argv
out = a[a.index("--out") + 1]
if "SLOW" in open(a[a.index("--props") + 1]).read() or True:
    pass
open(out, "wb").write(b"clip")
"""
SLOW = "import time\ntime.sleep(60)\n"


def _scene(**kw):
    s = {"id": "fit", "cue_range": [1, 4], "layout": "fit_chart", "title": "What fits?", "limit": 24,
         "bits": 4, "items": [{"label": "7B", "params_b": 7, "cue": 1}, {"label": "70B", "params_b": 70, "cue": 2}]}
    s.update(kw)
    return {"scenes": [s]}


def _build(**kw):
    return recipes.build(_scene(**kw), parsing.parse_srt(SRT))


def _err(**kw):
    try:
        _build(**kw)
    except ExplainerError as e:
        return " ".join(e.errors)
    return ""


class ValidationTests(unittest.TestCase):
    def test_builds_a_remotion_scene_without_objects(self):
        doc = _build()
        sc = doc["scenes"][0]
        self.assertEqual(sc["objects"], [])
        self.assertEqual(sc["remotion"]["component"], "fit_chart")
        self.assertEqual(sc["remotion"]["params"]["items"][1]["start_ms"], 8000)
        self.assertIsNotNone(va.parse_doc(doc).scenes[0].remotion)

    def test_bad_parameters_are_readable(self):
        self.assertIn("limit", _err(limit=0))
        self.assertIn("params_b", _err(items=[{"label": "7B", "cue": 1}]))
        self.assertIn("over 14", _err(items=[{"label": "x" * 15, "params_b": 7, "cue": 1}]))
        short = SRT.replace("00:00:24,000 --> 00:00:32,000", "00:00:24,000 --> 00:00:26,000")
        with self.assertRaises(ExplainerError) as cm:
            recipes.build(_scene(cue_range=[3, 4], items=[{"label": "7B", "params_b": 7, "cue": 4}]),
                          parsing.parse_srt(short))
        self.assertIn("starts too late", " ".join(cm.exception.errors))

    def test_unknown_component_and_stray_objects_rejected(self):
        d = _build()
        d["scenes"][0]["remotion"]["component"] = "evil"
        with self.assertRaises(ExplainerError) as cm:
            va.parse_doc(d)
        self.assertIn("unknown Remotion component", " ".join(cm.exception.errors))
        d = _build()
        d["scenes"][0]["objects"] = [{"id": "t", "type": "text", "text": "x", "box": [0, 0, .1, .1]}]
        with self.assertRaises(ExplainerError):
            va.parse_doc(d)


class CacheAndStopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.doc = va.parse_doc(_build())
        self.compiled = compile_doc(self.doc, parsing.parse_srt(SRT), 30)[0]

    def _script(self, body):
        p = os.path.join(self.tmp, "fake.py")
        with open(p, "w") as fh:
            fh.write(body)
        return [sys.executable, p]

    def test_renders_into_its_own_cache_then_hits(self):
        cmd = self._script(FAKE)
        path = rb.render_scene_clip(self.doc, self.compiled, self.tmp, 30, command=cmd)
        self.assertTrue(os.path.isfile(path))
        self.assertIn(os.path.join("out", "remotion"), path)
        self.assertNotIn(os.path.join("out", "explainer"), path)
        self.assertEqual(prepare.render_scene_clip.__module__, "whisperradar.video.explainer.prepare")
        again = rb.render_scene_clip(self.doc, self.compiled, self.tmp, 30, command=["definitely-not-run"])
        self.assertEqual(again, path)

    def test_params_and_size_change_the_key(self):
        a = rb.fingerprint(rb.scene_props(self.compiled, self.doc, 30), 1920, 1080, "p")
        d2 = va.parse_doc(_build(limit=16))
        c2 = compile_doc(d2, parsing.parse_srt(SRT), 30)[0]
        self.assertNotEqual(a, rb.fingerprint(rb.scene_props(c2, d2, 30), 1920, 1080, "p"))
        self.assertNotEqual(a, rb.fingerprint(rb.scene_props(self.compiled, self.doc, 30), 960, 540, "p"))
        self.assertNotEqual(a, rb.fingerprint(rb.scene_props(self.compiled, self.doc, 30), 1920, 1080, "q"))

    def test_scale_only_for_supported_sizes(self):
        self.assertEqual(rb.scale_for(960, 540), 0.5)
        with self.assertRaises(ExplainerError):
            rb.scale_for(1280, 720)

    def test_stop_ends_the_owned_process_and_leaves_no_clip(self):
        rb.GRACE_S = 2
        cmd = self._script(SLOW)
        ev = threading.Event()
        threading.Timer(0.6, ev.set).start()
        t = time.time()
        with self.assertRaises(RuntimeError) as cm:
            rb.render_scene_clip(self.doc, self.compiled, self.tmp, 30, cancel_event=ev, command=cmd)
        self.assertIn("cancelled", str(cm.exception))
        self.assertLess(time.time() - t, 10)
        left = [f for f in os.listdir(os.path.join(self.tmp, "out", "remotion")) if f.endswith(".mp4")]
        self.assertEqual(left, [])

    def test_failure_message_includes_the_tail(self):
        cmd = self._script("import sys\nprint('boom detail')\nsys.exit(3)\n")
        with self.assertRaises(RuntimeError) as cm:
            rb.render_scene_clip(self.doc, self.compiled, self.tmp, 30, command=cmd)
        self.assertIn("boom detail", str(cm.exception))


if __name__ == "__main__":
    unittest.main()


class MoreComponentsTests(unittest.TestCase):
    def _b(self, scene):
        base = {"id": "z", "cue_range": [1, 4]}
        base.update(scene)
        return recipes.build({"scenes": [base]}, parsing.parse_srt(SRT))

    def test_counter_builds_and_validates(self):
        d = self._b({"layout": "counter", "counter": {"from": 0, "to": 14, "unit": "GB",
                                                      "label": "7B at 16-bit", "cue": 2}})
        p = d["scenes"][0]["remotion"]["params"]
        self.assertEqual((p["to"], p["start_ms"]), (14, 8000))
        va.parse_doc(d)

    def test_counter_errors(self):
        with self.assertRaises(ExplainerError) as cm:
            self._b({"layout": "counter", "counter": {"from": 5, "to": 5}})
        self.assertIn("nothing would count", " ".join(cm.exception.errors))
        with self.assertRaises(ExplainerError):
            self._b({"layout": "counter", "counter": {"from": 0, "to": 9, "label": "x" * 60}})

    def test_screenshot_checks_and_inlines(self):
        d = self._b({"layout": "screenshot", "image": "assets/a.png",
                     "zoom": {"box": [0.1, 0.1, 0.4, 0.3], "cue": 2}, "caption": "look here"})
        va.parse_doc(d)
        for bad in ("../a.png", "/etc/a.png", "a.exe"):
            with self.assertRaises(ExplainerError):
                self._b({"layout": "screenshot", "image": bad, "zoom": {"box": [0, 0, .5, .5]}})
        with self.assertRaises(ExplainerError):
            self._b({"layout": "screenshot", "image": "a.png", "zoom": {"box": [0.8, 0, .5, .5]}})
        tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmp, "assets"))
        with open(os.path.join(tmp, "assets", "a.png"), "wb") as fh:
            fh.write(b"\x89PNG-data")
        doc = va.parse_doc(d)
        comp = compile_doc(doc, parsing.parse_srt(SRT), 30)[0]
        keyed, run = rb._prepare_assets(rb.scene_props(comp, doc, 30), tmp)
        self.assertTrue(run["params"]["image"].startswith("data:image/png;base64,"))
        self.assertEqual(keyed["params"]["image"]["path"], "assets/a.png")
        with open(os.path.join(tmp, "assets", "a.png"), "wb") as fh:
            fh.write(b"changed")
        keyed2, _ = rb._prepare_assets(rb.scene_props(comp, doc, 30), tmp)
        self.assertNotEqual(keyed["params"]["image"]["sha"], keyed2["params"]["image"]["sha"])
        with self.assertRaises(ExplainerError):
            rb._prepare_assets(rb.scene_props(comp, doc, 30), tempfile.mkdtemp())

    def test_fade_validated_and_in_the_cache_key(self):
        base = {"layout": "counter", "counter": {"from": 0, "to": 9, "unit": "x", "label": "n"}}
        d1, d2 = self._b(base), self._b(dict(base, fade={"in_ms": 400, "out_ms": 400}))
        k = []
        for d in (d1, d2):
            doc = va.parse_doc(d)
            comp = compile_doc(doc, parsing.parse_srt(SRT), 30)[0]
            k.append(rb.fingerprint(rb.scene_props(comp, doc, 30), 1920, 1080, "p"))
        self.assertNotEqual(*k)
        with self.assertRaises(ExplainerError):
            self._b(dict(base, fade={"in_ms": 9999}))


class DraftTests(unittest.TestCase):
    def test_cli_and_render_accept_draft(self):
        import inspect
        from whisperradar.video.explainer import render
        self.assertIn("draft", inspect.signature(render.render_video).parameters)


class RetimeTests(unittest.TestCase):
    def test_retime_makes_a_remotion_clip_match_the_pen_clips(self):
        import shutil
        import subprocess
        ff = shutil.which("ffmpeg")
        probe = shutil.which("ffprobe")
        if not ff or not probe:
            self.skipTest("ffmpeg and ffprobe are required")
        tmp = tempfile.mkdtemp()
        src, dst = os.path.join(tmp, "a.mp4"), os.path.join(tmp, "b.mp4")
        subprocess.run([ff, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=0x12161A:s=64x64:r=30:d=1",
                        "-vf", "scale=out_range=pc,format=yuvj420p", "-c:v", "libx264", "-video_track_timescale", "90000",
                        src], check=True)
        rb._retime(ff, src, dst)
        out = subprocess.run([probe, "-v", "error", "-select_streams", "v",
                              "-show_entries", "stream=pix_fmt,time_base,color_range", "-of", "csv=p=0", dst],
                             capture_output=True, text=True).stdout
        self.assertIn("yuv420p", out)
        self.assertNotIn("yuvj", out)
        self.assertIn("1/15360", out)
