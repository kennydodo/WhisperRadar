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

from whisperradar import studio  # noqa: E402


class AdoptFlowbatchOutputsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdir = Path(self.tmp.name)
        (self.pdir / "flow_images").mkdir()
        (self.pdir / "flow_images" / "S01_01.png").write_bytes(b"MASTER")
        (self.pdir / "flow_images" / "S01_01_2k.png").write_bytes(b"UPSCALED")

    def tearDown(self):
        self.tmp.cleanup()

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


if __name__ == "__main__":
    unittest.main()
