"""When FlowBatch stops on "too many cards failed in a row", the images stage
first tries the Flow-gallery recovery (the same one the button runs): Flow
usually generated some of those cards before the UI broke. If the recovery
brings in everything, the pause and the re-render are skipped; if some are
still missing, the normal pause + resume follows and only the gaps render.
Every subprocess is stubbed - no browser, no network.

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

from whisperradar import autorun, db, services, studio  # noqa: E402
from whisperradar.config import load_config  # noqa: E402

SHOTLIST = {
    "shots": [{"asset": "S01_01.png", "cues": "1-1"},
              {"asset": "S02_01.png", "cues": "2-2"}],
    "images": [{"file": "S01_01.png", "prompt": "a red fox"},
               {"file": "S02_01.png", "prompt": "an owl"}],
    "refs": {},
}


def _stop_error():
    return RuntimeError(f"FlowBatch {studio.FLOW_CARDS_FAILED_MARKER} - Flow "
                        "is refusing this session.")


class RecoverAfterStopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg.db_path = Path(self.tmp.name) / "wr.db"
        self.cfg.studio_dir = Path(self.tmp.name) / "studio"
        conn = db.connect(self.cfg.db_path)
        db.init_db(conn)
        chan = db.create_own_channel(conn, "Ch")
        self.pid = db.create_production(conn, "P", "general", None, None)
        db.update_production(conn, self.pid, own_channel_id=chan)
        db.set_setting(conn, "default_engine", "flowbatch")
        conn.commit()
        conn.close()
        self.pdir = studio.prod_dir(self.cfg, self.pid)
        self.pdir.mkdir(parents=True, exist_ok=True)
        (self.pdir / "shotlist.json").write_text(json.dumps(SHOTLIST),
                                                 encoding="utf-8")
        (self.pdir / "images").mkdir(exist_ok=True)
        self.logs = []

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def _run(self, generate, recover, pause_effect=None):
        with mock.patch.object(studio, "run_imagegen_flowbatch",
                               side_effect=generate) as gen, \
                mock.patch.object(studio, "run_flowbatch_recover",
                                  side_effect=recover) as rec, \
                mock.patch.object(autorun, "_pause",
                                  side_effect=pause_effect) as pause, \
                mock.patch.object(services.MANAGER, "ensure"), \
                mock.patch.object(services.MANAGER, "release"):
            autorun._run_images(self.cfg, self.pid, log=self.logs.append)
        return gen, rec, pause

    def _step(self):
        conn = db.connect(self.cfg.db_path)
        try:
            return [s for s in db.step_history(conn, self.pid)
                    if s["stage"] == "images"][0]
        finally:
            conn.close()

    def _write(self, *names):
        for n in names:
            (self.pdir / "images" / n).write_bytes(b"x")

    def test_everything_recovered_skips_the_pause_and_the_rerender(self):
        def generate(*a, **k):
            raise _stop_error()

        def recover(cfg, pdir, pid, **k):
            self._write("S01_01.png", "S02_01.png")
            return {"recovered": ["S01_01.png", "S02_01.png"],
                    "still_missing": []}

        gen, rec, pause = self._run(generate, recover)
        self.assertEqual(gen.call_count, 1)
        rec.assert_called_once()
        pause.assert_not_called()
        detail = self._step()["detail"]
        self.assertIn("2 image(s)", detail)
        self.assertIn("gallery recovery", detail)

    def test_partial_recovery_still_pauses_and_resumes_the_gaps(self):
        calls = {"n": 0}

        def generate(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _stop_error()
            self._write("S02_01.png")
            return 1

        def recover(cfg, pdir, pid, **k):
            self._write("S01_01.png")
            return {"recovered": ["S01_01.png"],
                    "still_missing": ["S02_01.png"]}

        gen, rec, pause = self._run(generate, recover)
        self.assertEqual(gen.call_count, 2)
        pause.assert_called_once()
        self.assertIn("recovered from the Flow gallery",
                      self._step()["detail"])

    def test_a_failing_recovery_never_blocks_the_normal_resume(self):
        calls = {"n": 0}

        def generate(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _stop_error()
            self._write("S01_01.png", "S02_01.png")
            return 2

        def recover(*a, **k):
            raise RuntimeError("FlowBatch recover failed (exit 1)")

        gen, rec, pause = self._run(generate, recover)
        self.assertEqual(gen.call_count, 2)
        pause.assert_called_once()
        self.assertTrue(any("gallery recovery did not run" in m
                            for m in self.logs))

    # ---- account throttle: the long pause gets a recovery AFTER the wait ----

    def _throttle(self):
        return studio.throttle_error("test", 60)

    def test_throttle_recovers_after_the_pause_and_skips_the_rerender(self):
        """Flow finishes cards while the account is out: adopt them first."""
        order = []

        def generate(*a, **k):
            order.append("generate")
            raise self._throttle()       # would fail again if it ever re-ran

        def recover(cfg, pdir, pid, **k):
            order.append("recover")
            self._write("S01_01.png", "S02_01.png")
            return {"recovered": ["S01_01.png", "S02_01.png"],
                    "still_missing": []}

        gen, rec, pause = self._run(
            generate, recover,
            pause_effect=lambda *a, **k: order.append("pause"))
        # no gallery read before the throttle pause, one right after it
        self.assertEqual(order, ["generate", "pause", "recover"])
        self.assertEqual(gen.call_count, 1)        # nothing was re-rendered
        self.assertTrue(any("after the pause" in m for m in self.logs))
        detail = self._step()["detail"]
        self.assertIn("2 image(s)", detail)
        self.assertIn("recovered from the Flow gallery", detail)

    def test_partial_recovery_after_the_throttle_renders_only_the_gaps(self):
        calls = {"gen": 0}

        def generate(*a, **k):
            calls["gen"] += 1
            if calls["gen"] == 1:
                raise self._throttle()
            self.assertTrue((self.pdir / "images" / "S01_01.png").exists())
            self._write("S02_01.png")
            return 1

        def recover(cfg, pdir, pid, **k):
            self._write("S01_01.png")
            return {"recovered": ["S01_01.png"], "still_missing": ["S02_01.png"]}

        gen, rec, pause = self._run(generate, recover)
        self.assertEqual((gen.call_count, rec.call_count), (2, 1))
        self.assertIn("1 of them recovered from the Flow gallery",
                      self._step()["detail"])

    def test_nothing_new_after_the_throttle_just_resumes(self):
        calls = {"gen": 0}

        def generate(*a, **k):
            calls["gen"] += 1
            if calls["gen"] == 1:
                raise self._throttle()
            self._write("S01_01.png", "S02_01.png")
            return 2

        gen, rec, pause = self._run(
            generate, lambda *a, **k: {"recovered": [], "still_missing":
                                       ["S01_01.png", "S02_01.png"]})
        self.assertEqual((gen.call_count, rec.call_count), (2, 1))
        pause.assert_called_once()

    def test_a_failing_recovery_after_the_throttle_never_blocks_the_resume(self):
        calls = {"gen": 0}

        def generate(*a, **k):
            calls["gen"] += 1
            if calls["gen"] == 1:
                raise self._throttle()
            self._write("S01_01.png", "S02_01.png")
            return 2

        def recover(*a, **k):
            raise RuntimeError("FlowBatch recover failed (exit 1)")

        gen, rec, pause = self._run(generate, recover)
        self.assertEqual(gen.call_count, 2)
        self.assertTrue(any("gallery recovery did not run" in m
                            for m in self.logs))

    def test_a_stop_during_the_throttle_pause_never_recovers_or_renders(self):
        def cancelled_pause(*a, **k):
            raise studio.BatchCancelled("stop requested while waiting")

        with mock.patch.object(studio, "run_imagegen_flowbatch",
                               side_effect=lambda *a, **k: (_ for _ in ()).throw(
                                   self._throttle())) as gen, \
                mock.patch.object(studio, "run_flowbatch_recover") as rec, \
                mock.patch.object(autorun, "_pause",
                                  side_effect=cancelled_pause), \
                mock.patch.object(services.MANAGER, "ensure"), \
                mock.patch.object(services.MANAGER, "release"):
            with self.assertRaises(studio.BatchCancelled):
                autorun._run_images(self.cfg, self.pid, log=self.logs.append)
        self.assertEqual((gen.call_count, rec.call_count), (1, 0))

    def test_short_pauses_do_not_get_the_after_the_wait_recovery(self):
        """'Cards failed' (10 min) already recovers once BEFORE its pause; no
        second one after it. 'Still busy' never recovers."""
        for exc_factory, expected in ((_stop_error, 1),
                                      (lambda: RuntimeError(
                                          "2 of 3 image(s) were not produced"), 0)):
            self.logs.clear()
            for f in (self.pdir / "images").glob("*"):
                f.unlink()
            calls = {"gen": 0}

            def generate(*a, **k):
                calls["gen"] += 1
                if calls["gen"] == 1:
                    raise exc_factory()
                self._write("S01_01.png", "S02_01.png")
                return 2

            gen, rec, pause = self._run(
                generate, lambda *a, **k: {"recovered": [], "still_missing":
                                           ["S01_01.png", "S02_01.png"]})
            self.assertEqual(rec.call_count, expected)
            self.assertEqual(gen.call_count, 2)
            self.assertFalse(any("after the pause" in m for m in self.logs))

    def test_other_errors_do_not_trigger_a_recovery(self):
        calls = {"n": 0}

        def generate(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError(
                    "Flow is refusing this session - the generation was "
                    "refused")
            self._write("S01_01.png", "S02_01.png")
            return 2

        gen, rec, pause = self._run(generate, lambda *a, **k: {})
        rec.assert_not_called()


if __name__ == "__main__":
    unittest.main()
