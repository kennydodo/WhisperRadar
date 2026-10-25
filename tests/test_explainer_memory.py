"""Explainer scenes must not hold full-frame pictures or one ffmpeg input per typed character."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw  # noqa: E402

from whisperradar.video.explainer import compose, pipe, plates, schema as sc  # noqa: E402


def _plate(text_w, canvas=(1920, 1080), at=(300, 200)):
    img = Image.new("RGBA", canvas, (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle([at[0], at[1], at[0] + text_w, at[1] + 40], fill=(255, 255, 255, 255))
    return img


class CropTests(unittest.TestCase):
    def test_plates_shrink_to_the_text_and_keep_their_position(self):
        full = [_plate(50), _plate(200), _plate(400)]
        cropped, origin = plates.crop_to_content(full)
        self.assertEqual(origin, (300, 200))
        self.assertEqual({p.size for p in cropped}, {(401, 41)})     # the union, same for all
        self.assertLess(cropped[0].size[0] * cropped[0].size[1], 1920 * 1080 / 100)

    def test_pasting_the_crop_at_its_origin_reproduces_the_original(self):
        original = _plate(300, at=(120, 700))
        (cropped,), origin = plates.crop_to_content([original])
        canvas = Image.new("RGBA", original.size, (0, 0, 0, 0))
        canvas.paste(cropped, origin)
        self.assertEqual(canvas.tobytes(), original.tobytes())

    def test_an_empty_plate_stays_a_tiny_plate(self):
        (p,), origin = plates.crop_to_content([Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))])
        self.assertEqual((p.size, origin), ((1, 1), (0, 0)))


class BackendChoiceTests(unittest.TestCase):
    def _scene(self, actions, objects=(sc.LABEL,)):
        return NS(spec=NS(objects=[NS(type=t) for t in objects]),
                  events=[NS(event=NS(action=a)) for a in actions])

    def test_typed_on_text_goes_through_the_frame_pipe(self):
        self.assertTrue(compose.needs_pipe(self._scene([sc.TYPE_ON])))

    def test_plain_fades_stay_in_the_filtergraph(self):
        self.assertFalse(compose.needs_pipe(self._scene([sc.FADE_IN], (sc.TEXT,))))

    def test_paths_still_need_the_pipe(self):
        self.assertTrue(compose.needs_pipe(self._scene([sc.FADE_IN], (sc.PATH,))))


class GentleOnThePcTests(unittest.TestCase):
    def test_encoder_gets_a_thread_cap(self):
        args = pipe.encode_threads()
        self.assertEqual(args[0], "-threads")
        self.assertGreaterEqual(int(args[1]), 1)

    def test_flags_are_valid_for_this_platform(self):
        flags = pipe.process_flags()
        self.assertIsInstance(flags, int)
        if sys.platform != "win32":
            self.assertEqual(flags, 0)


if __name__ == "__main__":
    unittest.main()
