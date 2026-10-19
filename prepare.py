#!/usr/bin/env python3
"""Prepare clean pixel art offline, without changing the IRCART renderer."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from math import gcd, isqrt
from pathlib import Path

from PIL import Image, ImageChops, ImageOps

from ircart import (
    IRCArt, PALETTES, colour_distance, image_to_ircart, render_irc_lines,
)


RGB = tuple[int, int, int]


@dataclass(frozen=True)
class PreparedImage:
    image: Image.Image
    art: IRCArt
    factor: int
    exact_grid: bool
    crop_box: tuple[int, int, int, int]
    utf8_bytes: tuple[int, ...]

    @property
    def max_utf8_bytes(self) -> int:
        return max(self.utf8_bytes, default=0)


def parse_rgb(value: str) -> RGB:
    text = value.strip().removeprefix("#")
    if len(text) != 6 or any(c not in "0123456789abcdefABCDEF" for c in text):
        raise argparse.ArgumentTypeError("colours must be six-digit RGB hex, e.g. ff00ff")
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))


def parse_colours(value: str) -> tuple[RGB, ...]:
    return tuple(dict.fromkeys(parse_rgb(part) for part in value.split(",")))


def parse_factor(value: str) -> int | None:
    if value.lower() == "auto":
        return None
    try:
        factor = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("factor must be 'auto' or a positive integer") from exc
    if factor < 1:
        raise argparse.ArgumentTypeError("factor must be 'auto' or a positive integer")
    return factor


def exact_grid_factor(image: Image.Image) -> int:
    """Largest square block size that can reconstruct this image exactly."""
    horizontal, vertical = image.size
    pixels = image.load()
    for y in range(image.height):
        for x in range(image.width):
            if x and pixels[x, y] != pixels[x - 1, y]:
                horizontal = gcd(horizontal, x)
            if y and pixels[x, y] != pixels[x, y - 1]:
                vertical = gcd(vertical, y)
            if horizontal == vertical == 1:
                return 1
    return gcd(horizontal, vertical)


def _divisors(value: int) -> list[int]:
    result = set()
    for divisor in range(1, isqrt(value) + 1):
        if value % divisor == 0:
            result.update((divisor, value // divisor))
    return sorted(result, reverse=True)


def prepare_image(
    image: Image.Image,
    *,
    factor: int | None = None,
    colours: tuple[RGB, ...] | None = None,
    background: RGB = (0, 0, 0),
    keep_frame: bool = False,
    border: int | None = None,
    require_exact: bool = False,
    max_width: int = 36,
    max_height: int = 18,
    max_body_bytes: int = 350,
    palette: str = "mirc99",
) -> PreparedImage:
    """Clean, trim, pad and reduce, with full rendering preflight before saving.

    Automatic mode prefers the largest exact square grid. If none fits, it
    chooses the smallest integer reduction meeting the output limits. An
    explicit factor is never silently changed. keep_frame requires an exact
    reduction, preserving alignment when regenerating an edited preview.
    """
    if factor is not None and (type(factor) is not int or factor < 1):
        raise ValueError("factor must be a positive integer or None for automatic")
    if any(type(v) is not int or v < 1 for v in (max_width, max_height, max_body_bytes)):
        raise ValueError("width, height and byte limits must be positive integers")
    if palette not in PALETTES:
        raise ValueError(f"unknown IRC palette: {palette}")
    border = (0 if keep_frame else 1) if border is None else border
    if type(border) is not int or border < 0:
        raise ValueError("border must be a non-negative integer")
    if keep_frame and border:
        raise ValueError("--keep-frame requires --border 0")
    colour_values = (background,) + (() if colours is None else tuple(colours))
    if any(len(c) != 3 or any(type(v) is not int or not 0 <= v <= 255 for v in c)
           for c in colour_values):
        raise ValueError("colours must be RGB triplets with channels in 0..255")
    if colours is not None and (not colours or background not in colours):
        raise ValueError("--colours must include the background colour")

    rgba = image.convert("RGBA")
    canvas = Image.new("RGBA", rgba.size, background + (255,))
    clean = Image.alpha_composite(canvas, rgba).convert("RGB")
    if colours is not None:
        mapping = {c: min(colours, key=lambda base: colour_distance(c, base))
                   for c in set(clean.getdata())}
        clean.putdata([mapping[c] for c in clean.getdata()])

    bounds = (0, 0, clean.width, clean.height)
    if not keep_frame:
        bounds = ImageChops.difference(
            clean, Image.new("RGB", clean.size, background)).getbbox()
        if bounds is None:
            raise ValueError("image contains only background after cleanup; use --keep-frame to retain it")
        clean = clean.crop(bounds)

    grid = exact_grid_factor(clean)
    exact_only = require_exact or keep_frame
    has_artwork = ImageChops.difference(
        clean, Image.new("RGB", clean.size, background)).getbbox() is not None

    def candidate(reduction: int) -> PreparedImage:
        exact = grid % reduction == 0
        if exact_only and not exact:
            raise ValueError(
                f"factor {reduction} does not match the cleaned pixel grid; "
                f"exact factors: {', '.join(map(str, _divisors(grid)))}"
            )
        width = (clean.width + reduction - 1) // reduction + 2 * border
        height = (clean.height + reduction - 1) // reduction + 2 * border
        lines = (height + 1) // 2
        if width > max_width or lines > max_height:
            raise ValueError(
                f"factor {reduction} needs {width} characters x {lines} IRC lines; "
                f"limits are {max_width} x {max_height}"
            )
        # Sample the virtually padded canvas without allocating a huge source
        # border. This matches an integer resize of the explicitly padded image.
        small = clean.transform(
            (width, height), Image.Transform.AFFINE,
            (reduction, 0, -border * reduction, 0, reduction, -border * reduction),
            resample=Image.Resampling.NEAREST, fillcolor=background,
        )
        if has_artwork and ImageChops.difference(
                small, Image.new("RGB", small.size, background)).getbbox() is None:
            raise ValueError(f"factor {reduction} removes all artwork; choose a smaller factor")
        if height % 2:
            small = ImageOps.expand(small, border=(0, 0, 0, 1), fill=background)
        art = image_to_ircart(
            small, no_scaling=True, max_width=max_width, max_height=max_height,
            palette_name=palette, dither="none", encoding="rle",
        )
        sizes = tuple(len(line.encode("utf-8")) for line in render_irc_lines(art))
        largest = max(sizes)
        if largest > max_body_bytes:
            raise ValueError(
                f"factor {reduction} renders a {largest}-byte line; "
                f"body budget is {max_body_bytes} bytes"
            )
        return PreparedImage(small, art, reduction, exact, bounds, sizes)

    if factor is not None:
        return candidate(factor)

    last_error = None
    for reduction in _divisors(grid):
        try:
            return candidate(reduction)
        except ValueError as exc:
            last_error = exc
    if exact_only:
        raise ValueError(f"no exact reduction fits the configured limits: {last_error}")

    available_width = max_width - 2 * border
    available_height = 2 * max_height - 2 * border
    if min(available_width, available_height) < 1:
        raise ValueError("output limits leave no room for artwork inside the border")
    first = max(2, (clean.width + available_width - 1) // available_width,
                (clean.height + available_height - 1) // available_height)
    for reduction in range(first, max(clean.size) + 1):
        if grid % reduction == 0:
            continue
        try:
            return candidate(reduction)
        except ValueError as exc:
            last_error = exc
    raise ValueError(f"no integer reduction fits the configured limits: {last_error}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="source image or enlarged edited preview")
    parser.add_argument("output", type=Path, help="prepared small PNG")
    parser.add_argument("--factor", type=parse_factor, default=None, metavar="auto|N",
                        help="integer reduction factor (default: auto)")
    parser.add_argument("--colours", type=parse_colours, metavar="HEX,HEX,...",
                        help="snap shades to these RGB colours; include the background")
    parser.add_argument("--background", type=parse_rgb, default=(0, 0, 0), metavar="HEX",
                        help="background for transparency, trimming and padding (default: 000000)")
    parser.add_argument("--keep-frame", action="store_true",
                        help="preserve an edited preview's frame and require an exact grid match")
    parser.add_argument("--border", type=int, metavar="N",
                        help="border in output pixels (default: 1; 0 with --keep-frame)")
    parser.add_argument("--require-exact", action="store_true",
                        help="reject reductions that discard cleaned source detail")
    parser.add_argument("--preview", type=Path, help="enlarged PNG (default: OUTPUTSTEM-preview.png)")
    parser.add_argument("--ircart", type=Path, help="also write an IRCART/1 asset")
    parser.add_argument("--palette", choices=sorted(PALETTES), default="mirc99",
                        help="IRC palette for byte checks and optional asset (default: mirc99)")
    parser.add_argument("--max-width", type=int, default=36, help="maximum IRC columns (default: 36)")
    parser.add_argument("--max-height", type=int, default=18, help="maximum IRC lines (default: 18)")
    parser.add_argument("--max-body-bytes", type=int, default=350,
                        help="maximum rendered UTF-8 body bytes; use your bot's budget (default: 350)")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    preview = args.preview or args.output.with_name(args.output.stem + "-preview.png")
    outputs = [args.output, preview] + ([args.ircart] if args.ircart else [])
    try:
        if args.output.suffix.lower() != ".png" or preview.suffix.lower() != ".png":
            raise ValueError("small image and preview outputs must use .png")
        resolved = [path.resolve() for path in outputs]
        if args.input.resolve() in resolved or len(set(resolved)) != len(resolved):
            raise ValueError("output paths must be distinct and must not overwrite the source")
        if any(not path.parent.is_dir() for path in outputs):
            raise ValueError("output directories must already exist")
        with Image.open(args.input) as image:
            prepared = prepare_image(
                image, factor=args.factor, colours=args.colours, background=args.background,
                keep_frame=args.keep_frame, border=args.border, require_exact=args.require_exact,
                max_width=args.max_width, max_height=args.max_height,
                max_body_bytes=args.max_body_bytes, palette=args.palette,
            )
        # All paths, grid checks, dimensions and byte budgets pass before writing.
        prepared.image.save(args.output)
        prepared.image.resize(
            (prepared.image.width * 12, prepared.image.height * 12),
            Image.Resampling.NEAREST,
        ).save(preview)
        if args.ircart:
            args.ircart.write_text(prepared.art.to_text(), encoding="utf-8")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))

    print(f"Reduction: {prepared.factor}:1; exact cleaned pixel grid: {prepared.exact_grid}")
    print(f"Prepared: {prepared.image.width} x {prepared.image.height} pixels; "
          f"{prepared.art.width} columns x {prepared.art.height} IRC lines")
    print(f"Maximum IRC body: {prepared.max_utf8_bytes} / {args.max_body_bytes} UTF-8 bytes")
    print(f"Saved: {args.output}; preview: {preview}")
    if args.ircart:
        print(f"IRCART: {args.ircart}")
    if not prepared.exact_grid:
        print("Source does not match this exact pixel grid; inspect the preview for lost detail.")


if __name__ == "__main__":
    main()
