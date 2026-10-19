import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

from PIL import Image

import ircart


class NativeSizeTests(unittest.TestCase):
    def test_even_height_preserves_each_palette_pixel(self):
        palette = ircart.palette_rgb("mirc99")
        image = Image.new("RGB", (3, 4))
        image.putdata([palette[i] for i in range(16, 28)])
        with warnings.catch_warnings(record=True) as caught:
            art = ircart.image_to_ircart(
                image, no_scaling=True, palette_name="mirc99")
        self.assertEqual((art.width, art.height), (3, 2))
        self.assertEqual(len(caught), 0)
        for y in range(4):
            for x in range(3):
                cell = art.rows[y // 2][x]
                self.assertEqual(palette[cell.bg if y % 2 else cell.fg],
                                 image.getpixel((x, y)))

    def test_odd_padding_does_not_affect_source_adjustments(self):
        image = Image.new("RGB", (3, 3))
        image.putdata([(i * 25, i * 12, i * 30) for i in range(9)])
        for mode in ("color", "grayscale"):
            for dither in ("none", "floyd-steinberg"):
                settings = dict(mode=mode, dither=dither, contrast=1.4,
                                brightness=1.2, palette_name="mirc99")
                adjusted = ircart.adjust_colours(
                    image, contrast=1.4, brightness=1.2)
                quantize = (ircart.quantize_none if dither == "none"
                            else ircart.quantize_floyd_steinberg)
                expected = quantize(adjusted, ircart.palette_rgb("mirc99"),
                                    mode=mode, palette_name="mirc99")
                with self.assertWarnsRegex(UserWarning, "height is 3 pixels"):
                    art = ircart.image_to_ircart(
                        image, no_scaling=True, padding_colour=4, **settings)
                self.assertEqual((art.width, art.height), (3, 2))
                for y in range(3):
                    for x in range(3):
                        cell = art.rows[y // 2][x]
                        self.assertEqual(cell.bg if y % 2 else cell.fg,
                                         expected[y][x])
                self.assertTrue(all(cell.bg == 4 for cell in art.rows[-1]))

    def test_limits_and_padding_validation(self):
        for size in ((37, 2), (2, 37)):
            with self.assertRaisesRegex(ValueError, "native image requires"):
                ircart.image_to_ircart(Image.new("RGB", size), no_scaling=True)
        for index in (-1, 16):
            with self.assertRaisesRegex(ValueError, "padding colour"):
                ircart.image_to_ircart(Image.new("RGB", (2, 3)),
                                      no_scaling=True, padding_colour=index)
        with self.assertWarns(UserWarning):
            art = ircart.image_to_ircart(Image.new("RGB", (1, 1)),
                                        no_scaling=True)
        self.assertEqual(art.rows[0][0].bg, 1)

    def test_default_scaling_still_fills_output_bounds(self):
        image = Image.new("RGB", (3, 4), "red")
        art = ircart.image_to_ircart(image)
        self.assertEqual((art.width, art.height), (27, 18))
        self.assertTrue(all(cell == ircart.Cell(4, 4)
                            for row in art.rows for cell in row))

    def test_cli_file_and_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGB", (26, 31), "red").save(root / "input.png")
            result = subprocess.run(
                [sys.executable, str(Path(ircart.__file__)), "convert",
                 str(root / "input.png"), str(root / "output.ircart"),
                 "--no-scaling", "--padding-colour", "2",
                 "--preview", str(root / "preview.png")],
                capture_output=True, text=True, check=True)
            self.assertIn("added one background row", result.stderr)
            art = ircart.load_ircart(root / "output.ircart")
            self.assertEqual((art.width, art.height), (26, 16))
            self.assertTrue(all(cell.bg == 2 for cell in art.rows[-1]))
            with Image.open(root / "preview.png") as preview:
                self.assertEqual(preview.size, (312, 384))


class ResamplingTests(unittest.TestCase):
    def test_nearest_integer_downscale_selects_exact_source_pixels(self):
        palette = ircart.palette_rgb("mirc16")
        image = Image.new("RGB", (4, 4), "black")
        for position, index in (((1, 1), 4), ((3, 1), 2),
                                ((1, 3), 1), ((3, 3), 0)):
            image.putpixel(position, palette[index])
        art = ircart.image_to_ircart(
            image, max_width=2, max_height=1, resample="nearest")
        self.assertEqual((art.width, art.height), (2, 1))
        self.assertEqual(art.rows, ((ircart.Cell(4, 1), ircart.Cell(2, 0)),))

    def test_nearest_keeps_two_colours_at_noninteger_scale(self):
        image = Image.new("RGB", (8, 8))
        image.putdata([(255, 255, 255) if (x + y) % 2 else (0, 0, 0)
                       for y in range(8) for x in range(8)])
        settings = dict(max_width=5, max_height=3, palette_name="mirc99")
        nearest = ircart.image_to_ircart(image, resample="nearest", **settings)
        smooth = ircart.image_to_ircart(image, resample="lanczos", **settings)
        nearest_colours = {value for row in nearest.rows for cell in row
                           for value in (cell.fg, cell.bg)}
        smooth_colours = {value for row in smooth.rows for cell in row
                          for value in (cell.fg, cell.bg)}
        self.assertEqual(nearest_colours, {0, 1})
        self.assertTrue(smooth_colours - {0, 1})

    def test_default_matches_explicit_lanczos(self):
        image = Image.new("RGB", (7, 5))
        image.putdata([(i * 17 % 256, i * 31 % 256, i * 47 % 256)
                       for i in range(35)])
        for palette in ("mirc16", "mirc99"):
            for mode in ("color", "grayscale"):
                for dither in ("none", "floyd-steinberg"):
                    settings = dict(palette_name=palette, mode=mode, dither=dither,
                                    max_width=5, max_height=2)
                    self.assertEqual(
                        ircart.image_to_ircart(image, **settings).to_text(),
                        ircart.image_to_ircart(
                            image, resample="lanczos", **settings).to_text())

    def test_no_scaling_bypasses_filter_and_invalid_method_is_rejected(self):
        image = Image.new("RGB", (4, 4), "red")
        self.assertEqual(
            ircart.image_to_ircart(image, no_scaling=True).to_text(),
            ircart.image_to_ircart(
                image, no_scaling=True, resample="nearest").to_text())
        for no_scaling in (True, False):
            with self.assertRaisesRegex(ValueError, "unknown resampling method"):
                ircart.image_to_ircart(
                    image, resample="invalid", no_scaling=no_scaling)

    def test_cli_passes_filter_through_conversion_and_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = Image.new("RGB", (8, 8))
            image.putdata([(255, 255, 255) if (x + y) % 2 else (0, 0, 0)
                           for y in range(8) for x in range(8)])
            image.save(root / "input.png")
            subprocess.run(
                [sys.executable, "-B", str(Path(ircart.__file__)), "convert",
                 str(root / "input.png"), str(root / "out.ircart"),
                 "--resample", "nearest", "--max-width", "5", "--max-height", "3",
                 "--palette", "mirc99", "--preview", str(root / "preview.png")],
                capture_output=True, text=True, check=True)
            actual = ircart.load_ircart(root / "out.ircart")
            expected = ircart.image_to_ircart(
                image, max_width=5, max_height=3, palette_name="mirc99",
                resample="nearest")
            self.assertEqual(actual, expected)
            with Image.open(root / "preview.png") as preview:
                self.assertEqual(set(preview.getdata()), {(0, 0, 0), (255, 255, 255)})


if __name__ == "__main__":
    unittest.main()
