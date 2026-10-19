# Clean pixel-art preparation

Run `prepare.py` from the repository root with Python 3 and Pillow installed.
Use your own source image; the commands do not depend on project-specific
artwork. The tool writes files locally and never sends messages to IRC.

## Basic preparation

```bash
python3 prepare.py image.png image-small.png --ircart image.ircart
```

This preserves source colours, composites transparency onto black, trims empty
background margins, adds a one-output-pixel border, and chooses an integer
nearest-neighbour reduction. It creates:

- `image-small.png`: an editable prepared image, including any final padding row.
- `image-small-preview.png`: a 12× enlargement of that image.
- `image.ircart`: an optional IRCART/1 asset when `--ircart` is supplied.

The PNGs show prepared RGB colours. Exported IRCART uses the nearest available
IRC palette colours; rendering may therefore change shades. The current
upper-half-block renderer is unchanged. The tool reports actual rendered
UTF-8 body lengths even when no IRCART file is requested.

Source files are never overwritten. Existing output files may be replaced.
All output directories must already exist. Output paths must be distinct.

## Remove existing antialiasing

Nearest-neighbour sampling prevents new blended shades but does not remove
greys or other antialiased colours already in the source. Select the intended
solid colours explicitly using comma-separated six-digit RGB hex values:

```bash
python3 prepare.py image.png image-small.png \
  --colours 000000,ffffff,ff00ff --ircart image.ircart
```

This example uses black, white and magenta. Every source colour is assigned to
its closest chosen colour using the converter's weighted RGB distance. Choose
colours appropriate to your artwork; genuine grey details or ambiguous edges
can otherwise be reassigned unintentionally. If `--colours` is omitted, no
colour cleanup is performed.

`--background HEX` chooses the colour used for transparency, trimming and
padding. It defaults to `000000`. If a colour list is supplied, it must include
that background. Cleanup happens before trimming, so mapped background shades
can also be removed from empty margins.

## Choose the reduction

Automatic mode (`--factor auto`, the default) finds the largest exact square
pixel grid in the cleaned artwork and checks whether its reduction fits the
configured limits. If an exact result fits, the original cleaned blocks can
be reconstructed at that factor. Empty outer margins may have been trimmed.
The border defaults to one output pixel; change it with `--border N`.

If no exact reduction fits, automatic mode tries increasing integer factors,
starting with the smallest that could fit the dimensions, until the actual
rendered bytes also fit. It clearly reports when this is not an exact grid
match. Integer factors and nearest-neighbour sampling avoid blending, but
cannot guarantee that thin features survive an approximate reduction.

For exact results only:

```bash
python3 prepare.py image.png image-small.png --require-exact
```

To choose the reduction yourself:

```bash
python3 prepare.py image.png image-small.png --factor 4 --ircart image.ircart
```

An explicit factor is never silently changed. The tool pads the right/bottom
edges to whole multiples of the factor. If the resulting sampled height is
odd, it adds one final background pixel row for half-block rendering.
Inspect the preview for lost small details. A factor that erases the entire
artwork is rejected rather than producing a blank result.

## Regenerate an edited preview

Default previews enlarge every sampled pixel to a 12 × 12 square. Edit whole
squares, preserve the complete frame, then use:

```bash
python3 prepare.py edited-preview.png edited-small.png \
  --factor 12 --keep-frame --colours 000000,ffffff \
  --ircart edited.ircart
```

Change the colour list to suit the image, or omit it for already clean edits.
For example, a 384 × 384 preview returns to a 32 × 32 sampled grid, or
32 columns × 16 IRC lines. `--keep-frame` disables trimming and the added
border and requires an exact match after colour cleanup. Off-grid edits are
rejected, so isolated changes inside enlarged squares cannot disappear silently.
It can still add the final row needed by an odd sampled height.

Editing the saved small PNG directly is often easier. Use `--factor 1 --keep-frame` to retain its pixels and frame while performing optional cleanup.

## Match the bot's limits

Defaults are 36 columns, 18 IRC lines and 350 UTF-8 bytes per rendered body.
Height is measured in IRC lines, each representing two sampled pixel rows.
Set your bot's actual height and transport-aware body budget explicitly:

```bash
python3 prepare.py image.png image-small.png \
  --max-width 40 --max-height 30 --max-body-bytes 350 \
  --ircart image.ircart
```

These are example limits, not universal IRC quotas. Byte counts include colour
controls and the renderer's reset; your bot must reserve protocol overhead and
any formatting it adds. RLE reduces asset storage size, not message-body size.
A failed limit or grid check stops preparation before output files are written.
The tool does not truncate lines or partly prepare an oversized asset.

The palette used for byte checks and export defaults to `mirc99`; select
`--palette mirc16` when needed. The small PNG and its preview remain in prepared
RGB colours, independently of that IRC palette choice.

The standalone parser/helper defaults to 20 lines, so inspecting or loading a
taller asset needs an explicit height allowance, for example:

```bash
python3 ircart.py inspect image.ircart --max-height 30
```

Check the preview's outlines, separated dots and thin features, and test a
small asset in your IRC client. PNG appearance does not prove terminal glyph
rendering or network flood acceptance. Keep the bot's pacing and byte guards.
