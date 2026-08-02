"""Diagrams and charts, rendered for a greyscale e-ink panel.

WHY THIS EXISTS
---------------
Structure written as prose is hard to hold in your head; the same structure
as a picture is not. The documents this hub carries are full of structure --
architectures, decision trees, debt schedules, weekly plans -- and on a
reading device a rendered diagram beats a wall of dashes.

HOW GREYSCALE CHANGES THE RULES
-------------------------------
Standard chart guidance assumes colour carries identity: assign a hue per
series, show a legend, label selectively. On a 16-level greyscale panel that
whole channel is gone, and two consequences follow that are not optional:

  1. Identity comes from POSITION and DIRECT LABELS, never from fill. Every
     bar is labelled on the bar. There is no legend to look up, because a
     legend keyed to shades of grey is a memory test.
  2. Magnitude may use lightness, because a light-to-dark ramp is exactly
     what greyscale is good at. This is the one encoding that gets *better*
     here rather than worse.

Everything else that survives the translation still applies: one scale per
axis, recessive gridlines, thin marks, no decoration that is not data.

Interaction guidance does not apply at all -- these are PNGs inside an EPUB.
Anything a tooltip would have said has to be on the page or it does not
exist.

WHY NOT MERMAID
---------------
It needs a headless browser. On a single-vCPU box already running nine other
vhosts, that is a large amount of machinery to draw a box and an arrow.
Graphviz does the same layout job in 1.3 MB.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import shutil
import subprocess

log = logging.getLogger("kindle_hub.diagram")

# Paperwhite 5 usable width in portrait, minus the reader's own margins.
# Rendering wider only to have KOReader downscale wastes bytes and softens
# the type; rendering narrower wastes the panel.
TARGET_W = 1050
MAX_H = 1400

# Flat tones. Values sit on 16-level quantisation boundaries so nothing
# dithers into crosshatch.
INK = "#111111"
GREY_DARK = "#555555"
GREY_MID = "#888888"
GREY_PALE = "#C8C8C8"
PAPER = "#F2F2F2"

_DOT_TIMEOUT = 15


def graphviz_available() -> bool:
    return shutil.which("dot") is not None


# --- structural diagrams (graphviz) --------------------------------------

_DOT_PREAMBLE = f"""
  bgcolor="{PAPER}";
  rankdir=TB;
  splines=ortho;
  nodesep=0.42;
  ranksep=0.52;
  fontname="DejaVu Sans";
  node [shape=box, style="filled,rounded", fillcolor="white",
        color="{INK}", penwidth=1.6, fontname="DejaVu Sans", fontsize=15,
        fontcolor="{INK}", margin="0.20,0.13", height=0.42];
  edge [color="{GREY_DARK}", penwidth=1.5, arrowsize=0.7,
        fontname="DejaVu Sans", fontsize=12, fontcolor="{GREY_DARK}"];
"""

# A diagram body may only contain these. Graphviz's DOT language has file
# and shell reach (`imagepath`, `shapefile`, and on some builds external
# layout plugins), and these documents are authored by an agent writing into
# an inbox -- so the input is filtered rather than trusted.
_DOT_FORBIDDEN = re.compile(
    r"\b(image|imagepath|shapefile|imagescale|epsf|fontpath)\s*=", re.IGNORECASE
)


def render_dot(body: str, *, rankdir: str = "TB") -> bytes:
    """Render a DOT graph body to a greyscale PNG.

    `body` is the inside of the graph -- nodes and edges only. The preamble
    is ours, so a document cannot restyle the whole diagram into something
    that reads badly on e-ink, and cannot reach the filesystem.
    """
    if not graphviz_available():
        raise RuntimeError("graphviz `dot` is not installed")
    if _DOT_FORBIDDEN.search(body):
        raise ValueError("diagram may not reference external files")

    rankdir = "LR" if rankdir.upper() == "LR" else "TB"
    src = (
        "digraph G {\n"
        + _DOT_PREAMBLE.replace("rankdir=TB;", f"rankdir={rankdir};")
        + body
        + "\n}\n"
    )

    proc = subprocess.run(
        ["dot", "-Tpng", "-Gdpi=150"],
        input=src.encode("utf-8"),
        capture_output=True,
        timeout=_DOT_TIMEOUT,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"dot failed: {proc.stderr.decode('utf-8', 'replace')[:300]}"
        )
    return _to_eink(proc.stdout)


# --- bar charts (Pillow) --------------------------------------------------


def render_bars(
    rows: list[tuple[str, float]],
    *,
    unit: str = "",
    title: str = "",
    note: str = "",
) -> bytes:
    """Horizontal bars, one row per item, every bar labelled on the bar.

    Horizontal rather than vertical on purpose: category names here are
    phrases ("kewajiban bulan ini"), and rotated axis labels are unreadable
    at e-ink contrast.

    Every value is printed. The usual advice is to label selectively and let
    the axis carry the rest, but that assumes a reader who can hover or
    squint at a gridline. Here the number is the point.
    """
    from PIL import Image, ImageDraw, ImageFont

    if not rows:
        raise ValueError("no rows")

    def font(sz: int, bold: bool = False):
        paths = (
            [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            ]
            if bold
            else [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/System/Library/Fonts/Supplemental/Arial.ttf",
            ]
        )
        for p in paths:
            try:
                return ImageFont.truetype(p, sz)
            except OSError:
                continue
        try:
            return ImageFont.load_default(sz)
        except TypeError:
            return ImageFont.load_default()

    f_label = font(26)
    f_value = font(26, bold=True)
    f_title = font(32, bold=True)
    f_note = font(22)

    pad = 28
    row_h = 76
    label_w = 340
    top = pad + (54 if title else 0)
    height = top + row_h * len(rows) + pad + (44 if note else 0)
    height = min(height, MAX_H)

    img = Image.new("L", (TARGET_W, height), 0xF2)
    d = ImageDraw.Draw(img)

    if title:
        d.text((pad, pad), title, font=f_title, fill=0x11)

    peak = max(abs(v) for _, v in rows) or 1.0
    track_x = pad + label_w
    track_w = TARGET_W - track_x - pad - 200

    for i, (label, value) in enumerate(rows):
        y = top + i * row_h + 14

        d.text((pad, y + 6), label[:34], font=f_label, fill=0x33)

        # Recessive track so short bars still read as "out of" something.
        d.rectangle([track_x, y, track_x + track_w, y + 34], fill=0xE0)

        w = int(track_w * (abs(value) / peak))
        if w > 0:
            # Lightness encodes magnitude: the ramp is the one thing
            # greyscale does better than colour.
            tone = 0x22 if value / peak > 0.66 else (0x55 if value / peak > 0.33 else 0x8A)
            d.rectangle([track_x, y, track_x + w, y + 34], fill=tone)

        txt = f"{value:,.0f}{(' ' + unit) if unit else ''}".replace(",", ".")
        d.text((track_x + track_w + 18, y + 4), txt, font=f_value, fill=0x11)

    if note:
        d.text((pad, height - pad - 26), note, font=f_note, fill=0x77)

    img = img.quantize(colors=16).convert("L")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# --- shared ---------------------------------------------------------------


def _to_eink(png_bytes: bytes) -> bytes:
    """Greyscale, fit to panel width, quantise to the panel's 16 levels.

    Quantising here rather than leaving it to the device means what we test
    is what the reader shows.
    """
    from PIL import Image

    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    if img.width > TARGET_W:
        h = int(img.height * TARGET_W / img.width)
        img = img.resize((TARGET_W, h), Image.LANCZOS)
    if img.height > MAX_H:
        w = int(img.width * MAX_H / img.height)
        img = img.resize((w, MAX_H), Image.LANCZOS)
    img = img.quantize(colors=16).convert("L")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def name_for(kind: str, source: str) -> str:
    """Content-addressed filename, so an unchanged diagram keeps its name and
    the EPUB stays byte-identical across rebuilds."""
    h = hashlib.sha256(f"{kind}\x00{source}".encode()).hexdigest()[:12]
    return f"fig-{kind}-{h}.png"
