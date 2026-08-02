"""Generated EPUB covers, drawn for e-ink.

WHY GENERATE RATHER THAN SHIP AN IMAGE
--------------------------------------
Every document here is produced from markdown, so there is no artwork to
attach. A cover still earns its place: KOReader's file browser and the OPDS
list are grids of thumbnails, and a shelf of identical grey placeholders is
genuinely hard to navigate once there are thirty of them. The cover's job is
recognition at thumbnail size, not decoration.

DRAWN FOR THE PANEL, NOT FOR A SCREENSHOT
-----------------------------------------
Kindle Paperwhite 5, confirmed from the device's own User-Agent: 1236x1648,
16-level greyscale, no colour.

Consequences that shape every choice below:

  * No gradients, no soft shadows, no anti-aliased large fills. E-ink
    dithers them into visible crosshatch that looks like a printing fault.
    Everything here is flat black, flat white, or one of a few fixed greys.
  * Contrast does the work colour would. The palette is literally two tones
    plus a couple of greys for hierarchy.
  * A thumbnail is ~120px wide. Anything below roughly 40px of source type
    turns to mush at that size, so the title is set large and the metadata
    is allowed to disappear -- it is there for the full-size view only.
  * Pure black over large areas is fine on e-ink (it costs nothing to hold)
    but a full-bleed black cover makes the page-turn flash worse, so the
    ground stays light and black is used as ink.

The mark is a simple original glyph -- a bracketed caret, the shape a
terminal prompt makes. It is drawn from primitives, not traced from anyone's
logo, and it exists to make the shelf scannable rather than to brand it.
"""

from __future__ import annotations

import hashlib
import io
import textwrap

# Paperwhite 5 panel. Covers render at panel size; KOReader downsamples for
# thumbnails far better than it upsamples, so err large.
COVER_W = 1236
COVER_H = 1648

# Flat tones only. Values chosen to survive 16-level quantisation without
# landing between levels, where dithering starts.
INK = 0x11
GREY_DARK = 0x55
GREY_MID = 0x88
GREY_PALE = 0xC8
PAPER = 0xF2


def _font(size: int, bold: bool = False):
    """Best available system font at `size`, falling back to Pillow's builtin.

    The builtin bitmap font cannot scale, so on a host with no TrueType fonts
    the cover still renders -- just plainer. That is a deliberate soft
    failure: a dull cover is much better than a build that dies because a
    container image happens to ship without fonts.
    """
    from PIL import ImageFont

    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow < 10.1 has no size argument
        return ImageFont.load_default()


def _mark(draw, x: int, y: int, size: int, weight: int) -> None:
    """A bracketed caret: [ ^ ] -- the shape of a prompt.

    Drawn from lines so it stays crisp at any size and never depends on a
    font being present.
    """
    half = size // 2
    arm = size // 3

    # Left bracket
    draw.line([(x, y - half), (x - arm, y - half)], fill=INK, width=weight)
    draw.line([(x - arm, y - half), (x - arm, y + half)], fill=INK, width=weight)
    draw.line([(x - arm, y + half), (x, y + half)], fill=INK, width=weight)

    # Right bracket
    rx = x + size + arm
    draw.line([(rx, y - half), (rx + arm, y - half)], fill=INK, width=weight)
    draw.line([(rx + arm, y - half), (rx + arm, y + half)], fill=INK, width=weight)
    draw.line([(rx + arm, y + half), (rx, y + half)], fill=INK, width=weight)

    # Caret between them
    cx = x + (size + arm) // 2
    draw.line([(cx - arm, y + arm // 2), (cx, y - arm // 2)], fill=INK, width=weight)
    draw.line([(cx, y - arm // 2), (cx + arm, y + arm // 2)], fill=INK, width=weight)


def _accent_bars(draw, x: int, y: int, width: int, doc_id: str) -> None:
    """A short row of bars whose pattern is derived from the document id.

    Purely for recognition: two documents never get the same pattern, so at
    thumbnail size the shelf is distinguishable even when the titles are too
    small to read. Deterministic, so a rebuild produces an identical cover
    and the EPUB stays byte-stable.
    """
    digest = hashlib.sha256(doc_id.encode("utf-8")).digest()
    tones = (INK, GREY_DARK, GREY_MID, GREY_PALE)
    n = 8
    gap = 6
    bar_w = (width - gap * (n - 1)) // n
    for i in range(n):
        tone = tones[digest[i] % len(tones)]
        height = 14 + (digest[i + 8] % 3) * 10
        bx = x + i * (bar_w + gap)
        draw.rectangle([bx, y, bx + bar_w, y + height], fill=tone)


def render_cover(
    title: str,
    *,
    doc_id: str,
    collection: str = "",
    date: str = "",
    source: str = "Claude Code",
    site: str = "kindle-os",
) -> bytes:
    """Render a cover PNG. Returns encoded bytes, greyscale, no alpha.

    Deterministic for a given set of arguments -- required, because the build
    asserts EPUBs are byte-identical across rebuilds.
    """
    from PIL import Image, ImageDraw

    img = Image.new("L", (COVER_W, COVER_H), PAPER)
    d = ImageDraw.Draw(img)

    margin = 110
    inner = COVER_W - margin * 2

    # Hairline frame. One pixel would vanish on a 300ppi panel; 3 reads as a
    # deliberate rule.
    d.rectangle([margin - 34, margin - 34, COVER_W - margin + 34, COVER_H - margin + 34],
                outline=GREY_PALE, width=3)

    # --- mark + wordmark -------------------------------------------------
    _mark(d, margin + 26, margin + 60, 66, 7)
    d.text((margin + 190, margin + 26), site.upper(), font=_font(46, bold=True), fill=INK)

    d.line([(margin, margin + 150), (COVER_W - margin, margin + 150)], fill=INK, width=4)

    # --- title -----------------------------------------------------------
    # Wrap by characters rather than measuring: predictable, and the box is
    # generous enough that the approximation never overflows.
    size = 96 if len(title) <= 40 else (78 if len(title) <= 70 else 64)
    wrap_at = max(14, int(inner / (size * 0.52)))
    lines = textwrap.wrap(title, wrap_at)[:6]

    y = margin + 290
    tf = _font(size, bold=True)
    for line in lines:
        d.text((margin, y), line, font=tf, fill=INK)
        y += int(size * 1.22)

    # --- metadata, bottom ------------------------------------------------
    by = COVER_H - margin - 250
    _accent_bars(d, margin, by, inner, doc_id)

    by += 90
    d.line([(margin, by), (COVER_W - margin, by)], fill=GREY_PALE, width=3)

    by += 34
    mf = _font(40)
    if collection:
        d.text((margin, by), collection.upper(), font=mf, fill=GREY_DARK)
        by += 58
    if date:
        d.text((margin, by), date, font=mf, fill=GREY_MID)

    sf = _font(36)
    label = source.upper()
    try:
        w = int(d.textlength(label, font=sf))
    except AttributeError:
        w = len(label) * 18
    d.text((COVER_W - margin - w, COVER_H - margin - 44), label, font=sf, fill=GREY_MID)

    # Quantise to the panel's own 16 levels. Doing it here means what we see
    # is what the device shows, rather than letting the reader dither for us.
    img = img.quantize(colors=16).convert("L")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
