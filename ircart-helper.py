"""
ircart_helper.py

Small bot-side helper for IRCART/1 files.

- No Pillow dependency.
- Accepts plain or RLE IRCART/1.
- Strictly validates dimensions and palette indexes.
- Returns rendered IRC message bodies as a list[str].
- Reports UTF-8 byte lengths so the bot can apply its own flood/queue policy.
- Does not send anything to IRC.

Typical use:

    from ircart_helper import load_for_irc

    rendered = load_for_irc("art/facepalm.ircart")

    for line in rendered.lines:
        queue_for_later(target, line)   # your existing flood-control path
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


IRC_COLOR = "\x03"
IRC_RESET = "\x0f"
HALF_BLOCK = "▀"

# Resource guards are separate from the actual rendered message-body budget.
DEFAULT_MAX_WIDTH = 512
DEFAULT_MAX_BODY_BYTES = 400

# IRC colour indexes available to the file format.
# For validation the bot only needs their counts, not RGB values.
PALETTE_SIZES = {
    "mirc16": 16,  # 0..15
    "mirc99": 99,  # 0..98; 99 itself means "default" in IRC formatting
}


class IRCArtError(ValueError):
    """Raised when an IRCART file is invalid or unsafe."""


@dataclass(frozen=True)
class Cell:
    fg: int
    bg: int


@dataclass(frozen=True)
class IRCArt:
    width: int
    height: int
    palette: str
    rows: tuple[tuple[Cell, ...], ...]
    glyph: str = "upper-half"
    encoding: str = "plain"


@dataclass(frozen=True)
class RenderedIRCArt:
    """
    Bot-facing result.

    `lines` are ready-to-send IRC message *bodies*. They do not include
    PRIVMSG, target, CRLF, rate limiting, or queueing.
    """
    width: int
    height: int
    palette: str
    lines: tuple[str, ...]
    utf8_bytes: tuple[int, ...]

    @property
    def max_utf8_bytes(self) -> int:
        return max(self.utf8_bytes, default=0)

    @property
    def total_utf8_bytes(self) -> int:
        return sum(self.utf8_bytes)


def _parse_header(lines: list[str]) -> tuple[dict[str, str], int]:
    if not lines or lines[0].strip() != "IRCART/1":
        raise IRCArtError("missing or invalid IRCART/1 header")

    header: dict[str, str] = {}
    i = 1

    while i < len(lines):
        raw = lines[i]
        line = raw.strip()

        if line == "":
            return header, i + 1

        if "=" not in line:
            raise IRCArtError(f"invalid header line: {raw!r}")

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if not key:
            raise IRCArtError("empty header key")
        if key in header:
            raise IRCArtError(f"duplicate header key: {key}")

        header[key] = value
        i += 1

    raise IRCArtError("missing blank line after header")


def _parse_cell(cell_text: str, palette_size: int) -> Cell:
    if cell_text.count(",") != 1:
        raise IRCArtError(f"invalid cell token: {cell_text!r}")

    fg_s, bg_s = cell_text.split(",", 1)

    if not fg_s.isdigit() or not bg_s.isdigit():
        raise IRCArtError(f"non-numeric cell token: {cell_text!r}")

    fg = int(fg_s, 10)
    bg = int(bg_s, 10)

    if not (0 <= fg < palette_size and 0 <= bg < palette_size):
        raise IRCArtError(f"colour index out of range: {cell_text!r}")

    return Cell(fg=fg, bg=bg)


def _parse_rle_token(
    token: str,
    palette_size: int,
    remaining_cells: int,
) -> tuple[int, Cell]:
    if "*" in token:
        if token.count("*") != 1:
            raise IRCArtError(f"invalid RLE token: {token!r}")

        count_s, cell_text = token.split("*", 1)

        if not count_s.isdigit():
            raise IRCArtError(f"invalid RLE count: {token!r}")

        count = int(count_s, 10)

        # "1*x" is forbidden so there is only one canonical representation.
        if count < 2:
            raise IRCArtError(f"RLE count must be >= 2: {token!r}")
    else:
        count = 1
        cell_text = token

    if count > remaining_cells:
        raise IRCArtError(f"RLE token expands past row width: {token!r}")

    return count, _parse_cell(cell_text, palette_size)


def parse_ircart(
    text: str,
    *,
    max_width: int = DEFAULT_MAX_WIDTH,
    max_height: int = 20,
    max_text_bytes: int = 64 * 1024,
) -> IRCArt:
    """
    Parse and validate IRCART/1 text.

    Dimension and text-size limits bound parsing work. They do not establish
    whether rendered lines fit IRC; load_for_irc checks that separately.
    """
    if not isinstance(text, str):
        raise TypeError("text must be str")

    encoded_size = len(text.encode("utf-8"))
    if encoded_size > max_text_bytes:
        raise IRCArtError(
            f"IRCART file is {encoded_size} bytes; limit is {max_text_bytes}"
        )

    lines = text.splitlines()
    header, body_start = _parse_header(lines)

    required = {"palette", "width", "height", "glyph", "encoding"}
    missing = required - set(header)
    if missing:
        raise IRCArtError(
            "missing header key(s): " + ", ".join(sorted(missing))
        )

    palette = header["palette"]
    glyph = header["glyph"]
    encoding = header["encoding"]

    if palette not in PALETTE_SIZES:
        raise IRCArtError(f"unsupported palette: {palette}")
    if glyph != "upper-half":
        raise IRCArtError(f"unsupported glyph: {glyph}")
    if encoding not in {"plain", "rle"}:
        raise IRCArtError(f"unsupported encoding: {encoding}")

    try:
        width = int(header["width"], 10)
        height = int(header["height"], 10)
    except ValueError as exc:
        raise IRCArtError("width and height must be integers") from exc

    if width <= 0 or height <= 0:
        raise IRCArtError("width and height must be positive")
    if width > max_width:
        raise IRCArtError(f"width {width} exceeds limit {max_width}")
    if height > max_height:
        raise IRCArtError(f"height {height} exceeds limit {max_height}")

    body = lines[body_start:]
    if len(body) != height:
        raise IRCArtError(
            f"body contains {len(body)} row(s), expected {height}"
        )

    palette_size = PALETTE_SIZES[palette]
    rows: list[tuple[Cell, ...]] = []

    for row_no, raw_line in enumerate(body, start=1):
        tokens = raw_line.split()

        if encoding == "plain":
            if len(tokens) != width:
                raise IRCArtError(
                    f"row {row_no} contains {len(tokens)} cell(s), "
                    f"expected {width}"
                )
            row = tuple(_parse_cell(t, palette_size) for t in tokens)

        else:  # rle
            expanded: list[Cell] = []

            for token in tokens:
                count, cell = _parse_rle_token(
                    token,
                    palette_size,
                    width - len(expanded),
                )
                expanded.extend([cell] * count)

            if len(expanded) != width:
                raise IRCArtError(
                    f"row {row_no} expands to {len(expanded)} cell(s), "
                    f"expected {width}"
                )

            row = tuple(expanded)

        rows.append(row)

    return IRCArt(
        width=width,
        height=height,
        palette=palette,
        rows=tuple(rows),
        glyph=glyph,
        encoding=encoding,
    )


def load_ircart(
    path: str | Path,
    *,
    max_width: int = DEFAULT_MAX_WIDTH,
    max_height: int = 20,
    max_text_bytes: int = 64 * 1024,
) -> IRCArt:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise IRCArtError(f"cannot read IRCART file: {path}") from exc

    return parse_ircart(
        text,
        max_width=max_width,
        max_height=max_height,
        max_text_bytes=max_text_bytes,
    )


def render_row(row: tuple[Cell, ...], *, reset: bool = True) -> str:
    """
    Convert one validated row to an IRC message body.

    Repeated adjacent cells with the same fg/bg pair share one IRC colour
    control code, minimizing wire length.
    """
    if not row:
        return IRC_RESET if reset else ""

    parts: list[str] = []
    current_pair: tuple[int, int] | None = None
    run = 0

    def flush(pair: tuple[int, int] | None, count: int) -> None:
        if pair is None or count == 0:
            return
        fg, bg = pair
        parts.append(
            f"{IRC_COLOR}{fg:02d},{bg:02d}"
            + HALF_BLOCK * count
        )

    for cell in row:
        pair = (cell.fg, cell.bg)

        if current_pair is None:
            current_pair = pair
            run = 1
        elif pair == current_pair:
            run += 1
        else:
            flush(current_pair, run)
            current_pair = pair
            run = 1

    flush(current_pair, run)

    if reset:
        parts.append(IRC_RESET)

    return "".join(parts)


def render_ircart(art: IRCArt, *, reset_each_line: bool = True) -> RenderedIRCArt:
    lines = tuple(
        render_row(row, reset=reset_each_line)
        for row in art.rows
    )
    byte_lengths = tuple(len(line.encode("utf-8")) for line in lines)

    return RenderedIRCArt(
        width=art.width,
        height=art.height,
        palette=art.palette,
        lines=lines,
        utf8_bytes=byte_lengths,
    )


def load_for_irc(
    path: str | Path,
    *,
    max_width: int = DEFAULT_MAX_WIDTH,
    max_height: int = 20,
    max_text_bytes: int = 64 * 1024,
    max_body_bytes: int | None = DEFAULT_MAX_BODY_BYTES,
    reset_each_line: bool = True,
) -> RenderedIRCArt:
    """
    Convenience entry point for bot.

    Returns rendered message bodies only. It deliberately does NOT send,
    sleep, queue, throttle, or split them.

    Each rendered line, including colour controls and any trailing reset, must
    fit max_body_bytes (default 400 UTF-8 bytes). Callers should supply their
    own transport-aware body budget, accounting for command, target, CRLF,
    any additional formatting, and server-prefix allowance where needed.
    Explicit None disables this check for callers enforcing it elsewhere.
    Width is only a parsing resource guard (default 512 columns).
    """
    if max_body_bytes is not None and max_body_bytes <= 0:
        raise ValueError("max_body_bytes must be positive")

    art = load_ircart(
        path,
        max_width=max_width,
        max_height=max_height,
        max_text_bytes=max_text_bytes,
    )
    rendered = render_ircart(art, reset_each_line=reset_each_line)

    if max_body_bytes is not None:
        too_long = [
            (i + 1, size)
            for i, size in enumerate(rendered.utf8_bytes)
            if size > max_body_bytes
        ]

        if too_long:
            details = ", ".join(
                f"line {line_no}: {size} bytes"
                for line_no, size in too_long
            )
            raise IRCArtError(
                f"rendered IRC body exceeds {max_body_bytes}-byte limit "
                f"({details})"
            )

    return rendered


def visible_debug_line(line: str) -> str:
    """Make IRC controls visible in logs/tests without changing block glyphs."""
    return (
        line
        .replace(IRC_COLOR, r"\x03")
        .replace(IRC_RESET, r"\x0f")
    )
