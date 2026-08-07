"""The `ebooks` shelf: a directory of files, served over the same OPDS feed.

WHY THIS IS A SEPARATE CONCEPT
------------------------------
Everything else in this hub follows one path: markdown lands in the inbox, the
builder converts it, and the catalog is a row in SQLite. That model does not
fit a personal library. These files are not authored here, have no front
matter, and must not be rewritten -- the job is to list what is on disk and
hand the bytes over unchanged.

So this shelf reads a directory instead of the documents table. It is
deliberately the only part of the system that does.

WHAT IT DOES NOT DO
-------------------
No conversion. A PDF is served as a PDF.

That is not a limitation being apologised for -- it is the finding that made
this shelf worth building. KOReader has a reflow mode (k2pdfopt) that crops
margins, splits columns and reflows text-layer PDFs to a 6-inch panel. For a
library that is already 100% text-layer PDF, reflow on the device may well
beat a lossy server-side conversion, and it costs nothing to try first.

Converting 433 books to EPUB before testing that would have been days of CPU
spent to maybe make things worse.

CACHING
-------
The directory is scanned on demand and the result cached for a short interval.
A personal shelf changes when the owner rsyncs something new, which is rare,
and a 6-inch reader re-requests the feed constantly while browsing.
"""

from __future__ import annotations

import hashlib
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, send_file

from ..auth.middleware import require_any, require_opds

bp = Blueprint("ebooks", __name__)

# Extensions the Kindle side can actually open. Anything else on disk is
# ignored rather than listed and then failing at download time.
READABLE = {".pdf": "application/pdf",
            ".epub": "application/epub+zip",
            ".cbz": "application/vnd.comicbook+zip",
            ".txt": "text/plain"}

_CACHE_SECONDS = 120
_cache: dict[str, tuple[float, list[Book]]] = {}


@dataclass(frozen=True)
class Book:
    bid: str          # stable id derived from the relative path
    title: str
    rel: str
    size: int
    mtime: float
    mime: str

    @property
    def size_h(self) -> str:
        mb = self.size / 1048576
        return f"{mb:.1f} MB" if mb >= 1 else f"{self.size // 1024} KB"


def _root() -> Path:
    cfg = current_app.config["HUB_CONFIG"]
    return Path(getattr(cfg, "ebooks_dir", "/data/ebooks"))


def _title_from(name: str) -> str:
    """Best-effort human title from a filename.

    Filenames in a real library are a mess: leading catalogue numbers, scraper
    suffixes, underscores for spaces. This tidies the obvious cases and leaves
    the rest alone rather than guessing.
    """
    import re
    t = Path(name).stem
    t = re.sub(r"^\s*\d{1,5}[\.\-_ ]+", "", t)
    t = re.sub(r"\(\s*(PDFDrive|z-lib|Z-Library|libgen)[^)]*\)", "", t, flags=re.I)
    t = t.replace("_", " ")
    t = re.sub(r"\s{2,}", " ", t).strip(" -_")
    return t or Path(name).stem


def scan() -> list[Book]:
    root = _root()
    key = str(root)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_SECONDS:
        return hit[1]

    books: list[Book] = []
    if root.is_dir():
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.name.startswith("."):
                continue
            mime = READABLE.get(p.suffix.lower())
            if not mime:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            rel = str(p.relative_to(root))
            books.append(Book(
                bid=hashlib.sha256(rel.encode("utf-8")).hexdigest()[:12],
                title=_title_from(p.name),
                rel=rel,
                size=st.st_size,
                mtime=st.st_mtime,
                mime=mime,
            ))
    # Sort by title, accent-insensitively, so an Indonesian and an English
    # shelf interleave predictably rather than by byte value.
    books.sort(key=lambda b: unicodedata.normalize("NFKD", b.title.lower()))
    _cache[key] = (now, books)
    return books


def by_id(bid: str) -> Book | None:
    return next((b for b in scan() if b.bid == bid), None)


def initials() -> list[tuple[str, int]]:
    """(letter, count) for an A-Z index.

    433 entries in one flat feed is unusable on a device that repaints in a
    second, so the shelf is split by first letter."""
    buckets: dict[str, int] = {}
    for b in scan():
        c = b.title[:1].upper()
        key = c if c.isalpha() else "#"
        buckets[key] = buckets.get(key, 0) + 1
    return sorted(buckets.items())


@bp.route("/ebooks/<bid>/<path:filename>")
@require_any
def download(bid: str, filename: str):
    """Serve the file bytes.

    require_any, matching document downloads: the Kindle arrives with Basic
    auth, a browser with a session, and neither may be redirected.
    """
    book = by_id(bid)
    if book is None:
        abort(404)
    path = (_root() / book.rel).resolve()
    # Defence in depth: bid comes from our own scan, but resolve() and this
    # check mean a symlink inside the tree still cannot escape it.
    if not str(path).startswith(str(_root().resolve())):
        abort(404)
    if not path.is_file():
        abort(404)

    cfg = current_app.config["HUB_CONFIG"]
    if getattr(cfg, "use_xaccel", False):
        resp = Response(status=200)
        resp.headers["X-Accel-Redirect"] = f"/_ebooks/{book.rel}"
        resp.headers["Content-Type"] = book.mime
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="{Path(book.rel).name}"'
        )
        return resp
    return send_file(path, mimetype=book.mime, as_attachment=True,
                     download_name=Path(book.rel).name)


# --- curated shelves ------------------------------------------------------
#
# The A-Z index answers "where is this book". It cannot answer "what should I
# read next", which is the question that actually decides whether a shelf of
# 433 files gets used or ignored.
#
# Shelves live in a plain text manifest next to the books rather than in the
# database, for two reasons: the owner can edit it over ssh without a deploy,
# and a shelf is a reading decision, not application state.
#
# Format -- blank lines and # comments ignored:
#
#     [This week]
#     One line of description.
#     = exact-filename.pdf
#     = another.pdf
#
# A `=` line names a file. Anything else after the heading is description.
# A named file that is missing is skipped silently: the manifest is edited by
# hand and a typo should cost one entry, not the whole shelf.

MANIFEST = ".shelves"


@dataclass(frozen=True)
class Shelf:
    name: str
    note: str
    books: list


def shelves() -> list[Shelf]:
    path = _root() / MANIFEST
    if not path.is_file():
        return []
    by_name = {b.rel.rsplit("/", 1)[-1]: b for b in scan()}
    out: list[Shelf] = []
    name = note = ""
    picks: list = []

    def flush():
        if name:
            out.append(Shelf(name=name, note=note.strip(), books=list(picks)))

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            flush()
            name, note, picks = line[1:-1].strip(), "", []
        elif line.startswith("="):
            hit = by_name.get(line[1:].strip())
            if hit:
                picks.append(hit)
        elif name:
            note = (note + " " + line).strip()
    flush()
    return [s for s in out if s.books]


__all__ = ["bp", "scan", "by_id", "initials", "shelves", "Shelf",
           "Book", "require_opds"]
