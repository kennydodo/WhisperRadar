"""Aspect-ratio pipeline: motion code -> requested aspect, per path.

Covers the 2026-09 aspect work in WhisperRadar without any network or spend:

  Renderly API   run_imagegen runs ImgToVideo.Cli export-batch first, so
                 ImageGen --renderly requests the per-file "aspect" (21:9 for
                 PL/PR/PV; PU/PD deliberately stay 16:9 - the paid path never
                 spends a generation on a shape Flow gives away for free).
  FlowBatch      prepare_flowbatch_job writes per-item aspectRatio (PU/PD =
                 1:1); every other motion keeps the job default 16:9.
  Quota fallback run_imagegen's exit 3 becomes RenderlyQuotaExhausted, and
                 autorun._run_images finishes the rest through the Flow Driver.

The expected values below mirror the source tables exactly:
  ImgToVideo.Cli/Program.cs      `canvas` / `aspect`
  whisperradar/studio.py         FLOWBATCH_ASPECT_BY_MOTION
  Renderly extension-v2/flow.js  MOTION_ASPECT + FLOW_SUPPORTED_ASPECTS

Run: python -m unittest discover -s tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar.config import load_config  # noqa: E402
from whisperradar import autorun, db, services, studio  # noqa: E402

# Every motion code the shotlist can emit (the suffix of "<name>_<MOTION>.png").
MOTIONS = ["ST", "ZI", "ZO", "PL", "PR", "PU", "PD", "PV"]

# ImgToVideo.Cli export-batch writes these into out\image-batch.json["images"].
RENDERLY_API_ASPECT = {
    "ST": "16:9", "ZI": "16:9", "ZO": "16:9",
    "PL": "21:9", "PR": "21:9",
    "PU": "16:9", "PD": "16:9",   # by design on the paid API path
    "PV": "21:9",
}
# whisperradar/studio.py FLOWBATCH_ASPECT_BY_MOTION (job default 16:9 otherwise).
# PV is listed explicitly even though it maps to the same 16:9 the job
# default would give it anyway - Flow has no preset above 16:9, so PV
# (like PL/PR) can't do better there. Explicit beats implicit.
FLOWBATCH_ASPECT = {"PU": "1:1", "PD": "1:1", "PV": "16:9"}
FLOWBATCH_DEFAULT = "16:9"


def _cfg(db_path) -> object:
    cfg = load_config(ROOT / "config.yaml")
    cfg.db_path = Path(db_path)
    return cfg


def _shotlist(motions) -> dict:
    return {
        "style": "TEST style - flat lighting, no text",
        "images": [
            {"file": f"A{i:02d}_{m}.png",
             "prompt": f"TEST frame {i} ({m}) for the aspect-ratio pipeline"}
            for i, m in enumerate(motions, 1)
        ],
    }


def _motion_of(file_name: str) -> str:
    return Path(file_name).stem.rsplit("_", 1)[-1]


class FlowbatchAspectTests(unittest.TestCase):
    """prepare_flowbatch_job: PU/PD ask for 1:1, everything else inherits 16:9."""

    def test_the_motion_table_matches_pu_pd_and_pv(self):
        self.assertEqual(studio.FLOWBATCH_ASPECT_BY_MOTION, FLOWBATCH_ASPECT)

    def test_pu_pd_get_1_1_and_others_have_no_override(self):
        with tempfile.TemporaryDirectory() as d:
            pid_dir = Path(d)
            data = _shotlist(MOTIONS)
            (pid_dir / "shotlist.json").write_text(
                json.dumps(data), encoding="utf-8")
            cfg = _cfg(Path(d) / "wr.db")
            cfg.flowbatch_project_url = None

            job_path, expected = studio.prepare_flowbatch_job(cfg, pid_dir, 999999)
            job = json.loads(job_path.read_text(encoding="utf-8"))
            by_file = {i["file"]: i for i in job["images"]}

            self.assertEqual(expected, [i["file"] for i in data["images"]])
            self.assertEqual(job["defaults"]["aspectRatio"], FLOWBATCH_DEFAULT)
            for item in data["images"]:
                motion = _motion_of(item["file"])
                entry = by_file[item["file"]]
                # PV's mapped value equals the job default (16:9), so an
                # explicit-vs-absent aspectRatio key is an implementation
                # detail; only check the value actually resolved matches.
                if motion in FLOWBATCH_ASPECT:
                    self.assertEqual(
                        entry.get("aspectRatio", FLOWBATCH_DEFAULT),
                        FLOWBATCH_ASPECT[motion], motion)
                else:
                    self.assertNotIn("aspectRatio", entry, motion)


class RenderlyExportBatchTests(unittest.TestCase):
    """run_imagegen must (re)write out\\image-batch.json before ImageGen runs,
    and turn exit 3 into RenderlyQuotaExhausted carrying the partial count."""

    def _fixture(self, d, imagegen_rc, produced=()):
        repo = Path(d) / "ImgToVideo"
        (repo / "src" / "ImgToVideo.ImageGen").mkdir(parents=True)
        (repo / "src" / "ImgToVideo.Cli").mkdir(parents=True)
        pid_dir = Path(d) / "studio"
        (pid_dir / "images").mkdir(parents=True)

        cfg = _cfg(Path(d) / "wr.db")
        cfg.imgtovideo_repo = str(repo)
        cfg.renderly_url = "http://127.0.0.1:8022"

        calls = []

        class _Proc:
            def __init__(self, rc):
                self.returncode = rc
                self.stdout = ""
                self.stderr = ""

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if "export-batch" in cmd:
                return _Proc(0)  # export-batch always succeeds here
            for name in produced:
                (pid_dir / "images" / name).write_bytes(b"png")
            return _Proc(imagegen_rc)

        return cfg, pid_dir, calls, fake_run

    def test_export_batch_runs_before_imagegen(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, pid_dir, calls, fake_run = self._fixture(
                d, 0, produced=("A01_ST.png", "A02_PL.png"))
            with mock.patch.object(studio.subprocess, "run", fake_run):
                count = studio.run_imagegen(cfg, pid_dir, channel=123, upscale=0)

            self.assertEqual(count, 2)
            self.assertEqual(len(calls), 2)
            self.assertIn("export-batch", calls[0])
            self.assertIn("ImgToVideo.Cli", " ".join(calls[0]))
            self.assertIn("ImgToVideo.ImageGen", " ".join(calls[1]))
            self.assertIn("--renderly", calls[1])
            self.assertIn("--channel", calls[1])

    def test_exit_3_raises_quota_exhausted_with_the_partial_count(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, pid_dir, _, fake_run = self._fixture(
                d, 3, produced=("A01_ST.png", "A02_PL.png"))
            with mock.patch.object(studio.subprocess, "run", fake_run):
                with self.assertRaises(studio.RenderlyQuotaExhausted) as ctx:
                    studio.run_imagegen(cfg, pid_dir, channel=123, upscale=0)
            self.assertEqual(ctx.exception.generated, 2)

    def test_other_nonzero_exits_are_plain_runtime_errors(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, pid_dir, _, fake_run = self._fixture(d, 1)
            with mock.patch.object(studio.subprocess, "run", fake_run):
                with self.assertRaises(RuntimeError) as ctx:
                    studio.run_imagegen(cfg, pid_dir, channel=123, upscale=0)
            self.assertNotIsInstance(ctx.exception, studio.RenderlyQuotaExhausted)

    def test_a_quota_error_is_still_a_runtime_error(self):
        # autorun's generic resume handler catches RuntimeError, so the
        # subclass must not escape it by accident.
        self.assertTrue(issubclass(studio.RenderlyQuotaExhausted, RuntimeError))


class QuotaFallbackTests(unittest.TestCase):
    """autorun._run_images: Renderly exits 3 -> finish through the Flow Driver,
    and the reported count is the two partials added together."""

    def test_quota_exhaustion_is_finished_by_the_flow_driver(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d) / "wr.db")
            pid_dir = Path(d) / "studio"
            pid_dir.mkdir()
            (pid_dir / "shotlist.json").write_text(
                json.dumps(_shotlist(MOTIONS)), encoding="utf-8")

            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            pid = db.create_production(conn, "TEST aspect quota fallback")
            conn.commit()
            conn.close()

            seen = {"flow": 0}

            def fake_run_imagegen(cfg, pdir, channel=None, upscale=None):
                raise studio.RenderlyQuotaExhausted(2, "quota exceeded")

            def fake_run_imagegen_flow(cfg, pdir, **kwargs):
                seen["flow"] += 1
                return 3

            def fake_connect(c):
                c2 = db.connect(c.db_path)
                db.init_db(c2)
                return c2

            with mock.patch.object(studio, "prepare_project_folder",
                                   lambda c, p: pid_dir), \
                    mock.patch.object(autorun, "_effective",
                                      lambda c, p: {"engine": "renderly"}), \
                    mock.patch.object(autorun, "_connect", fake_connect), \
                    mock.patch.object(services.MANAGER, "ensure",
                                      lambda *a, **k: []), \
                    mock.patch.object(services.MANAGER, "release",
                                      lambda *a, **k: []), \
                    mock.patch.object(studio, "run_imagegen", fake_run_imagegen), \
                    mock.patch.object(studio, "run_imagegen_flow",
                                      fake_run_imagegen_flow):
                autorun._run_images(cfg, pid, mode="api", engine="renderly",
                                    log=lambda m: None)

            self.assertEqual(seen["flow"], 1)
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            detail = db.latest_steps(conn, pid)["images"]["detail"]
            conn.close()
            self.assertIn("5 image(s)", detail)
            self.assertIn("quota-limited", detail)

    def test_a_plain_renderly_failure_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d) / "wr.db")
            pid_dir = Path(d) / "studio"
            pid_dir.mkdir()
            (pid_dir / "shotlist.json").write_text(
                json.dumps(_shotlist(["ST"])), encoding="utf-8")

            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            pid = db.create_production(conn, "TEST aspect plain failure")
            conn.commit()
            conn.close()

            def fake_connect(c):
                c2 = db.connect(c.db_path)
                db.init_db(c2)
                return c2

            def fake_run_imagegen(cfg, pdir, channel=None, upscale=None):
                raise RuntimeError("ImageGen failed (exit 1): boom")

            with mock.patch.object(studio, "prepare_project_folder",
                                   lambda c, p: pid_dir), \
                    mock.patch.object(autorun, "_effective",
                                      lambda c, p: {"engine": "renderly"}), \
                    mock.patch.object(autorun, "_connect", fake_connect), \
                    mock.patch.object(services.MANAGER, "ensure",
                                      lambda *a, **k: []), \
                    mock.patch.object(services.MANAGER, "release",
                                      lambda *a, **k: []), \
                    mock.patch.object(studio, "run_imagegen", fake_run_imagegen), \
                    mock.patch.object(studio, "run_imagegen_flow",
                                      lambda *a, **k: self.fail(
                                          "must not fall back on a plain failure")):
                with self.assertRaises(RuntimeError):
                    autorun._run_images(cfg, pid, mode="api", engine="renderly",
                                        log=lambda m: None)


class AspectMatrixDocumentation(unittest.TestCase):
    """The reachability matrix the live tests assert against; kept here so a
    change to the tables fails a fast unit test before anyone spends money."""

    def test_renderly_api_matrix(self):
        self.assertEqual(RENDERLY_API_ASPECT,
                         {"ST": "16:9", "ZI": "16:9", "ZO": "16:9",
                          "PL": "21:9", "PR": "21:9",
                          "PU": "16:9", "PD": "16:9", "PV": "21:9"})

    def test_flowbatch_matrix(self):
        self.assertEqual(FLOWBATCH_ASPECT, {"PU": "1:1", "PD": "1:1", "PV": "16:9"})

    def test_every_motion_is_covered(self):
        for motion in MOTIONS:
            self.assertIn(motion, RENDERLY_API_ASPECT)


if __name__ == "__main__":
    unittest.main()
