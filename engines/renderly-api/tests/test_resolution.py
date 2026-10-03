"""Resolution preset math: exact ImgToVideo sizes, ratio preservation, naming."""
import unittest

import base  # noqa: F401  (sys.path + isolated DATABASE_URL)
from config import RESOLUTION_PRESETS
from services import upscaler


class TargetDimensions(unittest.TestCase):
    def test_sixteen_nine_sources_snap_to_exact_preset_sizes(self):
        for tier, (width, height) in RESOLUTION_PRESETS.items():
            self.assertEqual(upscaler.target_dimensions(tier, 1376, 768), (width, height), tier)
            self.assertEqual(
                upscaler.target_dimensions(tier, 768, 1376), (height, width), f"{tier} portrait"
            )

    def test_other_ratios_scale_by_their_short_side(self):
        self.assertEqual(upscaler.target_dimensions("HD", 1024, 1024), (1080, 1080))
        self.assertEqual(upscaler.target_dimensions("2K", 1152, 864), (1920, 1440))
        self.assertEqual(upscaler.target_dimensions("2K", 864, 1152), (1440, 1920))
        self.assertEqual(upscaler.target_dimensions("4K", 1024, 1024), (2160, 2160))

    def test_every_resized_result_classifies_back_to_its_tier(self):
        for tier in RESOLUTION_PRESETS:
            width, height = upscaler.target_dimensions(tier, 1376, 768)
            self.assertEqual(upscaler.classify_size(width, height), tier, tier)

    def test_classify_names_native_and_legacy_sizes(self):
        self.assertEqual(upscaler.classify_size(1376, 768), "1K")  # Gemini native
        self.assertEqual(upscaler.classify_size(2752, 1536), "2K")  # old 2x output
        self.assertEqual(upscaler.classify_size(5504, 3072), "4K")  # old 4x output
        self.assertEqual(upscaler.classify_size(1920, 1080), "HD")
        self.assertEqual(upscaler.classify_size(2560, 1440), "2K")
        self.assertEqual(upscaler.classify_size(3840, 2160), "4K")

    def test_unknown_tier_is_rejected(self):
        with self.assertRaises(KeyError):
            upscaler.target_dimensions("8K", 1376, 768)


class ResolveTier(unittest.TestCase):
    def test_tier_names_pass_through(self):
        for tier in ("HD", "2K", "4K"):
            self.assertEqual(upscaler.resolve_tier(tier), tier)

    def test_legacy_scale_maps_onto_the_nearest_tier(self):
        self.assertEqual(upscaler.resolve_tier(None, 2), "2K")
        self.assertEqual(upscaler.resolve_tier(None, 4), "4K")
        self.assertEqual(upscaler.resolve_tier(None, 1), "HD")
        self.assertEqual(upscaler.resolve_tier(None, 3), "2K")

    def test_unknown_tier_with_a_scale_maps_via_scale(self):
        self.assertEqual(upscaler.resolve_tier("bogus", 2), "2K")

    def test_missing_or_off_scale_is_rejected(self):
        # A caller that does not want an upscale must not call the endpoint at
        # all (WhisperRadar's upscale tier 0 means off) - no silent 4K fallback.
        for args in ((None, None), ("bogus", None), (None, 0), ("bogus", 0)):
            with self.assertRaises(ValueError):
                upscaler.resolve_tier(*args)

    def test_pre_rename_1k_tier_maps_onto_hd(self):
        self.assertEqual(upscaler.resolve_tier("1K"), "HD")


class LevelMapping(unittest.TestCase):
    def test_levels_map_to_tiers(self):
        self.assertIsNone(upscaler.level_to_tier(0))  # off = native
        self.assertEqual(upscaler.level_to_tier(1), "HD")
        self.assertEqual(upscaler.level_to_tier(2), "2K")
        self.assertEqual(upscaler.level_to_tier(3), "4K")
        self.assertIsNone(upscaler.level_to_tier(4))  # not a level any more
        self.assertIsNone(upscaler.level_to_tier(None))
        self.assertIsNone(upscaler.level_to_tier("x"))


if __name__ == "__main__":
    unittest.main()
