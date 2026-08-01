"""OPDS 1.2 Atom feeds -- the Kindle door.

Everything in this module is shaped by KOReader v2025.10's OPDS client, which
speaks exactly one dialect and fails silently when it does not get it. The
constraints, and what breaks if you relax them:

  * Atom XML only. No OPDS 2.0 / JSON -- zero support in the client.
  * No XML comments, no CDATA, no <?xml-stylesheet?>. The client regex-mangles
    all three before handing the result to a fragile lexer. `_guard` below
    asserts their absence rather than trusting future editors.
  * <summary type="text">, never XHTML inside <content>.
  * Acquisition href ends in .epub, carries no query string, and is same
    origin as the feed. getFiletype checks the filename suffix before the MIME
    type, and a link matching neither is silently dropped from the download
    dialog.
  * No 3xx, ever. luasocket follows redirects but drops user/password on the
    hop, so a redirected acquisition URL arrives unauthenticated.
  * Every route answers HEAD with an accurate Last-Modified. KOReader keys its
    in-memory feed cache on "opds|catalog|<url>|<last-modified>", so this is
    the single highest-leverage server-side value in the design.
  * Modest page sizes. The client eagerly pre-fetches rel="next" until it has
    filled a screen, and each fetch is a blocking round trip on a device with
    no progress indicator.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from flask import Blueprint, Response, current_app, render_template, request

from ..auth.middleware import require_opds
from ..catalog.store import Document, parse_ts

bp = Blueprint("opds", __name__)

FEED_NAMESPACE = uuid.UUID("2f4d6b90-1c37-5a88-9c21-7e0b5d3f6a44")

NAV_TYPE = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ_TYPE = "application/atom+xml;profile=opds-catalog;kind=acquisition"
OPENSEARCH_TYPE = "application/opensearchdescription+xml"
EPUB_TYPE = "application/epub+zip"  # registered in KOReader's credocument.lua

_FORBIDDEN = ("<!--", "<![CDATA[", "<?xml-stylesheet")


@dataclass
class Link:
    rel: str
    href: str
    type: str
    title: str = ""
    # Populated on acquisition links so KOReader (and San) know what a tap
    # will cost on a blocking download with no progress bar.
    length: int | None = None


@dataclass
class Entry:
    id: str
    title: str
    updated: str
    links: list[Link] = field(default_factory=list)
    summary: str = ""
    author: str = ""
    issued: str | None = None
    language: str = ""
    extent: str = ""
    categories: list[str] = field(default_factory=list)


def _cfg():
    return current_app.config["HUB_CONFIG"]


def _store():
    return current_app.config["HUB_STORE"]


def url(path: str) -> str:
    """Feed-internal URL.

    Absolute by default. UNVERIFIED: whether KOReader resolves root-relative
    hrefs (`/opds/new`) against the feed URL correctly in every code path. The
    OPDS spec allows it and the client appears to, but that was never tested
    on device, so the default emits absolute URLs built from
    HUB_PUBLIC_ORIGIN, which cannot be resolved wrongly. Set
    HUB_OPDS_ABSOLUTE_URLS=0 to switch, e.g. if you ever front the hub with a
    second hostname.

    Either way the URL is same-origin with the feed, which is the constraint
    that actually matters (luasocket drops credentials across a redirect or
    origin change).
    """
    cfg = _cfg()
    return f"{cfg.public_origin}{path}" if cfg.opds_absolute_urls else path


def feed_id(path: str) -> str:
    return f"urn:uuid:{uuid.uuid5(FEED_NAMESPACE, path)}"


def human_size(nbytes: int) -> str:
    if nbytes < 1024:
        return f"{nbytes} B"
    if nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.0f} KB"
    return f"{nbytes / (1024 * 1024):.1f} MB"


def _guard(body: str) -> str:
    """Fail loudly in the one place a future edit could silently break the
    Kindle. A feed containing any of these parses as an empty catalog with no
    error message on the device."""
    for token in _FORBIDDEN:
        if token in body:
            raise RuntimeError(
                f"OPDS feed contains {token!r}, which KOReader's parser mangles. "
                "See the module docstring."
            )
    return body


def _respond(body: str, content_type: str, last_modified: str) -> Response:
    resp = Response(_guard(body), mimetype=content_type.split(";")[0])
    resp.headers["Content-Type"] = content_type + "; charset=utf-8"
    resp.last_modified = parse_ts(last_modified)
    # Deliberately NOT calling make_conditional(): a 304 on a feed GET is
    # untested against this client, and the cache it actually uses is keyed on
    # the Last-Modified value from a preflight HEAD, which it already has.
    resp.headers["Cache-Control"] = "private, no-cache"
    return resp


def _document_entry(doc: Document) -> Entry:
    return Entry(
        id=f"urn:uuid:{doc.uuid}",
        title=doc.title,
        updated=doc.updated,
        summary=doc.summary,
        author=doc.author,
        issued=doc.issued,
        language=doc.language,
        extent=human_size(doc.epub_bytes),
        categories=doc.tags,
        links=[
            Link(
                rel="http://opds-spec.org/acquisition",
                href=url(doc.epub_href),
                type=EPUB_TYPE,
                length=doc.epub_bytes,
            ),
            Link(rel="alternate", href=url(doc.reader_href), type="text/html"),
        ],
    )


def _acquisition_feed(
    path: str, title: str, kind: str, key: str | None = None
) -> Response:
    cfg = _cfg()
    store = _store()
    page = max(1, request.args.get("page", type=int, default=1))
    size = cfg.opds_page_size
    docs, total = store.shelf(kind, key, limit=size, offset=(page - 1) * size)
    last_modified = store.shelf_last_modified(kind, key)
    pages = max(1, -(-total // size))

    query = ""
    if kind == "search" and key:
        # Query strings are fine on FEED urls -- the extension rule only binds
        # acquisition links.
        query = f"&q={request.args.get('q', '')}"

    links = [
        Link("self", url(_page_url(path, page, query)), ACQ_TYPE),
        Link("start", url("/opds"), NAV_TYPE, "Home"),
        Link("up", url("/opds"), NAV_TYPE, "Home"),
        Link("search", url("/opds/opensearch.xml"), OPENSEARCH_TYPE, "Search"),
    ]
    if page > 1:
        links.append(Link("first", url(_page_url(path, 1, query)), ACQ_TYPE))
        links.append(Link("previous", url(_page_url(path, page - 1, query)), ACQ_TYPE))
    if page < pages:
        links.append(Link("next", url(_page_url(path, page + 1, query)), ACQ_TYPE))
        links.append(Link("last", url(_page_url(path, pages, query)), ACQ_TYPE))

    body = render_template(
        "opds/acquisition.xml",
        feed_id=feed_id(path),
        feed_title=f"{cfg.site_title} — {title}",
        updated=last_modified,
        links=links,
        entries=[_document_entry(d) for d in docs],
        total=total,
        page=page,
        page_size=size,
    )
    return _respond(body, ACQ_TYPE, last_modified)


def _page_url(path: str, page: int, query: str = "") -> str:
    if page <= 1 and not query:
        return path
    if page <= 1:
        return f"{path}?{query.lstrip('&')}"
    return f"{path}?page={page}{query}"


def _navigation_feed(path: str, title: str, entries: list[Entry],
                     last_modified: str, up: str | None = None) -> Response:
    cfg = _cfg()
    links = [
        Link("self", url(path), NAV_TYPE),
        Link("start", url("/opds"), NAV_TYPE, "Home"),
        Link("search", url("/opds/opensearch.xml"), OPENSEARCH_TYPE, "Search"),
    ]
    if up:
        links.append(Link("up", url(up), NAV_TYPE, "Up"))
    body = render_template(
        "opds/navigation.xml",
        feed_id=feed_id(path),
        feed_title=f"{cfg.site_title} — {title}" if path != "/opds" else cfg.site_title,
        updated=last_modified,
        links=links,
        entries=entries,
    )
    return _respond(body, NAV_TYPE, last_modified)


# --- routes ---------------------------------------------------------------


@bp.route("/opds")
@require_opds
def root():
    store = _store()
    last_modified = store.library_last_modified()
    entries = [
        Entry(
            id=feed_id("/opds/new"),
            title="Recent",
            updated=last_modified,
            summary="Everything, newest first.",
            links=[Link("subsection", url("/opds/new"), ACQ_TYPE)],
        ),
        Entry(
            id=feed_id("/opds/collections"),
            title="Collections",
            updated=last_modified,
            summary="Grouped by inbox folder.",
            links=[Link("subsection", url("/opds/collections"), NAV_TYPE)],
        ),
        Entry(
            id=feed_id("/opds/tags"),
            title="Tags",
            updated=last_modified,
            summary="Grouped by front-matter tag.",
            links=[Link("subsection", url("/opds/tags"), NAV_TYPE)],
        ),
        Entry(
            id=feed_id("/opds/all"),
            title="All documents",
            updated=last_modified,
            summary=f"{store.count()} documents, A to Z.",
            links=[Link("subsection", url("/opds/all"), ACQ_TYPE)],
        ),
    ]
    return _navigation_feed("/opds", "Home", entries, last_modified)


@bp.route("/opds/new")
@require_opds
def new():
    # Recommended as the saved catalog URL on the device: the newest work is
    # then the first screen, with no navigation.
    return _acquisition_feed("/opds/new", "Recent", "new")


@bp.route("/opds/all")
@require_opds
def all_documents():
    return _acquisition_feed("/opds/all", "All documents", "all")


@bp.route("/opds/collections")
@require_opds
def collections():
    store = _store()
    last_modified = store.library_last_modified()
    entries = [
        Entry(
            id=feed_id(f"/opds/collections/{name}"),
            title=name,
            updated=updated,
            summary=f"{count} document{'s' if count != 1 else ''}.",
            links=[Link("subsection", url(f"/opds/collections/{name}"), ACQ_TYPE)],
        )
        for name, count, updated in store.collections()
    ]
    return _navigation_feed("/opds/collections", "Collections", entries,
                            last_modified, up="/opds")


@bp.route("/opds/collections/<name>")
@require_opds
def collection(name: str):
    return _acquisition_feed(f"/opds/collections/{name}", name, "collection", name)


@bp.route("/opds/tags")
@require_opds
def tags():
    store = _store()
    last_modified = store.library_last_modified()
    entries = [
        Entry(
            id=feed_id(f"/opds/tags/{tag}"),
            title=tag,
            updated=updated,
            summary=f"{count} document{'s' if count != 1 else ''}.",
            links=[Link("subsection", url(f"/opds/tags/{tag}"), ACQ_TYPE)],
        )
        for tag, count, updated in store.tags()
    ]
    return _navigation_feed("/opds/tags", "Tags", entries, last_modified, up="/opds")


@bp.route("/opds/tags/<tag>")
@require_opds
def tag(tag: str):
    return _acquisition_feed(f"/opds/tags/{tag}", tag, "tag", tag)


@bp.route("/opds/search")
@require_opds
def search():
    q = (request.args.get("q") or "").strip()
    return _acquisition_feed("/opds/search", f"Search: {q}" if q else "Search",
                             "search", q)


@bp.route("/opds/opensearch.xml")
@require_opds
def opensearch():
    cfg = _cfg()
    store = _store()
    body = render_template(
        "opds/opensearch.xml",
        site_title=cfg.site_title,
        # {searchTerms} is substituted by the client, so this URL is built as
        # a literal rather than through url().
        template=url("/opds/search") + "?q={searchTerms}",
        acq_type=ACQ_TYPE,
    )
    return _respond(body, OPENSEARCH_TYPE, store.library_last_modified())
