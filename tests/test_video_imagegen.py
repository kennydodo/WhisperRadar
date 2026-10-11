"""The Python port of ImgToVideo.ImageGen (Renderly mode) against a fake Renderly backend."""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402,F401

from whisperradar.video import imagegen  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 2048


class FakeRenderly(BaseHTTPRequestHandler):
    calls = []
    quota_after = None            # fail every generation once this many were served
    served = 0

    def log_message(self, *a):
        pass

    def _json(self, code, doc):
        body = json.dumps(doc).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n) or b"{}")
        FakeRenderly.calls.append((self.path, payload))
        if self.path.endswith("/generate"):
            cls = FakeRenderly
            cls.served += 1
            if cls.quota_after is not None and cls.served > cls.quota_after:
                return self._json(200, {"id": cls.served, "name": payload["name"],
                                        "status": "failed",
                                        "error": "Gemini quota exceeded for this project"})
            return self._json(200, {"id": cls.served, "name": payload["name"], "status": "done",
                                    "image_url": f"/img/{cls.served}.png"})
        if "/upscale" in self.path:
            gid = int(self.path.split("/")[3])
            return self._json(200, {"id": gid, "name": "x", "status": "done",
                                    "image_url": f"/img/{gid}x.png"})
        self._json(404, {})

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(PNG)))
        self.end_headers()
        self.wfile.write(PNG)


def _project(tmp, files, extra=None):
    folder = os.path.join(tmp, "proj")
    os.makedirs(folder, exist_ok=True)
    doc = {"style": "STYLE", "shots": [{"cues": "1-3", "asset": f, "scene": "S01"}
                                         for f in files],
           "images": [{"file": f, "prompt": "prompt " + f} for f in files]}
    doc.update(extra or {})
    Path(folder, "shotlist.json").write_text(json.dumps(doc), encoding="utf-8")
    return folder


class ImageGen(unittest.TestCase):
    def setUp(self):
        FakeRenderly.calls = []
        FakeRenderly.quota_after = None
        FakeRenderly.served = 0
        self.srv = HTTPServer(("127.0.0.1", 0), FakeRenderly)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.client = imagegen.RenderlyClient(f"http://127.0.0.1:{self.srv.server_port}", 10)
        self.tmp = tempfile.TemporaryDirectory()
        self.lines = []

    def tearDown(self):
        self.srv.shutdown()
        self.tmp.cleanup()

    def run_it(self, folder, **kw):
        kw.setdefault("parallel", 1)
        return imagegen.run(folder, self.client, 7, say=self.lines.append, **kw)

    def test_generates_missing_and_skips_existing(self):
        folder = _project(self.tmp.name, ["S01_01_SCN_ST.png", "S01_02_SCN_PL.png"])
        os.makedirs(os.path.join(folder, "images"))
        Path(folder, "images", "S01_01_SCN_ST.png").write_bytes(PNG)
        self.assertEqual(self.run_it(folder), 0)
        self.assertTrue(Path(folder, "images", "S01_02_SCN_PL.png").is_file())
        path, payload = FakeRenderly.calls[0]
        self.assertEqual(path, "/api/channels/7/generate")
        self.assertEqual(payload["prompt"], "STYLE\n\nprompt S01_02_SCN_PL.png")
        self.assertEqual(payload["aspect_ratio"], "16:9")
        self.assertEqual(payload["name"], "S01_02_SCN_PL")
        self.assertTrue(any(l.startswith("[skip] S01_01_SCN_ST.png") for l in self.lines))
        self.assertTrue(Path(folder, "out", "image-review.html").is_file())

    def test_per_shot_aspect_motion_filter_and_upscale(self):
        folder = _project(self.tmp.name, ["S01_01_SCN_ST.png", "S01_02_SCN_PL.png"])
        os.makedirs(os.path.join(folder, "out"))
        Path(folder, "out", "image-batch.json").write_text(
            json.dumps({"images": [{"file": "s01_02_scn_pl.png", "aspect": "21:9"}]}))
        self.assertEqual(self.run_it(folder, motion_filter={"PL", "PR"}, upscale=2,
                                     image_size="2K"), 0)
        gens = [p for path, p in FakeRenderly.calls if path.endswith("/generate")]
        self.assertEqual(len(gens), 1)                       # ST was filtered out
        self.assertEqual(gens[0]["aspect_ratio"], "21:9")
        self.assertEqual(gens[0]["image_size"], "2K")
        self.assertTrue(any("/upscale" in path for path, _ in FakeRenderly.calls))
        self.assertFalse(Path(folder, "images", "S01_01_SCN_ST.png").exists())

    def test_quota_wall_stops_the_batch_with_exit_3(self):
        FakeRenderly.quota_after = 1
        folder = _project(self.tmp.name, [f"S01_0{i}_SCN_ST.png" for i in range(1, 5)])
        self.assertEqual(self.run_it(folder), 3)
        self.assertEqual(sum(1 for p, _ in FakeRenderly.calls if p.endswith("/generate")), 2)
        self.assertTrue(Path(folder, "images", "S01_01_SCN_ST.png").is_file())
        self.assertTrue(any("quota/billing" in l for l in self.lines))

    def test_dry_run_and_args(self):
        folder = _project(self.tmp.name, ["S01_01_SCN_ST.png"])
        self.assertEqual(self.run_it(folder, dry_run=True), 0)
        self.assertEqual(FakeRenderly.calls, [])
        self.assertEqual(imagegen.main([folder]), 2)           # no --renderly
        o = imagegen._parse_args([folder, "--renderly", "http://x/", "--channel", "3",
                                  "--upscale", "4", "--motion-filter", "pl, pr"])
        self.assertEqual((o["renderly"], o["channel"], o["upscale"], o["motion_filter"]),
                         ("http://x", 3, 4, {"PL", "PR"}))

    def test_helpers(self):
        self.assertEqual(imagegen.motion_suffix("S01_01_SCN_PU.png"), "PU")
        self.assertEqual(imagegen.size_text(2048 + 100), "2 KB")
        self.assertTrue(imagegen.is_quota_or_billing_error("Billing not enabled"))


if __name__ == "__main__":
    unittest.main()
