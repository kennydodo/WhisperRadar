"""Generated references are found in the Flow project by the caption Flow gave
them, checked once, never uploaded again; only the missing ones are remade.

Flow cannot name a generated image: the asset library lists it under its own
caption ("Two apes sitting hunched") while an upload keeps its file name. The
refs stage records the captions; the images stage only checks them.

Run: python -m unittest discover -s tests
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisperradar import prodfiles, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402

URL = "https://flow.google.com/project/aaaa1111-2222"
OTHER = "https://flow.google.com/project/bbbb9999-0000"


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pdir = Path(self.tmp)
        (self.pdir / "refs").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def record(self):
        return json.loads(prodfiles.path(self.pdir, studio.REFS_PROJECT_RECORD)
                          .read_text(encoding="utf-8"))


class RecordTitles(Tmp):
    def test_captions_are_recorded_with_the_names(self):
        studio._record_refs_project(self.pdir, URL, ["CH_APES", "CH_TRIBE"],
                                    {"CH_APES": "Two apes sitting hunched"})
        rec = self.record()
        self.assertEqual(rec["names"], ["CH_APES", "CH_TRIBE"])
        self.assertEqual(rec["titles"], {"CH_APES": "Two apes sitting hunched"})

    def test_a_remade_ref_replaces_its_old_caption(self):
        studio._record_refs_project(self.pdir, URL, ["CH_APES"],
                                    {"CH_APES": "Old caption"})
        studio._record_refs_project(self.pdir, URL, ["CH_APES"],
                                    {"CH_APES": "New caption"})
        self.assertEqual(self.record()["titles"], {"CH_APES": "New caption"})
        # remade but caption unknown: the stale caption must not stay
        studio._record_refs_project(self.pdir, URL, ["CH_APES"], {})
        self.assertEqual(self.record()["titles"], {})

    def test_other_refs_keep_their_captions(self):
        studio._record_refs_project(self.pdir, URL, ["A", "B"], {"A": "a cap", "B": "b cap"})
        studio._record_refs_project(self.pdir, URL, ["B"], {"B": "b new"})
        self.assertEqual(self.record()["titles"], {"A": "a cap", "B": "b new"})

    def test_a_new_project_starts_the_record_over(self):
        studio._record_refs_project(self.pdir, URL, ["A"], {"A": "a cap"})
        studio._record_refs_project(self.pdir, OTHER, ["B"], {"B": "b cap"})
        rec = self.record()
        self.assertEqual(rec["projectUrl"], OTHER)
        self.assertEqual(rec["titles"], {"B": "b cap"})

    def test_titles_file_is_read_back(self):
        (self.pdir / studio.REFS_TITLES_FILE).write_text(
            json.dumps({"A": "a cap", "B": None, "C": "  "}), encoding="utf-8")
        self.assertEqual(studio._read_refs_titles(self.pdir), {"A": "a cap"})
        (self.pdir / studio.REFS_TITLES_FILE).write_text("not json", encoding="utf-8")
        self.assertEqual(studio._read_refs_titles(self.pdir), {})


class JobCarriesCaptions(Tmp):
    def test_reference_paths_survive_job_folder_move(self):
        # CH_JOE is an alias for an outfit filename: do not replace it with
        # the stem, or look for refs/ below the job's flow/ directory.
        outfit = self.pdir / "refs" / "CH_JOE_casual.png"
        outfit.write_bytes(b"x")
        external = self.pdir / "external.png"
        external.write_bytes(b"x")
        data = {"refs": {"CH_JOE": "refs/CH_JOE_casual.png",
                         "CH_OTHER": str(external)},
                "images": [{"file": "S01_01_SCN_ZI.png", "prompt": "p",
                            "refs": ["CH_JOE", "CH_OTHER"]}]}
        source = json.dumps(data)
        (self.pdir / "shotlist.json").write_text(source, encoding="utf-8")
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                if legacy:
                    prodfiles.path(self.pdir, "flowbatch.json").unlink()
                    (self.pdir / "flowbatch.json").write_text("{}", encoding="utf-8")
                job_path, _ = studio.prepare_flowbatch_job(
                    load_config(), self.pdir, 1, project_url=URL)
                job = json.loads(job_path.read_text(encoding="utf-8"))
                self.assertEqual(job_path.parent, self.pdir if legacy else self.pdir / "flow")
                self.assertEqual(job["refs"]["CH_JOE"], str(outfit.resolve()))
                self.assertEqual(job["refs"]["CH_OTHER"], str(external.resolve()))
                for value in job["refs"].values():
                    self.assertTrue(Path(value).is_absolute())
                    self.assertTrue((job_path.parent / value).is_file())
        self.assertEqual((self.pdir / "shotlist.json").read_text(encoding="utf-8"), source)

    def write(self, record_url):
        (self.pdir / "refs" / "CH_APES.png").write_bytes(b"x")
        prodfiles.wpath(self.pdir, studio.REFS_PROJECT_RECORD).write_text(json.dumps(
            {"projectUrl": record_url, "names": ["CH_APES"],
             "titles": {"CH_APES": "Two apes sitting hunched"}}), encoding="utf-8")
        (self.pdir / "shotlist.json").write_text(json.dumps({
            "refs": {"CH_APES": None},
            "images": [{"file": "S01_01_SCN_ZI.png", "prompt": "p",
                        "refs": ["CH_APES"]}]}), encoding="utf-8")

    def test_same_project_gives_ref_assets(self):
        self.write(URL)
        job_path, _ = studio.prepare_flowbatch_job(
            load_config(), self.pdir, 1, project_url=URL)
        job = json.loads(job_path.read_text(encoding="utf-8"))
        self.assertEqual(job["refAssets"], {"CH_APES": "Two apes sitting hunched"})

    def test_another_project_gives_none(self):
        self.write(OTHER)
        job_path, _ = studio.prepare_flowbatch_job(
            load_config(), self.pdir, 1, project_url=URL)
        job = json.loads(job_path.read_text(encoding="utf-8"))
        self.assertNotIn("refAssets", job)

    def test_the_refs_job_asks_flowbatch_to_record_captions(self):
        (self.pdir / "shotlist.json").write_text("{}", encoding="utf-8")
        job_path = studio._write_refs_job(
            load_config(), self.pdir, 1, {"CH_APES": {"prompt": "apes"}})
        job = json.loads(job_path.read_text(encoding="utf-8"))
        self.assertTrue(job["recordAssetTitles"])


class RegenerateOnlyMissing(Tmp):
    def shotlist(self):
        (self.pdir / "shotlist.json").write_text(json.dumps({
            "refs": {"CH_APES": "refs/CH_APES.png", "CH_TRIBE": "refs/CH_TRIBE.png"},
            "refPrompts": {"CH_APES": "apes", "CH_TRIBE": "a tribe"},
            "images": [{"file": "S01_01_SCN_ZI.png", "prompt": "p",
                        "refs": ["CH_APES", "CH_TRIBE"]}]}), encoding="utf-8")
        for n in ("CH_APES", "CH_TRIBE"):
            (self.pdir / "refs" / f"{n}.png").write_bytes(b"x")

    def test_generated_refs_missing_reads_the_report(self):
        report = {"refs": [
            {"name": "CH_APES", "status": "reused", "generated": True},
            {"name": "CH_TRIBE", "status": "missing", "generated": True},
            {"name": "SUPPLIED", "status": "missing"},
        ]}
        self.assertEqual(studio.generated_refs_missing(report), ["CH_TRIBE"])
        self.assertEqual(studio.generated_refs_missing({}), [])

    def test_only_the_missing_ref_is_made_again_and_forced(self):
        self.shotlist()
        seen = {}

        def fake(cfg, pdir, pid, refs, log=None, cancel=None, force=False, **kw):
            seen.update(refs=set(refs), force=force)
            return {"generated": list(refs), "missing": []}

        with mock.patch.object(studio, "run_flowbatch_refs", side_effect=fake):
            ok = studio._regenerate_generated_refs(
                load_config(), self.pdir, 1, ["CH_TRIBE"])
        self.assertTrue(ok)
        self.assertEqual(seen, {"refs": {"CH_TRIBE"}, "force": True})

    def test_nothing_is_made_when_there_is_no_prompt(self):
        self.shotlist()
        data = json.loads((self.pdir / "shotlist.json").read_text())
        data["refPrompts"] = {}
        (self.pdir / "shotlist.json").write_text(json.dumps(data), encoding="utf-8")
        with mock.patch.object(studio, "run_flowbatch_refs",
                               side_effect=AssertionError("no prompt, no run")):
            self.assertFalse(studio._regenerate_generated_refs(
                load_config(), self.pdir, 1, ["CH_TRIBE"]))

    def test_force_bypasses_the_existing_file_guard_and_does_not_resume(self):
        self.shotlist()
        cmds = []

        def fake_stream(cmd, repo, log, cancel):
            cmds.append(list(cmd))
            return 0, []

        with mock.patch.object(studio, "flowbatch_ready", return_value=True), \
                mock.patch.object(studio, "flowbatch_dir", return_value=self.pdir), \
                mock.patch.object(studio, "set_flowbatch_tier"), \
                mock.patch.object(studio, "flow_project_url_for",
                                  return_value=(URL, "production")), \
                mock.patch.object(studio, "_flowbatch_stream", side_effect=fake_stream):
            out = studio.run_flowbatch_refs(
                load_config(), self.pdir, 1, {"CH_TRIBE": {"prompt": "a tribe"}},
                force=True)
            self.assertEqual(out["generated"], ["CH_TRIBE"])
            self.assertIn("--no-resume", cmds[0])
            cmds.clear()
            # without force the same call pays for nothing: the file exists
            out = studio.run_flowbatch_refs(
                load_config(), self.pdir, 1, {"CH_TRIBE": {"prompt": "a tribe"}})
            self.assertEqual(out, {"generated": [], "missing": []})
            self.assertEqual(cmds, [])


class ImagesStageRemakesOnlyWhatIsGone(unittest.TestCase):
    """prepare reports a generated ref missing -> just that ref is remade, the
    job is rebuilt (new caption) and prepare checks again. No upload anywhere."""

    def test_missing_generated_ref_is_remade_then_checked_again(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        pdir = Path(tmp)
        job = pdir / "flowbatch.json"
        job.write_text("{}", encoding="utf-8")
        cfg = load_config(ROOT / "config.yaml")
        cfg.db_path = pdir / "wr.db"
        gone = {"projectUrl": URL, "created": False, "refs": [
            {"name": "CH_APES", "status": "reused", "generated": True,
             "source": "gallery"},
            {"name": "CH_TRIBE", "status": "missing", "generated": True}]}
        fine = {"projectUrl": URL, "created": False, "refs": [
            {"name": "CH_APES", "status": "reused", "generated": True,
             "source": "gallery"},
            {"name": "CH_TRIBE", "status": "reused", "generated": True,
             "source": "gallery"}]}
        prepares = [gone, fine]
        built = []
        regen = mock.Mock(return_value=True)

        def fake_job(*a, **k):
            built.append(1)
            return job, ["S01_01_SCN_ZI.png"]

        stop = RuntimeError("stop after the handshake")
        with mock.patch.object(studio, "flowbatch_ready", return_value=True), \
                mock.patch.object(studio, "flowbatch_dir", return_value=pdir), \
                mock.patch.object(studio, "prepare_flowbatch_job", side_effect=fake_job), \
                mock.patch.object(studio, "flow_project_url_for",
                                  return_value=(URL, "production")), \
                mock.patch.object(studio, "run_flowbatch_prepare",
                                  side_effect=lambda *a, **k: prepares.pop(0)), \
                mock.patch.object(studio, "_apply_prepare_report"), \
                mock.patch.object(studio, "_regenerate_generated_refs", regen), \
                mock.patch.object(studio, "set_flowbatch_tier", side_effect=stop):
            with self.assertRaises(RuntimeError) as ctx:
                studio.run_imagegen_flowbatch(cfg, pdir, 1)
        self.assertIs(ctx.exception, stop)
        regen.assert_called_once()
        self.assertEqual(regen.call_args.args[3], ["CH_TRIBE"])
        self.assertEqual(len(built), 2)      # job rebuilt after the remake
        self.assertEqual(prepares, [])       # prepare ran twice


if __name__ == "__main__":
    unittest.main()
