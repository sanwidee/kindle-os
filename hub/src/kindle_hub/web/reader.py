"""The web door: login, shelves, the reader view, and file delivery.

File bytes never pass through Python. The app authorizes the request and then
returns X-Accel-Redirect pointing at an nginx `internal` location that aliases
the library directory. `internal` means nginx refuses any external request for
that path outright, so there is no URL an unauthenticated client can guess
that reaches a file: the auth check is structurally in front of every byte,
and the single-vCPU box never busy-loops streaming files out of Python.
"""

from __future__ import annotations

import logging
import re
import secrets
from urllib.parse import urlsplit, urlunsplit

from flask import (
    Blueprint,
    Response,
    current_app,
    g,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.exceptions import BadRequest, NotFound

from .. import auth
from ..auth.middleware import (
    constant_failure_delay,
    current_session_principal,
    require_any,
    require_web,
)

log = logging.getLogger("kindle_hub.reader")

bp = Blueprint("reader", __name__)

CSRF_COOKIE = "hub_csrf"
SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._-]+$")


def _cfg():
    return current_app.config["HUB_CONFIG"]


def _store():
    return current_app.config["HUB_STORE"]


# --- CSRF -----------------------------------------------------------------
#
# The hub is read-only over HTTP: GETs render or serve content, and the only
# state-changing endpoints are POST /login and POST /logout. SameSite=Lax
# already blocks cross-site POST; a double-submit token on top of that is
# cheap and kills login-CSRF specifically. Any mutating endpoint added later
# inherits this requirement -- do not add one without it.


def _set_csrf_cookie(response: Response, token: str) -> None:
    cfg = _cfg()
    response.set_cookie(
        CSRF_COOKIE,
        token,
        secure=cfg.cookie_secure,
        httponly=False,
        samesite="Lax",
        path="/",
        max_age=60 * 60 * 24 * 30,
    )


def ensure_csrf_token() -> None:
    """App-wide before_request hook. Every rendered page carries a token, so
    the logout form on a page left open for a week still works."""
    g.csrf_token = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(16)


def attach_csrf_cookie(response: Response) -> Response:
    """App-wide after_request hook, paired with ensure_csrf_token."""
    token = getattr(g, "csrf_token", None)
    if token and request.cookies.get(CSRF_COOKIE) != token:
        _set_csrf_cookie(response, token)
    return response


def _check_csrf() -> None:
    cookie = request.cookies.get(CSRF_COOKIE, "")
    form = request.form.get("csrf", "")
    if not cookie or not form or not auth.digests_equal(cookie, form):
        raise BadRequest("Stale form. Reload the page and try again.")


# --- login / logout -------------------------------------------------------


def _safe_next(raw: str | None) -> str:
    """Only ever redirect to a path on this app. An open redirect on the login
    page is a phishing primitive and costs nothing to close.

    SECURITY FIX (review: auth-bypass, finding 1). The previous version
    rejected "//evil.com" but accepted "/\\evil.com". Werkzeug emits the
    backslash in the Location header verbatim, and per the WHATWG URL spec a
    browser treats "\\" as "/" in a special-scheme URL -- so the victim
    resolved it to https://evil.com while looking at our real hostname, our
    real certificate, and our real login form. With a single shared passphrase
    guarding everything, that is the whole system for one click.

    Parse instead of pattern-match: anything carrying a scheme, a netloc, or a
    second leading slash-or-backslash is refused outright.
    """
    if not raw:
        return url_for("reader.home")
    # Control characters and whitespace can smuggle a header or confuse the
    # parser; nothing legitimate contains them.
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in raw):
        return url_for("reader.home")
    if not raw.startswith("/") or raw[1:2] in ("/", "\\"):
        return url_for("reader.home")
    if "\\" in raw:
        return url_for("reader.home")
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc:
        return url_for("reader.home")
    return urlunsplit(("", "", parsed.path, parsed.query, parsed.fragment))


@bp.route("/login", methods=["GET"])
def login():
    if current_session_principal() is not None:
        return redirect(_safe_next(request.args.get("next")))
    resp = make_response(
        render_template(
            "web/login.html",
            site_title=_cfg().site_title,
            next=_safe_next(request.args.get("next")),
            error=None,
        )
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/login", methods=["POST"])
def login_post():
    _check_csrf()
    cfg = _cfg()
    store = _store()
    password = request.form.get("password", "")
    target = _safe_next(request.form.get("next"))

    if not auth.verify_password(cfg.web_password_hash, password):
        # Generic failure. There are no usernames here, so there is nothing to
        # enumerate and nothing to distinguish.
        constant_failure_delay()
        log.info("login failed ip=%s", request.headers.get("X-Forwarded-For",
                                                           request.remote_addr))
        resp = make_response(
            render_template(
                "web/login.html",
                site_title=cfg.site_title,
                next=target,
                error="Incorrect password.",
            ),
            401,
        )
        resp.headers["Cache-Control"] = "no-store"
        return resp

    session_id = auth.new_session_id()
    store.create_session(
        auth.session_id_digest(session_id),
        request.headers.get("User-Agent", ""),
        pw_epoch=auth.password_epoch(cfg.web_password_hash),
    )
    resp = make_response(redirect(target))
    resp.set_cookie(
        cfg.cookie_name,
        session_id,
        max_age=cfg.session_idle_days * 86400,
        secure=cfg.cookie_secure,
        httponly=True,
        samesite="Lax",  # not Strict: tapping a hub link from Slack or
                         # WhatsApp should land authed, not on /login.
        path="/",
        domain=None,  # __Host- prefix forbids a Domain attribute
    )
    log.info("login ok ip=%s", request.headers.get("X-Forwarded-For",
                                                   request.remote_addr))
    return resp


@bp.route("/logout", methods=["POST"])
def logout():
    _check_csrf()
    cfg = _cfg()
    raw = request.cookies.get(cfg.cookie_name)
    if raw:
        _store().drop_session(auth.session_id_digest(raw))
    resp = make_response(redirect(url_for("reader.login")))
    resp.delete_cookie(cfg.cookie_name, path="/")
    return resp


# --- shelves --------------------------------------------------------------


PAGE_SIZE = 40


def _render_shelf(title: str, kind: str, key: str | None = None, **extra) -> Response:
    store = _store()
    page = max(1, request.args.get("page", type=int, default=1))
    docs, total = store.shelf(kind, key, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    pages = max(1, -(-total // PAGE_SIZE))
    resp = make_response(
        render_template(
            "web/shelf.html",
            site_title=_cfg().site_title,
            shelf_title=title,
            documents=docs,
            total=total,
            page=page,
            pages=pages,
            **extra,
        )
    )
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@bp.route("/")
@require_web
def home():
    return _render_shelf("Recent", "new")


@bp.route("/all")
@require_web
def all_documents():
    return _render_shelf("All documents", "all")


@bp.route("/collections/<name>")
@require_web
def collection(name: str):
    return _render_shelf(f"Collection: {name}", "collection", name)


@bp.route("/tags/<tag>")
@require_web
def tag(tag: str):
    return _render_shelf(f"Tag: {tag}", "tag", tag)


@bp.route("/search")
@require_web
def search():
    q = (request.args.get("q") or "").strip()
    return _render_shelf(f"Search: {q}" if q else "Search", "search", q, query=q)


# --- document -------------------------------------------------------------


@bp.route("/d/<doc_id>/")
@require_web
def document(doc_id: str):
    store = _store()
    doc = store.get_document(doc_id)
    if doc is None:
        raise NotFound()
    html = store.get_document_html(doc_id) or ""
    resp = make_response(
        render_template(
            "web/doc.html",
            site_title=_cfg().site_title,
            doc=doc,
            body=html,
        )
    )
    resp.headers["Cache-Control"] = "private, no-store"
    # Keeps the acquisition URL out of the Referer header of any outbound link
    # the document happens to contain.
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@bp.route("/d/<doc_id>/<filename>")
@require_any
def download(doc_id: str, filename: str):
    """Acquisition URL. Reached by KOReader with Basic auth and by the browser
    with a session cookie. Never redirects, on either path."""
    if not SAFE_FILENAME.match(filename) or not filename.endswith(".epub"):
        raise NotFound()
    doc = _store().get_document(doc_id)
    if doc is None or doc.epub_name != filename:
        raise NotFound()
    log.info(
        "download doc=%s by=%s/%s", doc_id, g.principal.kind, g.principal.name
    )
    return _serve_library_file(
        f"{doc_id}/{filename}", "application/epub+zip", download_name=filename
    )


@bp.route("/d/<doc_id>/media/<filename>")
@require_any
def media(doc_id: str, filename: str):
    if not SAFE_FILENAME.match(filename) or not filename.endswith(".png"):
        raise NotFound()
    if _store().get_document(doc_id) is None:
        raise NotFound()
    return _serve_library_file(f"{doc_id}/media/{filename}", "image/png")


def _serve_library_file(
    relpath: str, content_type: str, download_name: str | None = None
) -> Response:
    cfg = _cfg()
    target = (cfg.library_dir / relpath).resolve()
    try:
        target.relative_to(cfg.library_dir.resolve())
    except ValueError:
        raise NotFound() from None
    if not target.is_file():
        raise NotFound()

    if not cfg.use_xaccel:
        # Local dev only: there is no nginx in front, so Flask serves the
        # bytes itself. Never the production path.
        return send_file(target, mimetype=content_type, conditional=True)

    resp = current_app.response_class(b"", mimetype=content_type)
    resp.headers["X-Accel-Redirect"] = cfg.xaccel_prefix + relpath
    if download_name:
        # Browsers use this; KOReader derives the filename from the URL, which
        # is why the build hash lives in the path rather than only here.
        resp.headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
    # nginx fills in Content-Length from the file it serves. Do not set a
    # zero-length one here or the transfer looks empty to the client.
    resp.headers.pop("Content-Length", None)
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp
