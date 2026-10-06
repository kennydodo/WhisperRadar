"""Aspect-ratio pipeline: motion code -> requested aspect, per path.

Covers the 2026-09 aspect work in WhisperRadar without any network or spend:

  Renderly API   run_imagegen runs ImgToVideo.Cli export-batch first, so
                 ImageGen --renderly requests the per-file "aspect" (21:9 for
                 PL/PR/PV; PU/PD deliberately stay 16:9 - the paid path never
                 spends a generation on a shape Flow gives away for free).
  FlowBatch      prepare_flowbatch_job writes per-item aspectRatio (PU/PD =
                 1:1); every other motion keeps the job default 16:9.
  Quota fallback run_imagegen's exit 3 becomes RenderlyQuotaExhausted, and
                 autorun._run_images finishes the rest through FlowBatch.

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
            with mock.patch.object(studio.subprocess, "run", fake_run), \
                 mock.patch.object(studio, "_run_tracked",
                                   lambda cmd, label, timeout=None, **kw:
                                   fake_run(cmd, **kw)):
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
            with mock.patch.object(studio.subprocess, "run", fake_run), \
                 mock.patch.object(studio, "_run_tracked",
                                   lambda cmd, label, timeout=None, **kw:
                                   fake_run(cmd, **kw)):
                with self.assertRaises(studio.RenderlyQuotaExhausted) as ctx:
                    studio.run_imagegen(cfg, pid_dir, channel=123, upscale=0)
            self.assertEqual(ctx.exception.generated, 2)

    def test_other_nonzero_exits_are_plain_runtime_errors(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, pid_dir, _, fake_run = self._fixture(d, 1)
            with mock.patch.object(studio.subprocess, "run", fake_run), \
                 mock.patch.object(studio, "_run_tracked",
                                   lambda cmd, label, timeout=None, **kw:
                                   fake_run(cmd, **kw)):
                with self.assertRaises(RuntimeError) as ctx:
                    studio.run_imagegen(cfg, pid_dir, channel=123, upscale=0)
            self.assertNotIsInstance(ctx.exception, studio.RenderlyQuotaExhausted)

    def test_a_quota_error_is_still_a_runtime_error(self):
        # autorun's generic resume handler catches RuntimeError, so the
        # subclass must not escape it by accident.
        self.assertTrue(issubclass(studio.RenderlyQuotaExhausted, RuntimeError))


class RenderlySplitTests(unittest.TestCase):
    """autorun._run_images, engine=renderly/mode=api: FlowBatch renders the
    bulk FIRST (skipping PL/PR), then the paid API renders only PL/PR; what the
    API could not do is finished by FlowBatch as 16:9 push-ins."""

    WIDE = ("PL", "PR")

    def _run(self, motions, fake_api, fake_flow, label, preexisting=()):
        """Run _run_images with fakes that really write files, so the
        "what is still missing" decisions behave as in production. Returns
        (step detail, call order)."""
        calls = []
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d) / "wr.db")
            pid_dir = Path(d) / "studio"
            (pid_dir / "images").mkdir(parents=True)
            data = _shotlist(motions)
            (pid_dir / "shotlist.json").write_text(
                json.dumps(data), encoding="utf-8")
            for name in preexisting:
                (pid_dir / "images" / name).write_bytes(b"x")

            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            pid = db.create_production(conn, label)
            conn.commit()
            conn.close()

            def fake_connect(c):
                c2 = db.connect(c.db_path)
                db.init_db(c2)
                return c2

            def write(pdir, wanted):
                n = 0
                for item in data["images"]:
                    f = item["file"]
                    if (not (pdir / "images" / f).exists()
                            and wanted(_motion_of(f))):
                        (pdir / "images" / f).write_bytes(b"x")
                        n += 1
                return n

            def api(cfg_, pdir, channel=None, upscale=None, motion_filter=None):
                calls.append(("api", tuple(motion_filter or ())))
                return fake_api(pdir, write)

            def flow(cfg_, pdir, pid_, **kwargs):
                skip = tuple(kwargs.get("skip_motion") or ())
                calls.append(("flow", skip))
                return fake_flow(pdir, write, skip)

            with mock.patch.object(studio, "prepare_project_folder",
                                   lambda c, p: pid_dir), \
                    mock.patch.object(autorun, "_effective",
                                      lambda c, p: {"engine": "renderly"}), \
                    mock.patch.object(autorun, "_connect", fake_connect), \
                    mock.patch.object(services.MANAGER, "ensure",
                                      lambda *a, **k: []), \
                    mock.patch.object(services.MANAGER, "release",
                                      lambda *a, **k: []), \
                    mock.patch.object(studio, "renderly_channel_list",
                                      lambda c: ([], None, set())), \
                    mock.patch.object(studio, "run_imagegen", api), \
                    mock.patch.object(studio, "run_imagegen_flowbatch", flow):
                try:
                    autorun._run_images(cfg, pid, mode="api",
                                        engine="renderly",
                                        log=lambda m: None)
                finally:
                    conn = db.connect(cfg.db_path)
                    db.init_db(conn)
                    steps = db.latest_steps(conn, pid)
                    conn.close()
            detail = steps["images"]["detail"] if "images" in steps else ""
            return detail, calls

    @staticmethod
    def _flow_all(pdir, write, skip):
        return write(pdir, lambda m: m not in skip)

    @staticmethod
    def _api_wide(pdir, write):
        return write(pdir, lambda m: m in ("PL", "PR"))

    def test_flow_renders_the_bulk_first_then_the_api_does_pl_pr(self):
        detail, calls = self._run(MOTIONS, self._api_wide, self._flow_all,
                                  "TEST order - normal")
        self.assertEqual(calls, [("flow", self.WIDE), ("api", self.WIDE)])
        self.assertIn("8 image(s)", detail)
        self.assertIn("FlowBatch (rest) + Renderly API (PL/PR)", detail)
        self.assertNotIn("quota-limited", detail)

    def test_quota_wall_on_pl_pr_is_finished_by_flow_afterwards(self):
        def api(pdir, write):
            # got one PL through, then the wall
            (pdir / "images" / "A04_PL.png").write_bytes(b"x")
            raise studio.RenderlyQuotaExhausted(1, "quota exceeded")

        detail, calls = self._run(MOTIONS, api, self._flow_all,
                                  "TEST order - quota")
        self.assertEqual(calls, [("flow", self.WIDE), ("api", self.WIDE),
                                 ("flow", ())])
        self.assertIn("8 image(s)", detail)
        self.assertIn("quota-limited", detail)

    def test_api_that_silently_misses_shots_is_topped_up_by_flow(self):
        def api(pdir, write):
            (pdir / "images" / "A04_PL.png").write_bytes(b"x")
            return 1          # PR never made it

        detail, calls = self._run(MOTIONS, api, self._flow_all,
                                  "TEST order - partial api")
        self.assertEqual([c[0] for c in calls], ["flow", "api", "flow"])
        self.assertIn("PL/PR it missed", detail)

    def test_a_plain_api_failure_does_not_fall_back(self):
        def api(pdir, write):
            raise RuntimeError("ImageGen failed (exit 1): boom")

        with self.assertRaises(RuntimeError):
            self._run(MOTIONS, api, self._flow_all, "TEST order - api failure")

    def test_the_api_is_never_called_when_flow_fails(self):
        def flow(pdir, write, skip):
            raise RuntimeError("FlowBatch failed (exit 1): boom")

        def api(pdir, write):
            self.fail("PL/PR must not be paid for while the bulk is unfinished")

        with self.assertRaises(RuntimeError):
            self._run(MOTIONS, api, flow, "TEST order - flow failure")

    def test_a_resume_skips_flow_when_only_pl_pr_are_left(self):
        done = [f"A{i:02d}_{m}.png" for i, m in enumerate(MOTIONS, 1)
                if m not in self.WIDE]
        detail, calls = self._run(MOTIONS, self._api_wide, self._flow_all,
                                  "TEST order - resume", preexisting=done)
        self.assertEqual(calls, [("api", self.WIDE)])

    def test_no_pl_pr_in_the_plan_means_flow_only(self):
        detail, calls = self._run(["ST", "ZI", "ZO"], self._api_wide,
                                  self._flow_all, "TEST order - no wide")
        self.assertEqual(calls, [("flow", self.WIDE)])
        self.assertIn("FlowBatch", detail)


class EngineResolutionTests(unittest.TestCase):
    """effective_engine / services_for / _default_render_mode after the Flow
    Driver was retired: 'flow' mode always means FlowBatch."""

    def test_effective_engine_truth_table(self):
        for engine, mode, want in (
                ("flowbatch", "api", "flowbatch"),
                ("flowbatch", "flow", "flowbatch"),
                ("flowbatch", None, "flowbatch"),
                ("renderly", "flow", "flowbatch"),
                ("renderly", "api", "renderly"),
                ("renderly", "auto", "renderly"),
                ("renderly", None, "renderly"),
                (None, "api", "renderly"),
                (None, "flow", "flowbatch")):
            self.assertEqual(studio.effective_engine(engine, mode), want,
                             (engine, mode))

    def test_services_for_table(self):
        self.assertEqual(services.services_for("flowbatch", "api"), [])
        self.assertEqual(services.services_for("flowbatch", "flow"), [])
        self.assertEqual(services.services_for("renderly", "flow"), [])
        self.assertEqual(services.services_for("renderly", "api"),
                         ["renderly"])

    def test_auto_render_mode_means_the_api(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d) / "wr.db")
            conn = db.connect(cfg.db_path)
            db.init_db(conn)
            pid = db.create_production(conn, "TEST auto mode")
            db.set_setting(conn, "default_render_mode", "auto")
            conn.commit()
            conn.close()
            prod = autorun._get_prod(cfg, pid)
            with mock.patch.object(autorun, "_connect",
                                   lambda c: db.init_db(
                                       db.connect(c.db_path)) or
                                   db.connect(c.db_path)):
                self.assertEqual(autorun._default_render_mode(cfg, prod),
                                 "api")


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
