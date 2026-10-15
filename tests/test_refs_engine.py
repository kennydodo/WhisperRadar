"""References generation must honor the channel's own image engine.

Before this fix, the refs stage always rendered its reference images through
FlowBatch, even for a channel configured to use Renderly - so a Renderly
channel silently depended on FlowBatch (and a Flow login) just to make its
reference images. One engine per channel: renderly => renderly does
everything, flowbatch => flowbatch does everything, no cross-engine calls.

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

from tests import wr_tmp  # noqa: E402

from whisperradar import autorun, db, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402


class RunRenderlyRefsTests(unittest.TestCase):
    """Unit tests for studio.run_renderly_refs() in isolation - the actual
    Renderly render call (studio.run_imagegen) is mocked; no subprocess, no
    network."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdir = Path(self.tmp.name)
        self.cfg = load_config(ROOT / "config.yaml")
        (self.pdir / "shotlist.json").write_text(json.dumps({
            "shots": [{"asset": "CH_MAYA.png", "cues": "1-1"},
                     {"asset": "BG_KITCHEN.png", "cues": "1-1"}],
            "images": [{"file": "S01_01.png", "prompt": "x",
                        "refs": ["CH_MAYA", "BG_KITCHEN"]}],
            "refs": {"CH_MAYA": None, "BG_KITCHEN": None},
            "refPrompts": {"CH_MAYA": "a woman in her 30s",
                          "BG_KITCHEN": "a cozy kitchen"},
            "style": "warm illustrated style",
        }), encoding="utf-8")

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def test_renders_through_run_imagegen_and_places_results(self):
        """A ref is rendered via the SAME Renderly call run_imagegen() makes
        for real shots (not a separate/forked subprocess path), and a ref
        that fails to render is reported as missing rather than raising."""
        refs = studio.refs_to_generate(self.pdir)
        self.assertEqual(set(refs), {"CH_MAYA", "BG_KITCHEN"})
        calls = []

        def fake_run_imagegen(cfg, pid_dir, channel=None, upscale=None):
            data = json.loads((pid_dir / "shotlist.json").read_text())
            calls.append({"pid_dir": pid_dir, "channel": channel,
                          "upscale": upscale, "job": data})
            img_dir = pid_dir / "images"
            img_dir.mkdir(exist_ok=True)
            for item in data["images"]:
                if item["file"] != "BG_KITCHEN.png":  # simulate one failure
                    (img_dir / item["file"]).write_bytes(b"png-bytes")
            return len(data["images"])

        with mock.patch.object(studio, "run_imagegen", fake_run_imagegen):
            result = studio.run_renderly_refs(
                self.cfg, self.pdir, 42, refs, channel=7, upscale=2)

        self.assertEqual(result["generated"], ["CH_MAYA"])
        self.assertEqual(result["missing"], ["BG_KITCHEN"])
        self.assertTrue((self.pdir / "refs" / "CH_MAYA.png").is_file())
        self.assertFalse((self.pdir / "refs" / "BG_KITCHEN.png").exists())
        # channel/upscale threaded through to the SAME Renderly path the
        # images stage itself uses
        self.assertEqual(calls[0]["channel"], 7)
        self.assertEqual(calls[0]["upscale"], 2)
        # the job it built for the renderer carried the channel's style
        self.assertEqual(calls[0]["job"]["style"], "warm illustrated style")
        # the registry now points the generated ref at its local file; the
        # one that failed is left exactly as it was
        data = json.loads((self.pdir / "shotlist.json").read_text())
        self.assertEqual(data["refs"]["CH_MAYA"], "refs/CH_MAYA.png")
        self.assertIsNone(data["refs"]["BG_KITCHEN"])
        manifest = json.loads(
            (self.pdir / studio.REFS_GENERATED_MANIFEST).read_text())
        self.assertEqual(manifest, ["CH_MAYA"])

    def test_no_refs_is_a_no_op(self):
        result = studio.run_renderly_refs(self.cfg, self.pdir, 42, {})
        self.assertEqual(result, {"generated": [], "missing": []})

    def test_cleans_up_its_temp_project_folder(self):
        """The throwaway shotlist-shaped folder used to drive run_imagegen()
        must not linger next to the real production."""
        captured = {}

        def fake_run_imagegen(cfg, pid_dir, channel=None, upscale=None):
            captured["pid_dir"] = pid_dir
            (pid_dir / "images").mkdir(exist_ok=True)
            (pid_dir / "images" / "CH_MAYA.png").write_bytes(b"x")
            return 1

        refs = {"CH_MAYA": {"path": None, "prompt": "a woman",
                            "file": None, "provided": False}}
        with mock.patch.object(studio, "run_imagegen", fake_run_imagegen):
            studio.run_renderly_refs(self.cfg, self.pdir, 42, refs)
        self.assertNotEqual(captured["pid_dir"], self.pdir)
        self.assertFalse(captured["pid_dir"].exists())


class RefsEngineDispatchTests(unittest.TestCase):
    """_run_refs() must pick the SAME engine the images stage would use for
    this channel, and never call the other engine's function at all."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        self.chan = db.create_own_channel(conn, "Ch")
        self.pid = db.create_production(conn, "P", "general", None, None)
        db.update_production(conn, self.pid, own_channel_id=self.chan)
        conn.commit()
        conn.close()

        self.pdir = studio.prod_dir(self.cfg, self.pid)
        self.pdir.mkdir(parents=True, exist_ok=True)
        (self.pdir / "shotlist.json").write_text(json.dumps({
            "shots": [{"asset": "S01_01.png", "cues": "1-1"}],
            "images": [{"file": "S01_01.png", "prompt": "x",
                        "refs": ["CH_MAYA"]}],
            "refs": {"CH_MAYA": None},
            "refPrompts": {"CH_MAYA": "a woman in her 30s"},
        }), encoding="utf-8")

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def test_renderly_channel_never_calls_flowbatch(self):
        # pin render_mode to 'api' so this tests the Renderly-API refs path;
        # 'auto' would otherwise be resolved by the default render mode
        conn = db.connect(self.cfg.db_path)
        db.update_production(conn, self.pid, render_mode="api")
        conn.commit()
        conn.close()
        with mock.patch.object(studio, "resolve_renderly_channel",
                               return_value=99) as resolve, \
                mock.patch.object(studio, "run_renderly_refs",
                                  return_value={"generated": ["CH_MAYA"],
                                               "missing": []}) as renderly, \
                mock.patch.object(studio, "run_flowbatch_refs") as flowbatch:
            result = autorun.run_stage(self.cfg, self.pid, "refs")
        self.assertEqual(result, "ok")
        flowbatch.assert_not_called()
        renderly.assert_called_once()
        args, kwargs = renderly.call_args
        self.assertIn("CH_MAYA", args[3])
        self.assertEqual(kwargs["channel"], 99)
        resolve.assert_called_once()

    def test_renderly_flow_mode_uses_flowbatch_not_api(self):
        """renderly + flow: refs go through FlowBatch (run_flowbatch_refs),
        never the :8022 API - and the Renderly channel is not even resolved
        (no API call at all)."""
        conn = db.connect(self.cfg.db_path)
        db.update_production(conn, self.pid, render_mode="flow")
        conn.commit()
        conn.close()
        with mock.patch.object(studio, "resolve_renderly_channel") as resolve, \
                mock.patch.object(studio, "run_renderly_refs") as renderly, \
                mock.patch.object(studio, "run_flowbatch_refs",
                                  return_value={"generated": ["CH_MAYA"],
                                               "missing": []}) as flowbatch:
            result = autorun.run_stage(self.cfg, self.pid, "refs")
        self.assertEqual(result, "ok")
        renderly.assert_not_called()
        resolve.assert_not_called()          # no Renderly channel/API involved
        flowbatch.assert_called_once()

    def test_flowbatch_channel_never_calls_renderly(self):
        conn = db.connect(self.cfg.db_path)
        db.set_setting(conn, "default_engine", "flowbatch")
        conn.commit()
        conn.close()
        with mock.patch.object(studio, "run_flowbatch_refs",
                               return_value={"generated": ["CH_MAYA"],
                                            "missing": []}) as flowbatch, \
                mock.patch.object(studio, "run_renderly_refs") as renderly, \
                mock.patch.object(studio, "resolve_renderly_channel") as resolve:
            result = autorun.run_stage(self.cfg, self.pid, "refs")
        self.assertEqual(result, "ok")
        renderly.assert_not_called()
        resolve.assert_not_called()
        flowbatch.assert_called_once()
        args, kwargs = flowbatch.call_args
        self.assertIn("CH_MAYA", args[3])


class FlowBatchRefsFolderTests(unittest.TestCase):
    """FlowBatch writes generated refs straight into refs\\ - no flow_refs
    staging folder, no copy, no upscaled twins."""

    def test_refs_job_outputs_into_refs_without_upscale(self):
        with wr_tmp.tempdir() as tmp:
            pdir = Path(tmp)
            (pdir / "shotlist.json").write_text("{}", encoding="utf-8")
            cfg = load_config()
            job_path = studio._write_refs_job(
                cfg, pdir, 1, {"CH_MAYA": {"prompt": "a woman"}})
            job = json.loads(job_path.read_text(encoding="utf-8"))
            self.assertEqual(Path(job["outputsDir"]), pdir / "refs")
            # generated tiles are named after their refs in Flow
            self.assertTrue(job["nameAssets"])

    def test_no_flow_refs_folder_in_the_code(self):
        self.assertNotIn("flow_refs", (ROOT / "whisperradar" / "studio.py")
                         .read_text(encoding="utf-8"))

    def test_generated_refs_are_adopted_in_place(self):
        with wr_tmp.tempdir() as tmp:
            pdir = Path(tmp)
            (pdir / "refs").mkdir()
            (pdir / "refs" / "CH_MAYA.png").write_bytes(b"x")
            (pdir / "shotlist.json").write_text(json.dumps(
                {"refs": {"CH_MAYA": None}, "images": []}), encoding="utf-8")

            class Proc:
                pid = 424242
                returncode = 0
                stdout = iter([])
                def poll(self): return 0
                def wait(self, timeout=None): return 0

            cfg = load_config()
            with mock.patch.object(studio, "flowbatch_ready", return_value=True), \
                    mock.patch.object(studio, "flowbatch_dir", return_value=pdir), \
                    mock.patch.object(studio, "set_flowbatch_tier") as tier, \
                    mock.patch.object(studio, "run_flowbatch_prepare",
                                      return_value={}), \
                    mock.patch.object(studio.subprocess, "Popen",
                                      return_value=Proc()):
                out = studio.run_flowbatch_refs(
                    cfg, pdir, 1, {"CH_MAYA": {"prompt": "p"}}, upscale=4)
            tier.assert_called_once_with(cfg, 0)
            self.assertEqual(out["generated"], ["CH_MAYA"])
            self.assertFalse((pdir / "flow_refs").exists())


    def test_first_pass_creates_the_project_and_generate_uses_it(self):
        """No stored Flow project: refs creates it via prepare --new-project,
        records it, and passes it to generate - so the images stage reuses it
        instead of opening a second project."""
        url = "https://labs.google/fx/tools/flow/project/abcdef12-3456"
        with wr_tmp.tempdir() as tmp:
            pdir = Path(tmp)
            (pdir / "refs").mkdir()
            (pdir / "refs" / "CH_MAYA.png").write_bytes(b"x")
            (pdir / "shotlist.json").write_text(json.dumps(
                {"refs": {"CH_MAYA": None}, "images": []}), encoding="utf-8")
            cfg = load_config()
            cmds, prepared = [], []

            def fake_stream(cmd, repo, log, cancel):
                cmds.append(list(cmd))
                return 0, []

            def fake_prepare(cfg_, pdir_, job_path, log=None, new_project=False):
                prepared.append(new_project)
                return {"projectUrl": url, "projectId": "abcdef12-3456",
                        "created": True, "refs": []}

            stored = {"url": None}

            def fake_url(cfg_, pid, override=None):
                return stored["url"], "production" if stored["url"] else "none"

            def fake_apply(cfg_, pdir_, pid, job_path, report, log=None):
                stored["url"] = report["projectUrl"]

            with mock.patch.object(studio, "flowbatch_ready", return_value=True), \
                    mock.patch.object(studio, "flowbatch_dir", return_value=pdir), \
                    mock.patch.object(studio, "set_flowbatch_tier"), \
                    mock.patch.object(studio, "flow_project_url_for", fake_url), \
                    mock.patch.object(studio, "run_flowbatch_prepare", fake_prepare), \
                    mock.patch.object(studio, "_apply_prepare_report", fake_apply), \
                    mock.patch.object(studio, "_flowbatch_stream", fake_stream):
                studio.run_flowbatch_refs(
                    cfg, pdir, 1, {"CH_MAYA": {"prompt": "p"}})
            self.assertEqual(prepared, [True])
            self.assertIn("--project-url", cmds[0])
            self.assertEqual(cmds[0][cmds[0].index("--project-url") + 1], url)

    def test_stored_project_is_not_recreated(self):
        with wr_tmp.tempdir() as tmp:
            pdir = Path(tmp)
            (pdir / "refs").mkdir()
            (pdir / "refs" / "CH_MAYA.png").write_bytes(b"x")
            (pdir / "shotlist.json").write_text(json.dumps(
                {"refs": {"CH_MAYA": None}, "images": []}), encoding="utf-8")
            cfg = load_config()
            with mock.patch.object(studio, "flowbatch_ready", return_value=True), \
                    mock.patch.object(studio, "flowbatch_dir", return_value=pdir), \
                    mock.patch.object(studio, "set_flowbatch_tier"), \
                    mock.patch.object(studio, "flow_project_url_for",
                                      return_value=("https://x/project/1", "production")), \
                    mock.patch.object(studio, "run_flowbatch_prepare") as prep, \
                    mock.patch.object(studio, "_flowbatch_stream",
                                      return_value=(0, [])):
                studio.run_flowbatch_refs(
                    cfg, pdir, 1, {"CH_MAYA": {"prompt": "p"}})
            prep.assert_not_called()


    def test_generated_refs_are_recorded_for_their_project(self):
        """The refs stage writes a sidecar naming the Flow project its tiles
        live in, so the images stage never uploads them a second time."""
        url = "https://flow.google.com/project/aaaa1111-2222"
        with wr_tmp.tempdir() as tmp:
            pdir = Path(tmp)
            (pdir / "refs").mkdir()
            (pdir / "refs" / "CH_MAYA.png").write_bytes(b"x")
            (pdir / "shotlist.json").write_text(json.dumps(
                {"refs": {"CH_MAYA": None}, "images": []}), encoding="utf-8")
            cfg = load_config()
            with mock.patch.object(studio, "flowbatch_ready", return_value=True), \
                    mock.patch.object(studio, "flowbatch_dir", return_value=pdir), \
                    mock.patch.object(studio, "set_flowbatch_tier"), \
                    mock.patch.object(studio, "flow_project_url_for",
                                      return_value=(url, "production")), \
                    mock.patch.object(studio, "_flowbatch_stream",
                                      return_value=(0, [])):
                studio.run_flowbatch_refs(cfg, pdir, 1, {"CH_MAYA": {"prompt": "p"}})
            rec = json.loads((pdir / studio.REFS_PROJECT_RECORD).read_text(
                encoding="utf-8"))
            self.assertEqual(rec["projectUrl"], url)
            self.assertIn("CH_MAYA", rec["names"])

    def test_the_images_job_carries_the_record(self):
        url = "https://flow.google.com/project/aaaa1111-2222"
        with wr_tmp.tempdir() as tmp:
            pdir = Path(tmp)
            (pdir / "refs").mkdir()
            (pdir / "refs" / "CH_MAYA.png").write_bytes(b"x")
            (pdir / "refs_project.json").write_text(json.dumps(
                {"projectUrl": url, "names": ["CH_MAYA"]}), encoding="utf-8")
            (pdir / "shotlist.json").write_text(json.dumps({
                "refs": {"CH_MAYA": None},
                "images": [{"file": "S01_01_SCN_ZI.png", "prompt": "p",
                            "refs": ["CH_MAYA"]}],
            }), encoding="utf-8")
            cfg = load_config()
            job_path, _ = studio.prepare_flowbatch_job(
                cfg, pdir, 1, project_url=url)
            job = json.loads(job_path.read_text(encoding="utf-8"))
            self.assertEqual(job["refsGenerated"],
                             {"projectUrl": url, "names": ["CH_MAYA"]})

    def test_a_record_is_carried_verbatim_the_guard_is_in_preparejs(self):
        """Python always embeds the record; prepare.js only trusts it when the
        project the job opens matches record.projectUrl (tested JS-side)."""
        url = "https://flow.google.com/project/aaaa1111-2222"
        other = "https://flow.google.com/project/bbbb9999-0000"
        with wr_tmp.tempdir() as tmp:
            pdir = Path(tmp)
            (pdir / "refs").mkdir()
            (pdir / "refs" / "CH_MAYA.png").write_bytes(b"x")
            (pdir / "refs_project.json").write_text(json.dumps(
                {"projectUrl": other, "names": ["CH_MAYA"]}), encoding="utf-8")
            (pdir / "shotlist.json").write_text(json.dumps({
                "refs": {"CH_MAYA": other},
                "images": [{"file": "S01_01_SCN_ZI.png", "prompt": "p",
                            "refs": ["CH_MAYA"]}],
            }), encoding="utf-8")
            cfg = load_config()
            job_path, _ = studio.prepare_flowbatch_job(
                cfg, pdir, 1, project_url=url)
            job = json.loads(job_path.read_text(encoding="utf-8"))
            self.assertEqual(job["refsGenerated"]["projectUrl"], other)


if __name__ == "__main__":
    unittest.main()
