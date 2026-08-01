"""Request-time auth for both doors, as Flask decorators.

Three invariants live in this file. Breaking any of them breaks the Kindle in
a way that presents as "nothing happened", so they are asserted here rather
than left to convention:

1. /opds/** NEVER returns a 3xx and NEVER returns an HTML login page.
   Verified worst failure mode: KOReader follows the redirect, feeds the HTML
   to its Atom parser, and shows a silent empty catalog with no error.
   Unauthenticated /opds gets a plain 401 + WWW-Authenticate: Basic.

2. Acquisition URLs (/d/<id>/<name>.epub) accept EITHER door -- Basic for the
   Kindle, session cookie for a browser -- and also never redirect. Same
   reason: the Kindle downloads from these URLs, and luasocket drops
   user/password across a redirect hop.

3. Browser-facing HTML pages may redirect to /login, because a browser
   handles that correctly and a Kindle never touches them.
"""

from __future__ import annotations

import base64
import binascii
import logging
import time
from collections.abc import Callable
from functools import wraps

from flask import current_app, g, redirect, request, url_for
from werkzeug.exceptions import Unauthorized

from . import (
    DEVICE,
    WEB,
    Principal,
    digests_equal,
    password_epoch,
    session_id_digest,
    token_digest,
)

log = logging.getLogger("kindle_hub.auth")

_GENERIC_401 = "Authentication required.\n"


def _unauthorized_basic() -> Unauthorized:
    """A plain 401 with a Basic challenge and a body that says nothing.

    There are no usernames in this system, so there is nothing to enumerate,
    and the body never distinguishes "wrong password" from "no such user".
    """
    exc = Unauthorized(_GENERIC_401)
    exc.response = current_app.response_class(
        _GENERIC_401,
        status=401,
        mimetype="text/plain",
        headers={
            # The realm is deliberately boring. KOReader shows the catalog
            # name it stored locally, not this.
            "WWW-Authenticate": 'Basic realm="kindle-os", charset="UTF-8"',
            "Cache-Control": "no-store",
        },
    )
    return exc


# --- door 1: web session --------------------------------------------------


def current_session_principal() -> Principal | None:
    cfg = current_app.config["HUB_CONFIG"]
    store = current_app.config["HUB_STORE"]
    raw = request.cookies.get(cfg.cookie_name)
    if not raw:
        return None
    if store.touch_session(
        session_id_digest(raw),
        cfg.session_idle_days,
        cfg.session_absolute_days,
        pw_epoch=password_epoch(cfg.web_password_hash),
    ):
        return Principal(kind=WEB, name="web")
    return None


# --- door 2: kindle device token -----------------------------------------


def current_basic_principal() -> Principal | None:
    """Validate a preemptive HTTP Basic header against the device_tokens table.

    KOReader builds this header on request #1 without waiting for a 401
    (verified), so this is the only auth exchange that ever happens on the
    Kindle path.
    """
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    name, _, token = decoded.partition(":")
    if not name or not token:
        return None

    store = current_app.config["HUB_STORE"]
    stored = store.device_token_digest(name)
    if stored is None:
        # Burn a comparable amount of time on an unknown name so the response
        # time does not distinguish "no such device" from "wrong token".
        token_digest(token)
        return None
    if not digests_equal(stored, token_digest(token)):
        return None

    store.note_device_use(name, _client_ip(), request.path, "ok")
    return Principal(kind=DEVICE, name=name)


def _client_ip() -> str:
    """The peer address as seen by our own reverse proxy.

    SECURITY FIX (found independently by all three review lenses). This used
    to return X-Forwarded-For.split(",")[0] -- the FIRST element. Our nginx
    sets the header with $proxy_add_x_forwarded_for, which APPENDS the real
    peer to whatever the client already sent. So the first element is entirely
    client-controlled and the trustworthy one is the LAST.

    That mattered more than it looks: the per-device access log is the only
    compensating control the design has for the two Kindle risks it knowingly
    accepts (MITM capture on an unvalidated TLS connection, and token theft
    off the unencrypted USB partition). An attacker who set X-Forwarded-For to
    San's home address made stolen-token use look like normal use.

    X-Real-IP is preferred where present because nginx sets it to
    $remote_addr, which a client cannot influence at all.
    """
    real = request.headers.get("X-Real-IP", "").strip()
    if real:
        return real
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        # Last element: appended by our proxy, not supplied by the caller.
        parts = [p.strip() for p in fwd.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.remote_addr or "-"


# --- decorators -----------------------------------------------------------


def require_opds(view: Callable) -> Callable:
    """Kindle door only. Basic or 401. Never a redirect, never HTML."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        principal = current_basic_principal()
        if principal is None:
            # A browser hitting /opds while logged in is a legitimate case
            # (poking at the feed by hand), so accept a session too. It still
            # never redirects: no session means 401, not /login.
            principal = current_session_principal()
        if principal is None:
            log.info("opds 401 path=%s ip=%s", request.path, _client_ip())
            raise _unauthorized_basic()
        g.principal = principal
        return view(*args, **kwargs)

    return wrapper


def require_any(view: Callable) -> Callable:
    """Either door, no redirect. Used for acquisition URLs, which are fetched
    by the Kindle with Basic and by the browser with a cookie."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        principal = current_basic_principal() or current_session_principal()
        if principal is None:
            log.info("download 401 path=%s ip=%s", request.path, _client_ip())
            raise _unauthorized_basic()
        g.principal = principal
        return view(*args, **kwargs)

    return wrapper


def require_web(view: Callable) -> Callable:
    """Browser-facing HTML. Redirects to /login, which is safe here because no
    Kindle code path ever requests these routes."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        principal = current_session_principal()
        if principal is None:
            return redirect(url_for("reader.login", next=request.full_path.rstrip("?")))
        g.principal = principal
        return view(*args, **kwargs)

    return wrapper


def require_admin(view: Callable) -> Callable:
    """Admin scope == the web session. There is exactly one human and one
    trust level; a separate admin credential would be ceremony, not security.

    NOTE (seam): if a second reader with lower privileges ever appears, this
    is the function that has to grow a real scope check, and `sessions` grows
    a scope column. Do not add roles before that day arrives.
    """

    @wraps(view)
    def wrapper(*args, **kwargs):
        principal = current_session_principal()
        if principal is None:
            raise _unauthorized_basic()
        g.principal = principal
        return view(*args, **kwargs)

    return wrapper


# --- login throttling -----------------------------------------------------


def constant_failure_delay() -> None:
    """~1s constant delay on a failed web login.

    This is a speed bump, not the control. The real controls are nginx
    limit_req on /login and the passphrase's own entropy. Deliberately NOT an
    account lockout: with a single shared password, lockout is a denial of
    service button pointed at the owner.
    """
    cfg = current_app.config["HUB_CONFIG"]
    if cfg.login_fail_delay_seconds > 0:
        time.sleep(cfg.login_fail_delay_seconds)
