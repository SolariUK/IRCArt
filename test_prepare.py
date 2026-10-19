import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops

import ircart
import prepare


def enlarged_sprite(scale=3):
    logical = Image.new("RGB", (4, 4), "white")
    logical.putpixel((1, 1), (0, 0, 0))
    logical.putpixel((2, 2), (0, 0, 0))
    enlarged = logical.resize((4 * scale, 4 * scale), Image.Resampling.NEAREST)
    source = Image.new("RGB", (enlarged.width + 7, enlarged.height + 9), "black")
    source.paste(enlarged, (2, 5))
    return logical, source


class PreparationTests(unittest.TestCase):
    def test_auto_finds_exact_grid_after_trimming_offset_margins(self):
        logical, source = enlarged_sprite()
        original = source.tobytes()
        result = prepare.prepare_image(source, require_exact=True)
        self.assertEqual(result.factor, 3)
        self.assertTrue(result.exact_grid)
        self.assertEqual(result.crop_box, (2, 5, 14, 17))
        self.assertEqual(result.image.size, (6, 6))
        self.assertEqual(result.image.crop((1, 1, 5, 5)).tobytes(), logical.tobytes())
        self.assertEqual(source.tobytes(), original)
        self.assertEqual(result.art.height, 3)

    def test_explicit_sampling_matches_padded_canvas_and_even_row(self):
        source = Image.new("RGB", (7, 5))
        source.putdata([(i * 17 % 256, i * 31 % 256, i * 47 % 256)
                        for i in range(35)])
        # No source pixel is the background, so trimming retains the full frame.
        source.putpixel((0, 0), (1, 1, 1))
        result = prepare.prepare_image(source, factor=3)
        canvas = Image.new("RGB", (15, 12), "black")
        canvas.paste(source, (3, 3))
        expected = canvas.resize((5, 4), Image.Resampling.NEAREST)
        self.assertEqual(result.image.tobytes(), expected.tobytes())
        self.assertFalse(result.exact_grid)
        with self.assertRaisesRegex(ValueError, "does not match"):
            prepare.prepare_image(source, factor=3, require_exact=True)

    def test_odd_height_gets_one_background_row_without_stretching(self):
        source = Image.new("RGB", (3, 3), "white")
        result = prepare.prepare_image(source, factor=1, border=0)
        self.assertEqual(result.image.size, (3, 4))
        self.assertEqual(result.image.crop((0, 0, 3, 3)).tobytes(), source.tobytes())
        self.assertTrue(all(result.image.getpixel((x, 3)) == (0, 0, 0)
                            for x in range(3)))

    def test_colour_cleanup_preserves_six_separate_dots_in_edited_grid(self):
        logical = Image.new("RGB", (9, 6), "white")
        dots = {(x, y) for x in (2, 4, 6) for y in (1, 3)}
        for x, y in dots:
            logical.putpixel((x, y), (19, 19, 19) if y == 1 else (0, 0, 0))
        logical.putpixel((0, 5), (255, 0, 255))
        enlarged = logical.resize((108, 72), Image.Resampling.NEAREST)
        result = prepare.prepare_image(
            enlarged, factor=12, keep_frame=True,
            colours=((0, 0, 0), (255, 255, 255), (255, 0, 255)))
        self.assertEqual(result.image.size, (9, 6))
        self.assertEqual({(x, y) for y in range(5) for x in range(9)
                          if result.image.getpixel((x, y)) == (0, 0, 0)}, dots)
        self.assertEqual(set(result.image.getdata()),
                         {(0, 0, 0), (255, 255, 255), (255, 0, 255)})

    def test_keep_frame_rejects_off_grid_edits(self):
        _, source = enlarged_sprite()
        with self.assertRaisesRegex(ValueError, "does not match"):
            prepare.prepare_image(source, factor=3, keep_frame=True)
        with self.assertRaisesRegex(ValueError, "border 0"):
            prepare.prepare_image(source, keep_frame=True, border=1)

    def test_transparency_and_nonblack_background_are_consistent(self):
        source = Image.new("RGBA", (6, 6), (255, 0, 0, 0))
        for y in (2, 3):
            for x in (2, 3):
                source.putpixel((x, y), (0, 0, 0, 255))
        result = prepare.prepare_image(
            source, background=(255, 255, 255),
            colours=((0, 0, 0), (255, 255, 255)), require_exact=True)
        self.assertEqual(result.crop_box, (2, 2, 4, 4))
        self.assertEqual(result.factor, 2)
        self.assertEqual(result.image.size, (3, 4))
        self.assertEqual(result.image.getpixel((1, 1)), (0, 0, 0))
        self.assertTrue(all(result.image.getpixel((x, 3)) == (255, 255, 255)
                            for x in range(3)))

    def test_auto_falls_back_only_when_no_exact_reduction_fits(self):
        _, source = enlarged_sprite(scale=2)
        result = prepare.prepare_image(source, border=0, max_width=3, max_height=2)
        self.assertEqual(result.factor, 3)
        self.assertFalse(result.exact_grid)
        self.assertEqual(result.image.size, (3, 4))
        with self.assertRaisesRegex(ValueError, "no exact reduction fits"):
            prepare.prepare_image(source, border=0, max_width=3, max_height=2,
                                  require_exact=True)

    def test_auto_uses_actual_rendered_byte_budget(self):
        source = Image.new("RGB", (6, 6))
        source.putdata([(255, 255, 255) if (x + y) % 2 else (0, 0, 0)
                        for y in range(6) for x in range(6)])
        result = prepare.prepare_image(source, border=0, max_body_bytes=25)
        self.assertGreater(result.factor, 1)
        self.assertLessEqual(result.max_utf8_bytes, 25)
        exact_sizes = tuple(len(line.encode("utf-8"))
                            for line in ircart.render_irc_lines(result.art))
        self.assertEqual(result.utf8_bytes, exact_sizes)
        with self.assertRaisesRegex(ValueError, "body budget"):
            prepare.prepare_image(source, factor=1, border=0, max_body_bytes=25)

    def test_validation_and_background_only_input(self):
        source = Image.new("RGB", (4, 4), "white")
        for options in ({"factor": 0}, {"factor": -2}, {"max_height": 0},
                        {"max_body_bytes": 0}, {"border": -1},
                        {"colours": ((255, 255, 255),)}, {"colours": ()},
                        {"palette": "unknown"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                prepare.prepare_image(source, **options)
        with self.assertRaisesRegex(ValueError, "only background"):
            prepare.prepare_image(Image.new("RGB", (4, 4), "black"))
        result = prepare.prepare_image(Image.new("RGB", (4, 4), "black"),
                                       keep_frame=True)
        self.assertEqual(result.image.size, (1, 2))

    def test_sampling_must_not_silently_erase_all_artwork(self):
        source = Image.new("RGB", (1, 10), "white")
        with self.assertRaisesRegex(ValueError, "removes all artwork"):
            prepare.prepare_image(source, factor=10)

    def test_palette_and_reported_pixels_match_the_generated_asset(self):
        _, source = enlarged_sprite()
        for palette in ("mirc16", "mirc99"):
            result = prepare.prepare_image(source, palette=palette)
            parsed = ircart.parse_ircart_text(result.art.to_text())
            self.assertEqual(parsed, result.art)
            self.assertEqual(ImageChops.difference(
                ircart.render_preview(parsed, cell_px=1), result.image).getbbox(), None)


class PreparationCLITests(unittest.TestCase):
    def invoke(self, *args):
        return subprocess.run([sys.executable, "-B", str(Path(prepare.__file__)),
                               *map(str, args)], capture_output=True, text=True)

    def test_writes_png_preview_and_optional_ircart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, asset = root / "source.png", root / "small.png", root / "art.ircart"
            _, image = enlarged_sprite()
            image.save(source)
            original = source.read_bytes()
            run = self.invoke(source, output, "--colours", "000000,ffffff",
                              "--require-exact", "--ircart", asset)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("Reduction: 3:1; exact cleaned pixel grid: True", run.stdout)
            self.assertEqual(source.read_bytes(), original)
            with Image.open(output) as small, Image.open(root / "small-preview.png") as preview:
                self.assertEqual(small.size, (6, 6))
                self.assertEqual(preview.size, (72, 72))
                self.assertEqual(set(small.getdata()), {(0, 0, 0), (255, 255, 255)})
            self.assertEqual(ircart.load_ircart(asset).height, 3)

    def test_errors_leave_existing_outputs_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "source.png", root / "small.png"
            Image.new("RGB", (4, 4), "white").save(source)
            output.write_bytes(b"previous output")
            cases = [(source, output, "--factor", "0"),
                     (source, source),
                     (source, output, "--preview", output),
                     (source, output, "--ircart", source),
                     (source, output, "--max-body-bytes", "1"),
                     (source, output, "--colours", "nothex"),
                     (source, root / "small.jpg")]
            original = source.read_bytes()
            for args in cases:
                with self.subTest(args=args):
                    run = self.invoke(*args)
                    self.assertNotEqual(run.returncode, 0)
                    self.assertEqual(source.read_bytes(), original)
                    self.assertEqual(output.read_bytes(), b"previous output")
                    self.assertFalse((root / "small-preview.png").exists())


if __name__ == "__main__":
    unittest.main()
