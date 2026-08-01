"""Admin surface: health, a manual rebuild trigger, and the two kill switches.

Small on purpose. Everything here is reachable only with a valid web session
(see require_admin -- admin scope IS the web session, because there is exactly
one human and one trust level).
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import time

from flask import Blueprint, current_app, jsonify, make_response, redirect, url_for

from ..auth.middleware import require_admin
from .reader import _check_csrf

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.route("/health")
@require_admin
def health():
    cfg = current_app.config["HUB_CONFIG"]
    store = current_app.config["HUB_STORE"]
    builder = current_app.config.get("HUB_BUILDER")

    last_sweep = store.get_meta("last_sweep")
    status = {
        "documents": store.count(),
        "sessions": store.session_count(),
        "devices": [dict(row) for row in store.device_tokens()],
        "last_sweep_epoch": int(last_sweep) if last_sweep else None,
        "last_sweep_age_seconds": (
            int(time.time()) - int(last_sweep) if last_sweep else None
        ),
        "owns_build_lock": bool(builder and builder.owns_lock),
        "library_last_modified": store.library_last_modified(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "selinux": _getenforce(),
        "inbox_readable": os.access(cfg.inbox_dir, os.R_OK),
        "library_writable": os.access(cfg.library_dir, os.W_OK),
        "xaccel": cfg.use_xaccel,
    }
    # SELinux is Disabled on the target box today. If it is ever returned to
    # enforcing, three things break at once (proxy_pass to 127.0.0.1, the bind
    # mounts, and the X-Accel-Redirect file reads), so a change here is a
    # health failure rather than a note.
    healthy = (
        status["inbox_readable"]
        and status["library_writable"]
        and status["selinux"] in ("Disabled", "unknown")
    )
    return jsonify({"ok": healthy, **status}), (200 if healthy else 503)


def _getenforce() -> str:
    try:
        return subprocess.run(
            ["getenforce"], capture_output=True, text=True, timeout=2
        ).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


@bp.route("/rebuild", methods=["POST"])
@require_admin
def rebuild():
    _check_csrf()
    builder = current_app.config.get("HUB_BUILDER")
    if builder is None or not builder.owns_lock:
        return make_response(
            jsonify({"ok": False, "error": "this process does not own the build lock"}),
            409,
        )
    builder.request_rebuild()
    return redirect(url_for("reader.home"))


@bp.route("/logout-everywhere", methods=["POST"])
@require_admin
def logout_everywhere():
    """The kill switch for a lost phone: wipe every server-side session.

    Session cookies live 30 days, so a stolen unlocked device inside that
    window reads the hub. This is the answer to that, and it is one row
    delete per session.
    """
    _check_csrf()
    dropped = current_app.config["HUB_STORE"].drop_all_sessions()
    resp = make_response(redirect(url_for("reader.login")))
    resp.delete_cookie(current_app.config["HUB_CONFIG"].cookie_name, path="/")
    current_app.logger.warning("all sessions dropped (%d)", dropped)
    return resp
