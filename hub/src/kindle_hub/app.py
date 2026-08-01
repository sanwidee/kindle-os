"""Flask application factory.

Flask rather than FastAPI, deliberately. The workload has no async story:
traffic is one Kindle and one browser, and the expensive work (markdown to
EPUB) is CPU-bound and happens on a background thread on the only vCPU the
box has. Async concurrency buys nothing and costs pydantic-core, starlette and
uvicorn. Meanwhile Werkzeug's conditional-response and HEAD handling matter
more than usual here, because KOReader's feed cache is built on a preflight
HEAD plus Last-Modified.
"""

from __future__ import annotations

import logging
import os

from flask import Flask, Response, request

from .builder import Builder
from .catalog.store import Store
from .config import Config
from .web import admin as admin_bp
from .web import opds as opds_bp
from .web import reader as reader_bp

log = logging.getLogger("kindle_hub")


def configure_logging() -> None:
    level = os.environ.get("HUB_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def seed_device_tokens(cfg: Config, store: Store) -> None:
    """Sync HUB_OPDS_TOKENS into the device_tokens table.

    The env carries only sha256 digests, never plaintext. Generate a token and
    its digest on your own machine with `python -m kindle_hub mint-token`, type
    the plaintext into KOReader once, and put it in the password manager. It
    should never exist on the server.

    Rows not named in the env are left alone, so a token minted at runtime via
    the CLI survives a restart.

    Revoked tokens are NEVER re-registered. Seeding runs on every startup, so
    without the tombstone check a token revoked after losing the device came
    back at the next restart with only an info-level log line to show for it.
    A refusal here is loud on purpose: it is the sound of the revocation
    working, and it tells the operator that secrets.env still holds a dead
    digest that ought to be cleaned up.
    """
    if not cfg.opds_token_seed.strip():
        return
    for pair in cfg.opds_token_seed.split(","):
        pair = pair.strip()
        if not pair:
            continue
        name, _, digest = pair.partition(":")
        name, digest = name.strip(), digest.strip().lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError(
                f"HUB_OPDS_TOKENS entry {name!r} is not a 64-character sha256 hex "
                "digest. This variable holds digests, not tokens."
            )
        if store.upsert_device_token(name, digest):
            log.info("device token %r registered", name)
        else:
            log.warning(
                "device token %r NOT registered: this digest is revoked. "
                "Remove it from HUB_OPDS_TOKENS, or run "
                "`python -m kindle_hub token unrevoke %s` if you really mean "
                "to bring it back.",
                name,
                name,
            )


def create_app(cfg: Config | None = None, start_builder: bool = True) -> Flask:
    cfg = cfg or Config.from_env()
    cfg.ensure_dirs()

    app = Flask(
        __name__,
        static_folder="assets",
        static_url_path="/static",
        template_folder="templates",
    )
    # No route may answer a request with a redirect (see the guard below), so
    # rules must match with and without a trailing slash rather than bouncing
    # the client. merge_slashes off for the same reason.
    app.url_map.strict_slashes = False
    app.url_map.merge_slashes = False
    app.config["HUB_CONFIG"] = cfg
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # only tiny form posts exist
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600

    store = Store(cfg.db_path)
    store.migrate()
    seed_device_tokens(cfg, store)
    app.config["HUB_STORE"] = store

    app.register_blueprint(reader_bp.bp)
    app.register_blueprint(opds_bp.bp)
    app.register_blueprint(admin_bp.bp)

    app.before_request(reader_bp.ensure_csrf_token)
    app.after_request(reader_bp.attach_csrf_cookie)
    app.after_request(_security_headers)
    app.after_request(_no_redirect_guard)

    @app.route("/healthz")
    def healthz() -> Response:
        """Unauthenticated on purpose, and it says nothing. This exists for the
        container healthcheck; everything informative is behind /admin/health."""
        return Response("ok\n", mimetype="text/plain")

    @app.errorhandler(404)
    def not_found(_exc):
        if request.path.startswith("/opds") or request.path.endswith(".epub"):
            # Never HTML on a Kindle path: KOReader would feed it to the Atom
            # parser and show a silent empty catalog.
            return Response("Not found.\n", status=404, mimetype="text/plain")
        return Response("Not found.\n", status=404, mimetype="text/plain")

    if start_builder:
        builder = Builder(cfg, store)
        builder.start()
        app.config["HUB_BUILDER"] = builder

    log.info(
        "kindle-os hub ready: %d documents, origin %s, xaccel=%s",
        store.count(), cfg.public_origin, cfg.use_xaccel,
    )
    return app


def _security_headers(response: Response) -> Response:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if response.mimetype == "text/html":
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'none'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
    return response


def _no_redirect_guard(response: Response) -> Response:
    """Structural enforcement of the single most dangerous invariant.

    A 3xx anywhere on a Kindle path fails in the worst possible way:
    luasocket drops user/password across the hop, or KOReader follows the
    redirect to an HTML login page and parses it as Atom, producing a silent
    empty catalog with no error. If a future route ever emits one, this turns
    it into a loud 500 in the log and a plain 401 on the wire.
    """
    path = request.path
    if not (path.startswith("/opds") or path.endswith(".epub")):
        return response
    if 300 <= response.status_code < 400:
        log.error(
            "BUG: %s returned %d on a Kindle path. See web/opds.py.",
            path, response.status_code,
        )
        return Response(
            "Authentication required.\n",
            status=401,
            mimetype="text/plain",
            headers={"WWW-Authenticate": 'Basic realm="kindle-os", charset="UTF-8"'},
        )
    return response


def wsgi_app():
    """Entry point for gunicorn: `gunicorn 'kindle_hub.app:wsgi_app()'`."""
    configure_logging()
    return create_app()
