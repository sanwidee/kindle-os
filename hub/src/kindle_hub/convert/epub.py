"""Deterministic EPUB 3.0 writer (with an NCX fallback).

Hand-written rather than EbookLib for three reasons, argued in full in the
architecture plan: EbookLib pulls lxml + six; almost every e-ink decision
lives in exactly the layer it abstracts (no embedded fonts, a specific
stylesheet, split-the-spine-on-H1, hybrid nav+NCX); and we need byte
determinism, because the build hash goes in the filename and that is how
KOReader tells a revision apart from the copy already on the device.

The escape hatch, if this file ever becomes a time sink: swap the
implementation behind the unchanged `write_epub(spec, path) -> int` signature
and accept the lxml dependency.

Two container details that are the classic ways to produce an EPUB that
readers reject, both handled below:
  * `mimetype` must be the FIRST entry and stored UNCOMPRESSED.
  * META-INF/container.xml must point at the OPF with a forward-slash path.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

log = logging.getLogger("kindle_hub.epub")

# Fixed timestamp for every entry. Determinism is the whole point: the same
# markdown must produce a byte-identical file, or the content hash in the
# filename means nothing.
FIXED_DATE = (1980, 1, 1, 0, 0, 0)

OPF_PATH = "EPUB/package.opf"
NAV_PATH = "EPUB/nav.xhtml"
NCX_PATH = "EPUB/toc.ncx"
CSS_PATH = "EPUB/style.css"


@dataclass
class EpubSection:
    title: str
    html: str
    anchor: str

    @property
    def filename(self) -> str:
        return f"text/{self.anchor}.xhtml"


@dataclass
class EpubImage:
    name: str
    data: bytes


@dataclass
class EpubSpec:
    identifier: str  # urn:uuid:...
    title: str
    language: str
    author: str
    modified: str  # ISO8601 Z -- dcterms:modified is required by EPUB 3
    css: str
    sections: list[EpubSection] = field(default_factory=list)
    images: list[EpubImage] = field(default_factory=list)
    description: str = ""
    # Optional generated cover. Kept separate from `images` because it needs
    # its own manifest properties, its own spine entry, and must NOT appear
    # in the reading order alongside inline figures.
    cover: EpubImage | None = None


_XHTML_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" \
xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{lang}">
<head>
<meta charset="utf-8" />
<title>{title}</title>
<link rel="stylesheet" type="text/css" href="../style.css" />
</head>
<body>
<section epub:type="chapter">
{body}
</section>
</body>
</html>
"""


def _wellformed(xml_text: str) -> bool:
    try:
        ET.fromstring(xml_text)
        return True
    except ET.ParseError as exc:
        log.warning("generated XHTML is not well formed: %s", exc)
        return False


def _render_section(section: EpubSection, language: str) -> str:
    doc = _XHTML_TEMPLATE.format(
        lang=escape(language, quote=True),
        title=escape(section.title),
        body=section.html,
    )
    if _wellformed(doc):
        return doc
    # Rather than shipping a file crengine will refuse to open, degrade to an
    # escaped plain-text rendering of the same content. Loud in the log,
    # silent for the reader, and never a broken book.
    fallback = "<pre>" + escape(section.html) + "</pre>"
    return _XHTML_TEMPLATE.format(
        lang=escape(language, quote=True),
        title=escape(section.title),
        body=fallback,
    )


COVER_XHTML = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" \
xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{lang}">
<head>
<meta charset="utf-8" />
<title>Cover</title>
<style type="text/css">
/* No stylesheet link and no margins: a cover page that inherits body padding
   renders with a white border the reader cannot remove. */
body {{ margin: 0; padding: 0; text-align: center; }}
img {{ max-width: 100%; max-height: 100%; }}
</style>
</head>
<body>
<section epub:type="cover">
<img src="images/{name}" alt="{alt}" />
</section>
</body>
</html>
"""


def _render_opf(spec: EpubSpec) -> str:
    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
        'properties="nav"/>',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="css" href="style.css" media-type="text/css"/>',
    ]
    spine = []

    # The cover needs THREE separate declarations to be recognised everywhere:
    #   1. properties="cover-image" on the image  -- EPUB 3 readers
    #   2. <meta name="cover" content="...">      -- EPUB 2 fallback, which is
    #      what several Kindle-side tools still look for
    #   3. a spine entry with linear="no"         -- so it shows as the cover
    #      rather than as the first page of the text
    # Declaring only the first produces a book whose thumbnail is blank in
    # exactly the place we wanted one: the file browser grid.
    if spec.cover is not None:
        manifest.append(
            f'<item id="cover-image" href="images/{spec.cover.name}" '
            f'media-type="image/png" properties="cover-image"/>'
        )
        manifest.append(
            '<item id="cover" href="cover.xhtml" '
            'media-type="application/xhtml+xml"/>'
        )
        spine.append('<itemref idref="cover" linear="no"/>')

    for i, section in enumerate(spec.sections):
        item_id = f"s{i:03d}"
        manifest.append(
            f'<item id="{item_id}" href="{section.filename}" '
            f'media-type="application/xhtml+xml"/>'
        )
        spine.append(f'<itemref idref="{item_id}"/>')
    for i, image in enumerate(spec.images):
        manifest.append(
            f'<item id="img{i:03d}" href="images/{image.name}" media-type="image/png"/>'
        )

    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="pub-id">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="pub-id">{escape(spec.identifier)}</dc:identifier>\n'
        f"    <dc:title>{escape(spec.title)}</dc:title>\n"
        f"    <dc:language>{escape(spec.language)}</dc:language>\n"
        f"    <dc:creator>{escape(spec.author)}</dc:creator>\n"
        + (f"    <dc:description>{escape(spec.description)}</dc:description>\n"
           if spec.description else "")
        + f'    <meta property="dcterms:modified">{escape(spec.modified)}</meta>\n'
        + ('    <meta name="cover" content="cover-image"/>\n'
           if spec.cover is not None else "")
        + "  </metadata>\n"
        "  <manifest>\n    " + "\n    ".join(manifest) + "\n  </manifest>\n"
        '  <spine toc="ncx">\n    ' + "\n    ".join(spine) + "\n  </spine>\n"
        "</package>\n"
    )


def _render_nav(spec: EpubSpec) -> str:
    items = "\n".join(
        f'      <li><a href="{s.filename}">{escape(s.title)}</a></li>'
        for s in spec.sections
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops" '
        f'xml:lang="{escape(spec.language, quote=True)}">\n'
        "<head><meta charset=\"utf-8\" /><title>Contents</title></head>\n"
        "<body>\n"
        '  <nav epub:type="toc" id="toc">\n'
        "    <h1>Contents</h1>\n"
        "    <ol>\n" + items + "\n    </ol>\n"
        "  </nav>\n"
        "</body>\n</html>\n"
    )


def _render_ncx(spec: EpubSpec) -> str:
    """EPUB 3 does not require an NCX, but shipping one costs a couple of KB
    and maximizes compatibility across crengine's code paths."""
    points = "\n".join(
        f'    <navPoint id="np{i:03d}" playOrder="{i + 1}">\n'
        f"      <navLabel><text>{escape(s.title)}</text></navLabel>\n"
        f'      <content src="{s.filename}"/>\n'
        f"    </navPoint>"
        for i, s in enumerate(spec.sections)
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        "  <head>\n"
        f'    <meta name="dtb:uid" content="{escape(spec.identifier)}"/>\n'
        '    <meta name="dtb:depth" content="1"/>\n'
        '    <meta name="dtb:totalPageCount" content="0"/>\n'
        '    <meta name="dtb:maxPageNumber" content="0"/>\n'
        "  </head>\n"
        f"  <docTitle><text>{escape(spec.title)}</text></docTitle>\n"
        "  <navMap>\n" + points + "\n  </navMap>\n"
        "</ncx>\n"
    )


CONTAINER_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
    "  <rootfiles>\n"
    f'    <rootfile full-path="{OPF_PATH}" '
    'media-type="application/oebps-package+xml"/>\n'
    "  </rootfiles>\n"
    "</container>\n"
)


def _write(zf: zipfile.ZipFile, name: str, data: bytes | str,
           compress: bool = True) -> None:
    info = zipfile.ZipInfo(name, date_time=FIXED_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    info.external_attr = 0o644 << 16
    info.create_system = 3  # unix, fixed so the archive does not vary by host
    if isinstance(data, str):
        data = data.encode("utf-8")
    zf.writestr(info, data)


def write_epub(spec: EpubSpec, path: Path) -> int:
    """Write the EPUB and return its size in bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")

    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 1. mimetype: first entry, stored, no compression. Non-negotiable.
        _write(zf, "mimetype", "application/epub+zip", compress=False)
        # 2. everything else, in a fixed order.
        _write(zf, "META-INF/container.xml", CONTAINER_XML)
        _write(zf, OPF_PATH, _render_opf(spec))
        _write(zf, NAV_PATH, _render_nav(spec))
        _write(zf, NCX_PATH, _render_ncx(spec))
        _write(zf, CSS_PATH, spec.css)
        if spec.cover is not None:
            _write(
                zf,
                "EPUB/cover.xhtml",
                COVER_XHTML.format(
                    lang=spec.language,
                    name=spec.cover.name,
                    alt=escape(spec.title),
                ),
            )
        for section in spec.sections:
            _write(zf, f"EPUB/{section.filename}", _render_section(section, spec.language))
        for image in sorted(spec.images, key=lambda i: i.name):
            _write(zf, f"EPUB/images/{image.name}", image.data)
        if spec.cover is not None:
            # PNG is already deflate-compressed internally; storing it avoids
            # a second pass that costs CPU and gains nothing.
            _write(zf, f"EPUB/images/{spec.cover.name}", spec.cover.data, compress=False)

    tmp.replace(path)
    return path.stat().st_size
