"""Image processing for a grayscale panel.

Pillow is imported lazily, inside the one function that needs it. A
text-only build -- which is the common case for Claude Code session output --
therefore never pays the ~15 MB of RSS that importing PIL costs, which
matters on a box with 1594 MB available.

Remote images are never fetched. A build-time HTTP fetch would be an SSRF
vector from a semi-production box, and a remote image is useless on a device
that reads offline anyway.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("kindle_hub.images")

SUPPORTED_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
GRAY_LEVELS = 16  # what the panel can actually display


@dataclass(frozen=True)
class ProcessedImage:
    name: str  # filename inside the EPUB / library media dir
    data: bytes
    width: int
    height: int


def is_remote(src: str) -> bool:
    return src.startswith(("http://", "https://", "//", "data:"))


def resolve_local(src: str, source_dir: Path, inbox_root: Path) -> Path | None:
    """Resolve an image reference relative to the markdown file, refusing to
    escape the inbox. `..` traversal out of the inbox is the only thing that
    would turn an image reference into an arbitrary file read."""
    if is_remote(src):
        return None
    candidate = (source_dir / src.split("#")[0].split("?")[0]).resolve()
    try:
        candidate.relative_to(inbox_root.resolve())
    except ValueError:
        log.warning("image reference escapes the inbox, skipping: %s", src)
        return None
    if not candidate.is_file():
        return None
    if candidate.suffix.lower() not in SUPPORTED_SUFFIXES:
        return None
    return candidate


def process(path: Path, max_width: int) -> ProcessedImage | None:
    """Grayscale, quantize to 16 levels, downscale to the panel width, emit
    PNG-8.

    PNG-8 rather than JPEG: for the screenshots and diagrams that dominate
    this content it compresses better at equal legibility, and it does not
    introduce ringing artefacts that dither badly on e-ink.
    """
    try:
        from PIL import Image  # lazy on purpose -- see module docstring
    except ImportError:  # pragma: no cover - Pillow is a hard dependency in prod
        log.warning("Pillow is not installed; image %s will be skipped", path.name)
        return None

    try:
        with Image.open(path) as im:
            im.load()
            gray = im.convert("L")
            if gray.width > max_width:
                ratio = max_width / gray.width
                gray = gray.resize(
                    (max_width, max(1, round(gray.height * ratio))), Image.LANCZOS
                )
            quantized = gray.quantize(colors=GRAY_LEVELS, method=Image.Quantize.MEDIANCUT)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
            name = f"{digest}.png"
            from io import BytesIO

            buf = BytesIO()
            # optimize=True keeps the file small; PIL writes no timestamp
            # chunk, so the output stays byte-deterministic for a given input.
            quantized.save(buf, format="PNG", optimize=True)
            return ProcessedImage(
                name=name, data=buf.getvalue(), width=quantized.width,
                height=quantized.height,
            )
    except Exception as exc:  # a corrupt image must not fail the whole build
        log.warning("could not process image %s: %s", path, exc)
        return None
