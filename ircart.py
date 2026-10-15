#!/usr/bin/env python3
"""
ircart.py - offline image -> compact IRCART/1 converter and renderer.

The .ircart format is safe UTF-8 text containing palette indexes only.
A separate renderer turns it into IRC control-coded lines for sending.

Requires:
    pip install pillow


Example:

python ircart.py convert picard.jpg picard.ircart \
  --palette mirc99 \
  --dither none \
  --encoding rle

"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import argparse
from typing import Iterable

from PIL import Image


# ---------- Palettes ----------

MIRC16 = (
    ("white",       (255, 255, 255)),
    ("black",       (0, 0, 0)),
    ("blue",        (0, 0, 127)),
    ("green",       (0, 147, 0)),
    ("light-red",   (255, 0, 0)),
    ("brown",       (127, 0, 0)),
    ("purple",      (156, 0, 156)),
    ("orange",      (252, 127, 0)),
    ("yellow",      (255, 255, 0)),
    ("light-green", (0, 252, 0)),
    ("cyan",        (0, 147, 147)),
    ("light-cyan",  (0, 255, 255)),
    ("light-blue",  (0, 0, 252)),
    ("pink",        (255, 0, 255)),
    ("grey",        (127, 127, 127)),
    ("light-grey",  (210, 210, 210)),
)

_EXTENDED_HEX = (
    "470000","472100","474700","324700","004700","00472c","004747","002747",
    "000047","2e0047","470047","47002a",
    "740000","743a00","747400","517400","007400","007449","007474","004074",
    "000074","4b0074","740074","740045",
    "b50000","b56300","b5b500","7db500","00b500","00b571","00b5b5","0063b5",
    "0000b5","7500b5","b500b5","b5006b",
    "ff0000","ff8c00","ffff00","b2ff00","00ff00","00ffa0","00ffff","008cff",
    "0000ff","a500ff","ff00ff","ff0098",
    "ff5959","ffb459","ffff71","cfff60","6fff6f","65ffc9","6dffff","59b4ff",
    "5959ff","c459ff","ff66ff","ff59bc",
    "ff9c9c","ffd39c","ffff9c","e2ff9c","9cff9c","9cffdb","9cffff","9cd3ff",
    "9c9cff","dc9cff","ff9cff","ff94d3",
    "000000","131313","282828","363636","4d4d4d","656565","818181","9f9f9f",
    "bcbcbc","e2e2e2","ffffff",
)

def _hex_rgb(s: str) -> tuple[int, int, int]:
    return tuple(int(s[i:i+2], 16) for i in (0, 2, 4))

MIRC99 = MIRC16 + tuple(
    (f"extended-{i}", _hex_rgb(value))
    for i, value in enumerate(_EXTENDED_HEX, start=16)
)

PALETTES = {
    "mirc16": MIRC16,
    "mirc99": MIRC99,  # indexes 0..98, leaving 99 reserved as IRC "default"
}


# ---------- Data model ----------

@dataclass(frozen=True)
class Cell:
    fg: int
    bg: int


@dataclass(frozen=True)
class IRCArt:
    width: int
    height: int
    rows: tuple[tuple[Cell, ...], ...]
    palette: str = "mirc16"
    glyph: str = "upper-half"
    encoding: str = "plain"

    def validate(self) -> None:
        if self.palette not in PALETTES:
            raise ValueError(f"unknown palette: {self.palette}")
        if self.glyph != "upper-half":
            raise ValueError(f"unsupported glyph mode: {self.glyph}")
        if self.encoding not in {"plain", "rle"}:
            raise ValueError(f"unsupported encoding: {self.encoding}")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("width and height must be positive")
        if len(self.rows) != self.height:
            raise ValueError("row count does not match height")

        limit = len(PALETTES[self.palette])
        for y, row in enumerate(self.rows):
            if len(row) != self.width:
                raise ValueError(
                    f"row {y} has {len(row)} cells, expected {self.width}"
                )
            for cell in row:
                if not (0 <= cell.fg < limit and 0 <= cell.bg < limit):
                    raise ValueError(
                        f"{self.palette} colour index out of range"
                    )

    def to_text(self) -> str:
        self.validate()
        header = [
            "IRCART/1",
            f"palette={self.palette}",
            f"width={self.width}",
            f"height={self.height}",
            f"glyph={self.glyph}",
            f"encoding={self.encoding}",
            "",
        ]
        if self.encoding == "plain":
            body = [
                " ".join(f"{cell.fg:02d},{cell.bg:02d}" for cell in row)
                for row in self.rows
            ]
        elif self.encoding == "rle":
            body = [serialize_rle_row(row) for row in self.rows]
        else:
            raise ValueError(f"unsupported encoding: {self.encoding}")
        return "\n".join(header + body) + "\n"


# ---------- Conversion ----------

def palette_rgb(name: str) -> tuple[tuple[int, int, int], ...]:
    try:
        return tuple(rgb for _, rgb in PALETTES[name])
    except KeyError as exc:
        raise ValueError(f"unknown palette: {name}") from exc


def colour_distance(a: tuple[float, float, float],
                    b: tuple[int, int, int]) -> float:
    dr = a[0] - b[0]
    dg = a[1] - b[1]
    db = a[2] - b[2]
    return 3 * dr * dr + 4 * dg * dg + 2 * db * db


def nearest_palette_index(
    rgb: tuple[float, float, float],
    palette: tuple[tuple[int, int, int], ...],
) -> int:
    return min(range(len(palette)), key=lambda i: colour_distance(rgb, palette[i]))


def choose_output_size(
    source_width: int,
    source_height: int,
    max_width: int = 36,
    max_height: int = 18,
) -> tuple[int, int]:
    """
    Preserve visual aspect ratio for upper-half-block output.

    One IRC character column maps to one sampled pixel horizontally.
    One IRC line maps to two sampled pixels vertically.
    """
    if source_width <= 0 or source_height <= 0:
        raise ValueError("invalid source dimensions")
    if max_width <= 0 or max_height <= 0:
        raise ValueError("invalid output bounds")

    source_aspect = source_width / source_height
    h_if_width_limited = max(1, round(max_width / (2.0 * source_aspect)))

    if h_if_width_limited <= max_height:
        return max_width, h_if_width_limited

    width = max(1, round(2.0 * max_height * source_aspect))
    return min(width, max_width), max_height


def quantize_none(
    image: Image.Image,
    palette: tuple[tuple[int, int, int], ...],
) -> list[list[int]]:
    return [
        [nearest_palette_index(image.getpixel((x, y)), palette)
         for x in range(image.width)]
        for y in range(image.height)
    ]


def quantize_floyd_steinberg(
    image: Image.Image,
    palette: tuple[tuple[int, int, int], ...],
) -> list[list[int]]:
    w, h = image.size
    work = [
        [list(map(float, image.getpixel((x, y)))) for x in range(w)]
        for y in range(h)
    ]
    result = [[0] * w for _ in range(h)]

    def add_error(x: int, y: int, err, scale: float) -> None:
        if 0 <= x < w and 0 <= y < h:
            p = work[y][x]
            for c in range(3):
                p[c] = max(0.0, min(255.0, p[c] + err[c] * scale))

    for y in range(h):
        for x in range(w):
            old = tuple(work[y][x])
            idx = nearest_palette_index(old, palette)
            new = palette[idx]
            result[y][x] = idx
            err = tuple(old[c] - new[c] for c in range(3))

            add_error(x + 1, y,     err, 7 / 16)
            add_error(x - 1, y + 1, err, 3 / 16)
            add_error(x,     y + 1, err, 5 / 16)
            add_error(x + 1, y + 1, err, 1 / 16)

    return result


def image_to_ircart(
    image: Image.Image,
    max_width: int = 36,
    max_height: int = 18,
    palette_name: str = "mirc16",
    dither: str = "none",
    encoding: str = "plain",
) -> IRCArt:
    image = image.convert("RGB")
    width, height = choose_output_size(
        image.width, image.height,
        max_width=max_width, max_height=max_height
    )

    sample_height = height * 2
    resized = image.resize((width, sample_height), Image.Resampling.LANCZOS)
    palette = palette_rgb(palette_name)

    if dither == "none":
        q = quantize_none(resized, palette)
    elif dither == "floyd-steinberg":
        q = quantize_floyd_steinberg(resized, palette)
    else:
        raise ValueError(f"unknown dither mode: {dither}")

    rows = []
    for line_y in range(height):
        top_y = line_y * 2
        bottom_y = top_y + 1
        rows.append(tuple(
            Cell(fg=q[top_y][x], bg=q[bottom_y][x])
            for x in range(width)
        ))

    art = IRCArt(
        width=width,
        height=height,
        rows=tuple(rows),
        palette=palette_name,
        encoding=encoding,
    )
    art.validate()
    return art


def render_preview(art: IRCArt, cell_px: int = 12) -> Image.Image:
    palette = palette_rgb(art.palette)
    img = Image.new("RGB", (art.width * cell_px, art.height * 2 * cell_px))
    px = img.load()

    for cy, row in enumerate(art.rows):
        for cx, cell in enumerate(row):
            top = palette[cell.fg]
            bottom = palette[cell.bg]
            x0 = cx * cell_px
            y0 = cy * 2 * cell_px

            for yy in range(y0, y0 + cell_px):
                for xx in range(x0, x0 + cell_px):
                    px[xx, yy] = top

            for yy in range(y0 + cell_px, y0 + 2 * cell_px):
                for xx in range(x0, x0 + cell_px):
                    px[xx, yy] = bottom

    return img


def convert_file(
    input_path: Path,
    output_path: Path,
    max_width: int = 36,
    max_height: int = 18,
    palette_name: str = "mirc16",
    dither: str = "none",
    encoding: str = "plain",
    preview_path: Path | None = None,
) -> IRCArt:
    with Image.open(input_path) as image:
        art = image_to_ircart(
            image,
            max_width=max_width,
            max_height=max_height,
            palette_name=palette_name,
            dither=dither,
            encoding=encoding,
        )

    output_path.write_text(art.to_text(), encoding="utf-8")

    if preview_path is not None:
        render_preview(art).save(preview_path)

    return art


def serialize_rle_row(row: tuple[Cell, ...]) -> str:
    """
    Serialize one row using a simple run-length syntax:

        4*12,34 05,06 3*07,08

    A count of 1 is written without the "1*" prefix.
    """
    if not row:
        return ""

    tokens: list[str] = []
    current = row[0]
    count = 1

    def emit(cell: Cell, run: int) -> None:
        base = f"{cell.fg:02d},{cell.bg:02d}"
        tokens.append(base if run == 1 else f"{run}*{base}")

    for cell in row[1:]:
        if cell == current:
            count += 1
        else:
            emit(current, count)
            current = cell
            count = 1
    emit(current, count)
    return " ".join(tokens)


# ---------- Parsing ----------

def parse_kv_header(lines: list[str]) -> tuple[dict[str, str], int]:
    if not lines or lines[0].strip() != "IRCART/1":
        raise ValueError("missing or invalid IRCART/1 magic header")

    header: dict[str, str] = {}
    i = 1
    while i < len(lines):
        line = lines[i].strip()
        if line == "":
            return header, i + 1
        if "=" not in line:
            raise ValueError(f"invalid header line: {lines[i]!r}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"empty header key in line: {lines[i]!r}")
        if key in header:
            raise ValueError(f"duplicate header key: {key}")
        header[key] = value
        i += 1

    raise ValueError("missing blank line after header")


def parse_cell_token(token: str, limit: int) -> Cell:
    if token.count(",") != 1:
        raise ValueError(f"invalid cell token: {token!r}")
    fg_s, bg_s = token.split(",", 1)
    if not fg_s.isdigit() or not bg_s.isdigit():
        raise ValueError(f"non-numeric cell token: {token!r}")
    fg = int(fg_s, 10)
    bg = int(bg_s, 10)
    if not (0 <= fg < limit and 0 <= bg < limit):
        raise ValueError(f"cell token out of range: {token!r}")
    return Cell(fg=fg, bg=bg)


def parse_rle_token(token: str, limit: int, remaining: int) -> tuple[int, Cell]:
    if "*" in token:
        if token.count("*") != 1:
            raise ValueError(f"invalid RLE token: {token!r}")
        count_s, cell_s = token.split("*", 1)
        if not count_s.isdigit():
            raise ValueError(f"invalid RLE count: {token!r}")
        count = int(count_s, 10)
        if count <= 1:
            raise ValueError(f"RLE count must be >= 2: {token!r}")
    else:
        count = 1
        cell_s = token

    if count > remaining:
        raise ValueError(
            f"RLE token expands past row width: {token!r}"
        )

    return count, parse_cell_token(cell_s, limit)


def parse_ircart_text(
    text: str,
    *,
    max_width: int = 40,
    max_height: int = 20,
) -> IRCArt:
    lines = text.splitlines()
    header, body_start = parse_kv_header(lines)

    required = {"palette", "width", "height", "glyph", "encoding"}
    missing = required - set(header)
    if missing:
        raise ValueError(f"missing header keys: {', '.join(sorted(missing))}")

    palette = header["palette"]
    glyph = header["glyph"]
    encoding = header["encoding"]

    if palette not in PALETTES:
        raise ValueError(f"unknown palette: {palette}")
    if glyph != "upper-half":
        raise ValueError(f"unsupported glyph: {glyph}")
    if encoding not in {"plain", "rle"}:
        raise ValueError(f"unsupported encoding: {encoding}")

    try:
        width = int(header["width"], 10)
        height = int(header["height"], 10)
    except ValueError as exc:
        raise ValueError("width and height must be integers") from exc

    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    if width > max_width:
        raise ValueError(f"width {width} exceeds limit {max_width}")
    if height > max_height:
        raise ValueError(f"height {height} exceeds limit {max_height}")

    limit = len(PALETTES[palette])
    body_lines = lines[body_start:]
    if len(body_lines) != height:
        raise ValueError(
            f"body has {len(body_lines)} row(s), expected {height}"
        )

    rows = []
    for row_index, line in enumerate(body_lines):
        tokens = line.split()

        if encoding == "plain":
            if len(tokens) != width:
                raise ValueError(
                    f"row {row_index} has {len(tokens)} token(s), expected {width}"
                )
            row = tuple(parse_cell_token(tok, limit) for tok in tokens)

        elif encoding == "rle":
            expanded: list[Cell] = []
            for tok in tokens:
                remaining = width - len(expanded)
                count, cell = parse_rle_token(tok, limit, remaining)
                expanded.extend([cell] * count)
            if len(expanded) != width:
                raise ValueError(
                    f"row {row_index} expands to {len(expanded)} cell(s), "
                    f"expected {width}"
                )
            row = tuple(expanded)

        else:
            raise AssertionError(f"unhandled encoding: {encoding}")

        rows.append(row)

    art = IRCArt(
        width=width,
        height=height,
        rows=tuple(rows),
        palette=palette,
        glyph=glyph,
        encoding=encoding,
    )
    art.validate()
    return art


def load_ircart(
    path: Path,
    *,
    max_width: int = 40,
    max_height: int = 20,
) -> IRCArt:
    return parse_ircart_text(
        path.read_text(encoding="utf-8"),
        max_width=max_width,
        max_height=max_height,
    )


# ---------- IRC rendering ----------

IRC_COLOR = "\x03"
IRC_RESET = "\x0f"
HALF_BLOCK = "▀"


def render_irc_line(row: tuple[Cell, ...], *, reset: bool = True) -> str:
    """
    Render one row of cells as optimized IRC text.

    We emit a colour control code only when the fg/bg pair changes, and then
    repeat the Unicode half-block glyph for the run length.
    """
    if not row:
        return IRC_RESET if reset else ""

    parts: list[str] = []
    current = None
    run_len = 0

    def flush(pair: tuple[int, int] | None, length: int) -> None:
        if pair is None or length <= 0:
            return
        fg, bg = pair
        parts.append(f"{IRC_COLOR}{fg:02d},{bg:02d}" + (HALF_BLOCK * length))

    for cell in row:
        pair = (cell.fg, cell.bg)
        if current is None:
            current = pair
            run_len = 1
        elif pair == current:
            run_len += 1
        else:
            flush(current, run_len)
            current = pair
            run_len = 1

    flush(current, run_len)

    if reset:
        parts.append(IRC_RESET)

    return "".join(parts)


def render_irc_lines(art: IRCArt, *, reset: bool = True) -> list[str]:
    art.validate()
    return [render_irc_line(row, reset=reset) for row in art.rows]


def escape_control_codes(s: str) -> str:
    """
    Convert raw IRC control chars to visible escape sequences for debugging.
    """
    out = []
    for ch in s:
        if ch == IRC_COLOR:
            out.append(r"\x03")
        elif ch == IRC_RESET:
            out.append(r"\x0f")
        elif ch == "\n":
            out.append(r"\n")
        else:
            out.append(ch)
    return "".join(out)


def render_file(
    input_path: Path,
    output_path: Path,
    *,
    max_width: int = 40,
    max_height: int = 20,
    reset: bool = True,
    escape: bool = False,
) -> IRCArt:
    art = load_ircart(
        input_path,
        max_width=max_width,
        max_height=max_height,
    )
    lines = render_irc_lines(art, reset=reset)
    if escape:
        text = "\n".join(escape_control_codes(line) for line in lines) + "\n"
    else:
        text = "\n".join(lines) + "\n"
    output_path.write_text(text, encoding="utf-8")
    return art


# ---------- CLI ----------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Convert, parse, and render IRCART/1 image art."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_convert = sub.add_parser("convert", help="convert image to IRCART/1")
    p_convert.add_argument("input", type=Path)
    p_convert.add_argument("output", type=Path)
    p_convert.add_argument("--max-width", type=int, default=36)
    p_convert.add_argument("--max-height", type=int, default=18)
    p_convert.add_argument(
        "--palette",
        choices=sorted(PALETTES),
        default="mirc16",
    )
    p_convert.add_argument(
        "--dither",
        choices=("none", "floyd-steinberg"),
        default="none",
    )
    p_convert.add_argument(
        "--encoding",
        choices=("plain", "rle"),
        default="plain",
        help="IRCART row encoding (default: plain)",
    )
    p_convert.add_argument("--preview", type=Path)

    p_render = sub.add_parser("render", help="render IRCART/1 to IRC text")
    p_render.add_argument("input", type=Path)
    p_render.add_argument("output", type=Path)
    p_render.add_argument("--max-width", type=int, default=40)
    p_render.add_argument("--max-height", type=int, default=20)
    p_render.add_argument(
        "--no-reset",
        action="store_true",
        help="omit trailing IRC reset on each line",
    )
    p_render.add_argument(
        "--escape",
        action="store_true",
        help="write visible escape sequences instead of raw IRC controls",
    )

    p_inspect = sub.add_parser("inspect", help="validate and summarize IRCART/1")
    p_inspect.add_argument("input", type=Path)
    p_inspect.add_argument("--max-width", type=int, default=40)
    p_inspect.add_argument("--max-height", type=int, default=20)

    return p


def main() -> None:
    args = build_arg_parser().parse_args()

    if args.cmd == "convert":
        art = convert_file(
            args.input,
            args.output,
            max_width=args.max_width,
            max_height=args.max_height,
            palette_name=args.palette,
            dither=args.dither,
            encoding=args.encoding,
            preview_path=args.preview,
        )
        print(
            f"Wrote {args.output}: {art.width} chars x {art.height} lines; "
            f"palette={art.palette}; dither={args.dither}; encoding={art.encoding}"
        )
    elif args.cmd == "render":
        art = render_file(
            args.input,
            args.output,
            max_width=args.max_width,
            max_height=args.max_height,
            reset=not args.no_reset,
            escape=args.escape,
        )
        print(
            f"Rendered {args.input} -> {args.output}: "
            f"{art.width} chars x {art.height} lines; palette={art.palette}"
        )
    elif args.cmd == "inspect":
        art = load_ircart(
            args.input,
            max_width=args.max_width,
            max_height=args.max_height,
        )
        print(
            f"Valid IRCART/1: palette={art.palette}, "
            f"{art.width} chars x {art.height} lines"
        )
    else:
        raise AssertionError(f"unhandled command: {args.cmd}")


if __name__ == "__main__":
    main()
