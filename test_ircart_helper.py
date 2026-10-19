import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


# The distributed helper has a hyphenated filename; bots copy it to their
# own importable runtime module name.
spec = importlib.util.spec_from_file_location(
    "ircart_helper_test_module", Path(__file__).with_name("ircart-helper.py"))
helper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = helper
spec.loader.exec_module(helper)


def asset(width, cells, encoding="rle"):
    return (f"IRCART/1\npalette=mirc99\nwidth={width}\nheight=1\n"
            f"glyph=upper-half\nencoding={encoding}\n\n{cells}\n")


class ByteBudgetTests(unittest.TestCase):
    def load_text(self, text, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.ircart"
            path.write_text(text, encoding="utf-8")
            return helper.load_for_irc(path, **kwargs)

    def test_wide_flat_row_accepted_at_exact_budget(self):
        for encoding, cells in (("rle", "98*04,01"),
                                ("plain", " ".join(["04,01"] * 98))):
            text = asset(98, cells, encoding)
            result = self.load_text(text, max_body_bytes=301)
            self.assertEqual(result.width, 98)
            self.assertEqual(result.utf8_bytes, (301,))
            self.assertEqual(result.lines[0].count("▀"), 98)
            with self.assertRaisesRegex(helper.IRCArtError, "line 1: 301 bytes"):
                self.load_text(text, max_body_bytes=300)

    def test_same_width_can_fail_from_colour_transitions(self):
        cells = " ".join(f"{4 if x % 2 else 2},01" for x in range(98))
        with self.assertRaisesRegex(helper.IRCArtError, "line 1: 883 bytes"):
            self.load_text(asset(98, cells, "plain"))

    def test_default_budget_and_explicit_override(self):
        text = asset(140, "140*04,01")
        with self.assertRaisesRegex(helper.IRCArtError, "400-byte limit"):
            self.load_text(text)
        self.assertEqual(self.load_text(text, max_body_bytes=427).utf8_bytes,
                         (427,))
        self.assertEqual(self.load_text(text, max_body_bytes=None).utf8_bytes,
                         (427,))

    def test_resource_guards_remain_independent(self):
        with self.assertRaisesRegex(helper.IRCArtError, "width 513 exceeds"):
            helper.parse_ircart(asset(513, "513*04,01"))
        with self.assertRaisesRegex(helper.IRCArtError, "width 98 exceeds"):
            self.load_text(asset(98, "98*04,01"), max_width=40)
        with self.assertRaisesRegex(helper.IRCArtError, "limit is 10"):
            helper.parse_ircart(asset(98, "98*04,01"), max_text_bytes=10)
        with self.assertRaisesRegex(helper.IRCArtError, "height 21 exceeds"):
            helper.parse_ircart(asset(1, "04,01").replace("height=1", "height=21"))

    def test_reset_is_included_in_exact_budget(self):
        text = asset(98, "98*04,01")
        result = self.load_text(text, max_body_bytes=300, reset_each_line=False)
        self.assertEqual(result.utf8_bytes, (300,))
        for budget in (0, -1):
            with self.assertRaises(ValueError):
                self.load_text(text, max_body_bytes=budget)


if __name__ == "__main__":
    unittest.main()
