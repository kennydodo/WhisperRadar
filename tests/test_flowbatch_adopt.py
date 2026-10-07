"""FlowBatch adoption: keep the Flow master, drop the redundant upscaled copy.

FlowBatch writes both <name>.png (Flow master) and <name>_<tier>.png (upscaled)
into flow_images. WhisperRadar adopts the upscaled into images\\<name> (the
pipeline's name), and should then remove that upscaled copy from flow_images so
it is not duplicated - while KEEPING the master (a bad upscale is redone later)."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import wr_tmp  # noqa: E402

from whisperradar import studio  # noqa: E402


class AdoptFlowbatchOutputsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdir = Path(self.tmp.name)
        (self.pdir / "flow_images").mkdir()
        (self.pdir / "flow_images" / "S01_01.png").write_bytes(b"MASTER")
        (self.pdir / "flow_images" / "S01_01_2k.png").write_bytes(b"UPSCALED")

    def tearDown(self):
        wr_tmp.cleanup(self.tmp)

    def test_adopts_upscale_and_keeps_only_the_master(self):
        adopted = studio._adopt_flowbatch_outputs(
            self.pdir, ["S01_01.png"], "2k")
        self.assertEqual(adopted, ["S01_01.png"])
        # images\ got the upscaled content under the shotlist name
        self.assertEqual((self.pdir / "images" / "S01_01.png").read_bytes(),
                         b"UPSCALED")
        # flow_images keeps the master ...
        self.assertTrue((self.pdir / "flow_images" / "S01_01.png").is_file())
        # ... and drops the redundant upscaled copy
        self.assertFalse((self.pdir / "flow_images" / "S01_01_2k.png").exists())

    def test_tier_off_adopts_the_master_and_keeps_it(self):
        (self.pdir / "flow_images" / "S01_01_2k.png").unlink()
        adopted = studio._adopt_flowbatch_outputs(
            self.pdir, ["S01_01.png"], "off")
        self.assertEqual(adopted, ["S01_01.png"])
        self.assertEqual((self.pdir / "images" / "S01_01.png").read_bytes(),
                         b"MASTER")
        self.assertTrue((self.pdir / "flow_images" / "S01_01.png").is_file())

    def test_a_stale_other_tier_file_is_never_touched(self):
        """A leftover "{stem}_<othertier>" from an earlier run/resume at a
        DIFFERENT tier setting must survive an adopt that did not use it -
        it was never copied into images\, so deleting it would be data
        loss, not cleanup (the bug fixed alongside this test: the cleanup
        used to glob-delete every "{stem}_*" file regardless of which one
        was actually adopted)."""
        (self.pdir / "flow_images" / "S01_01_4k.png").write_bytes(b"STALE-4K")
        adopted = studio._adopt_flowbatch_outputs(
            self.pdir, ["S01_01.png"], "2k")
        self.assertEqual(adopted, ["S01_01.png"])
        # the 2k upscale was adopted and its flow_images copy removed ...
        self.assertEqual((self.pdir / "images" / "S01_01.png").read_bytes(),
                         b"UPSCALED")
        self.assertFalse((self.pdir / "flow_images" / "S01_01_2k.png").exists())
        # ... but the UNRELATED stale 4k file was never touched
        self.assertTrue((self.pdir / "flow_images" / "S01_01_4k.png").is_file())
        self.assertEqual(
            (self.pdir / "flow_images" / "S01_01_4k.png").read_bytes(),
            b"STALE-4K")

    def test_tier_off_never_deletes_a_leftover_upscale_from_a_prior_tier(self):
        """Same bug, the scenario it actually happened in: adopting with
        tier="off" must not delete a "{stem}_2k" file left over from a
        PRIOR run/resume that had upscaling on - images\ gets the master
        this time, so that old upscale was never duplicated anywhere and
        deleting it would be a straight loss of the only copy."""
        adopted = studio._adopt_flowbatch_outputs(
            self.pdir, ["S01_01.png"], "off")
        self.assertEqual(adopted, ["S01_01.png"])
        self.assertEqual((self.pdir / "images" / "S01_01.png").read_bytes(),
                         b"MASTER")
        self.assertTrue((self.pdir / "flow_images" / "S01_01_2k.png").is_file())
        self.assertEqual(
            (self.pdir / "flow_images" / "S01_01_2k.png").read_bytes(),
            b"UPSCALED")


if __name__ == "__main__":
    unittest.main()
