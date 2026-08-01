"""The KOReader source findings, as assertions.

This is the file that stops a future refactor from silently breaking the
Kindle. Every case here corresponds to a behaviour of KOReader v2025.10's
OPDS client whose failure mode is silence: an empty catalog, or a download
link that never appears, with no error shown on the device.

If one of these fails, do not relax the assertion. Fix the feed.
"""

from __future__ import annotations

import re
import zipfile
from urllib.parse import urlparse

import pytest

FEED_ROUTES = [
    "/opds",
    "/opds/new",
    "/opds/all",
    "/opds/collections",
    "/opds/collections/notes",
    "/opds/tags",
    "/opds/tags/infra",
    "/opds/search?q=vps",
    "/opds/opensearch.xml",
]

FORBIDDEN_MARKUP = ["<!--", "<![CDATA[", "<?xml-stylesheet"]


@pytest.mark.parametrize("route", FEED_ROUTES)
def test_feed_has_no_markup_the_parser_mangles(client, basic_auth, route):
    """KOReader regex-preprocesses comments, CDATA and stylesheet PIs before
    handing the result to a fragile lexer."""
    body = client.get(route, headers=basic_auth).get_data(as_text=True)
    for token in FORBIDDEN_MARKUP:
        assert token not in body, f"{route} contains {token}"


@pytest.mark.parametrize("route", FEED_ROUTES)
def test_head_returns_last_modified(client, basic_auth, route):
    """KOReader keys its in-memory feed cache on the Last-Modified value from
    a preflight HEAD. Omit it and the shelf goes stale for the session."""
    resp = client.head(route, headers=basic_auth)
    assert resp.status_code == 200
    assert resp.headers.get("Last-Modified"), f"{route} answered HEAD without it"


@pytest.mark.parametrize("route", FEED_ROUTES)
def test_no_route_on_a_kindle_path_redirects(client, basic_auth, route):
    """luasocket drops user/password across a redirect hop, and a redirect to
    an HTML login page gets parsed as Atom and shows an empty catalog."""
    for headers in ({}, basic_auth):
        resp = client.get(route, headers=headers)
        assert not 300 <= resp.status_code < 400, (route, resp.status_code)


@pytest.mark.parametrize("route", FEED_ROUTES)
def test_unauthenticated_gets_a_plain_401_challenge(client, route):
    resp = client.get(route)
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"].startswith("Basic")
    assert "html" not in resp.headers["Content-Type"]


def test_summaries_are_plain_text(client, basic_auth):
    """The client escapes the whole <content> body to stop its lexer choking
    on embedded XHTML, so summaries must declare type="text"."""
    body = client.get("/opds/new", headers=basic_auth).get_data(as_text=True)
    for match in re.finditer(r"<summary\b([^>]*)>", body):
        assert 'type="text"' in match.group(1)
    assert "<content" not in body


def _acquisition_links(body: str) -> list[tuple[str, str]]:
    """(mime, href) for every acquisition link, whatever order the attributes
    happen to be written in."""
    out = []
    for tag in re.findall(r"<link\b[^>]*>", body):
        if 'rel="http://opds-spec.org/acquisition"' not in tag:
            continue
        mime = re.search(r'type="([^"]+)"', tag)
        href = re.search(r'href="([^"]+)"', tag)
        if mime and href:
            out.append((mime.group(1), href.group(1)))
    return out


def test_acquisition_links_are_downloadable(client, basic_auth, app):
    """getFiletype checks the filename suffix BEFORE the MIME type, and a link
    matching neither is silently dropped from the download dialog."""
    body = client.get("/opds/all", headers=basic_auth).get_data(as_text=True)
    links = _acquisition_links(body)
    assert links, "no acquisition links in the feed"
    origin = urlparse(app.config["HUB_CONFIG"].public_origin)
    for mime, href in links:
        assert mime == "application/epub+zip"
        assert href.endswith(".epub"), href
        assert "?" not in href, href
        parsed = urlparse(href)
        assert (parsed.scheme, parsed.netloc) == (origin.scheme, origin.netloc), href


def test_acquisition_links_declare_a_length(client, basic_auth):
    body = client.get("/opds/all", headers=basic_auth).get_data(as_text=True)
    entry = body[body.index("<entry>"):body.index("</entry>")]
    assert re.search(r'rel="http://opds-spec\.org/acquisition".*?length="\d+"', entry,
                     re.S)


def test_download_needs_auth_and_never_redirects(client, basic_auth, app):
    body = client.get("/opds/all", headers=basic_auth).get_data(as_text=True)
    href = _acquisition_links(body)[0][1]
    path = urlparse(href).path

    anon = client.get(path)
    assert anon.status_code == 401
    assert anon.headers["WWW-Authenticate"].startswith("Basic")

    authed = client.get(path, headers=basic_auth)
    assert authed.status_code == 200
    # X-Accel-Redirect is on, so nginx supplies the bytes and the app's body
    # is empty by design.
    assert authed.headers["X-Accel-Redirect"].startswith(
        app.config["HUB_CONFIG"].xaccel_prefix
    )


def test_pagination_advertises_next(client, basic_auth):
    """Page size is 1 in the fixture config, and there are two documents."""
    body = client.get("/opds/new", headers=basic_auth).get_data(as_text=True)
    assert 'rel="next"' in body
    page2 = client.get("/opds/new?page=2", headers=basic_auth).get_data(as_text=True)
    assert 'rel="previous"' in page2
    assert 'rel="first"' in page2


def test_feed_ids_are_stable_across_requests(client, basic_auth):
    first = client.get("/opds/new", headers=basic_auth).get_data(as_text=True)
    second = client.get("/opds/new", headers=basic_auth).get_data(as_text=True)
    assert re.findall(r"<id>(.*?)</id>", first) == re.findall(r"<id>(.*?)</id>", second)


def test_xml_special_characters_are_escaped(client, basic_auth):
    """A title containing & or < must not produce a feed the lexer chokes on."""
    body = client.get("/opds/new", headers=basic_auth).get_data(as_text=True)
    assert "&amp;" in body
    import xml.etree.ElementTree as ET

    ET.fromstring(body)  # raises if the feed is not well formed


# --- EPUB container -------------------------------------------------------


def _one_epub(cfg):
    """The audit fixture, which is the one carrying tables, code and an image
    reference. plain.md exists only to make the library have two documents."""
    return next(cfg.library_dir.glob("*/vps2-capacity-audit*.epub"))


def test_mimetype_is_first_and_stored(cfg):
    with zipfile.ZipFile(_one_epub(cfg)) as zf:
        first = zf.infolist()[0]
        assert first.filename == "mimetype"
        assert first.compress_type == zipfile.ZIP_STORED
        assert zf.read("mimetype") == b"application/epub+zip"


def test_container_points_at_the_opf(cfg):
    with zipfile.ZipFile(_one_epub(cfg)) as zf:
        container = zf.read("META-INF/container.xml").decode()
        assert 'full-path="EPUB/package.opf"' in container
        assert "\\" not in container


def test_every_xhtml_document_is_well_formed(cfg):
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(_one_epub(cfg)) as zf:
        names = [n for n in zf.namelist() if n.endswith((".xhtml", ".opf", ".ncx"))]
        assert names
        for name in names:
            ET.fromstring(zf.read(name))


def test_build_is_byte_deterministic(cfg):
    """The build hash goes in the filename, which is how KOReader tells a
    revision apart from the copy already on the device."""
    from kindle_hub.builder import sweep
    from kindle_hub.catalog.store import Store

    path = _one_epub(cfg)
    before = path.read_bytes()
    sweep(cfg, Store(cfg.db_path), force=True)
    assert path.read_bytes() == before


def test_code_is_reflowed_in_the_epub_but_not_on_the_web(cfg, client, basic_auth):
    with zipfile.ZipFile(_one_epub(cfg)) as zf:
        epub_html = "".join(
            zf.read(n).decode() for n in zf.namelist() if n.endswith(".xhtml")
        )
    for block in re.findall(r"<pre><code>(.*?)</code></pre>", epub_html, re.S):
        for line in block.split("\n"):
            assert len(line) <= cfg.code_columns, line

    from kindle_hub.catalog.store import Store

    store = Store(cfg.db_path)
    docs, _ = store.shelf("new")
    wide = store.get_document_html(docs[0].doc_id) or store.get_document_html(
        docs[1].doc_id
    )
    longest = max(
        len(line)
        for block in re.findall(r"<pre><code>(.*?)</code></pre>", wide, re.S)
        for line in block.split("\n")
    )
    assert longest > cfg.code_columns, "the web reader should keep the original lines"


def test_wide_table_becomes_records_and_narrow_stays_a_grid(cfg):
    with zipfile.ZipFile(_one_epub(cfg)) as zf:
        html = "".join(
            zf.read(n).decode() for n in zf.namelist() if n.endswith(".xhtml")
        )
    assert 'class="records"' in html, "the 9-column table should be transposed"
    assert "<table>" in html, "the 2-column table should stay a grid"


def test_remote_images_are_not_fetched(cfg):
    with zipfile.ZipFile(_one_epub(cfg)) as zf:
        html = "".join(
            zf.read(n).decode() for n in zf.namelist() if n.endswith(".xhtml")
        )
        assert not [n for n in zf.namelist() if n.startswith("EPUB/images/")]
    assert "image not available" in html


def test_no_section_degraded_to_escaped_text(cfg):
    """epub.py degrades a section to <pre>escaped markup</pre> if the XHTML it
    generated is not well formed, so a broken book never ships. That fallback
    must not fire on ordinary input.

    The way to trip it is an HTML named entity: only the five XML built-ins
    are legal in EPUB XHTML, so a stray &mdash; or &nbsp; anywhere in the
    renderers turns a whole chapter into escaped source.
    """
    with zipfile.ZipFile(_one_epub(cfg)) as zf:
        for name in zf.namelist():
            if not name.endswith(".xhtml"):
                continue
            body = zf.read(name).decode()
            assert "&lt;h1&gt;" not in body, f"{name} fell back to escaped text"
            assert "&lt;div class=" not in body, f"{name} fell back to escaped text"
