"""Regression tests for the findings from the adversarial security review.

Every test here maps to a specific finding. They exist because each of these
bugs was invisible from the inside: the app worked, the Kindle worked, and the
existing suite passed while the hole was open. If one of these ever goes red,
read the docstring before "fixing" the test.

The pre-existing suite covered only the Kindle door; the review noted the web
door had zero coverage, which is how the open redirect survived.
"""

from __future__ import annotations

import base64

import pytest
from conftest import DEVICE_NAME, DEVICE_TOKEN, WEB_PASSWORD

# --- finding: open redirect in _safe_next ---------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "/\\evil.example",  # THE bug: backslash, which browsers read as "/"
        "//evil.example",
        "/\\\\evil.example",
        "\\/evil.example",
        "https://evil.example",
        "http://evil.example/path",
        "//evil.example/@keep",
        "/\tevil",  # control character smuggling
        "/next\nLocation: https://evil.example",
    ],
)
def test_safe_next_refuses_offsite_targets(app, hostile):
    """A login-page open redirect is a complete compromise of the web door.

    The victim sees the real hostname, the real certificate, and the real
    login form, then gets bounced to a clone that asks again. With one shared
    passphrase guarding everything, one click is the whole system.

    The original check rejected "//evil" but accepted "/\\evil"; Werkzeug
    emits the backslash verbatim and browsers normalise it to "/" per the
    WHATWG URL spec.
    """
    from kindle_hub.web.reader import _safe_next

    with app.test_request_context("/login"):
        got = _safe_next(hostile)
    assert not got.startswith("//"), f"{hostile!r} -> {got!r} (protocol-relative)"
    assert "\\" not in got, f"{hostile!r} -> {got!r} (backslash survived)"
    assert "evil.example" not in got, f"{hostile!r} -> {got!r} (offsite)"
    assert "\n" not in got and "\r" not in got


@pytest.mark.parametrize(
    "benign", ["/", "/d/abc123/", "/shelf/notes", "/d/x/?page=2", "/a#frag"]
)
def test_safe_next_keeps_local_paths(app, benign):
    """The fix must not break the feature it guards."""
    from kindle_hub.web.reader import _safe_next

    with app.test_request_context("/login"):
        got = _safe_next(benign)
    assert got.startswith("/")
    assert "evil" not in got


def test_login_page_never_emits_an_offsite_location(client):
    """End to end: the hostile `next` must not reach the Location header."""
    resp = client.get("/login?next=/\\evil.example")
    assert resp.status_code in (200, 302)
    assert "evil.example" not in resp.headers.get("Location", "")
    assert b"evil.example" not in resp.data


# --- finding: X-Forwarded-For read from the wrong end ---------------------


def test_client_ip_ignores_client_supplied_xff(app):
    """Our nginx uses $proxy_add_x_forwarded_for, which APPENDS the real peer.

    So the first element is whatever the caller sent and the last is ours.
    Reading the first let anyone holding a stolen device token write their own
    address into the per-token access log -- the only compensating control the
    design has for MITM capture and USB token theft.
    """
    from kindle_hub.auth.middleware import _client_ip

    with app.test_request_context(
        "/opds", headers={"X-Forwarded-For": "203.0.113.9, 198.51.100.7"}
    ):
        assert _client_ip() == "198.51.100.7"

    # X-Real-IP is $remote_addr and cannot be influenced by the client at all.
    with app.test_request_context(
        "/opds",
        headers={"X-Forwarded-For": "203.0.113.9", "X-Real-IP": "198.51.100.7"},
    ):
        assert _client_ip() == "198.51.100.7"


# --- finding: revoked device tokens resurrected by re-seeding -------------


def test_revoked_token_is_not_resurrected_by_seeding(cfg, tmp_path):
    """The scenario: the Kindle is lost, the token is revoked, then anything
    restarts the container -- a deploy, a reboot, an OOM kill, or the very act
    of rotating the web password (which the docs say to do by editing
    secrets.env and restarting).

    Seeding runs on every startup from HUB_OPDS_TOKENS. Before the tombstone,
    that quietly re-registered the revoked digest and the thief was back in,
    with only an info-level "device token registered" line to show for it.
    """
    from kindle_hub import auth
    from kindle_hub.app import seed_device_tokens
    from kindle_hub.catalog.store import Store

    store = Store(tmp_path / "revoke.db")
    store.migrate()

    digest = auth.token_digest("a-token-that-will-be-revoked")
    assert store.upsert_device_token("kindle-lost", digest) is True
    assert store.device_token_digest("kindle-lost") == digest

    assert store.revoke_device_token("kindle-lost", reason="device lost") is True
    assert store.device_token_digest("kindle-lost") is None

    # Simulate a restart: the env still names the revoked digest.
    class _Cfg:
        opds_token_seed = f"kindle-lost:{digest}"

    seed_device_tokens(_Cfg(), store)
    assert store.device_token_digest("kindle-lost") is None, (
        "revoked token came back after re-seeding -- revocation is not durable"
    )

    # And a direct upsert must refuse it too, not just the seeding path.
    assert store.upsert_device_token("kindle-lost", digest) is False

    # Reuse of the NAME must be a deliberate act, never an accident.
    assert store.unrevoke_device_name("kindle-lost") == 1
    assert store.upsert_device_token("kindle-lost", digest) is True


def test_revoked_token_is_rejected_at_the_door(app, client):
    """The tombstone has to matter to a real request, not only to the store."""
    store = app.config["HUB_STORE"]
    raw = f"{DEVICE_NAME}:{DEVICE_TOKEN}".encode()
    hdr = {"Authorization": "Basic " + base64.b64encode(raw).decode()}

    assert client.get("/opds", headers=hdr).status_code == 200
    try:
        assert store.revoke_device_token(DEVICE_NAME, reason="test") is True
        assert client.get("/opds", headers=hdr).status_code == 401
    finally:
        # Restore for the rest of the session-scoped suite.
        store.unrevoke_device_name(DEVICE_NAME)
        from kindle_hub import auth

        store.upsert_device_token(DEVICE_NAME, auth.token_digest(DEVICE_TOKEN))


# --- finding: the device access log is one entry deep ---------------------


def test_device_access_log_is_append_only(app, tmp_path):
    """last_ip was a single overwritten column, so the next legitimate request
    erased any trace of an intruder. An audit trail with depth 1 cannot answer
    "did someone else use my token, and from where".
    """
    from kindle_hub.catalog.store import Store

    store = Store(tmp_path / "access.db")
    store.migrate()
    store.upsert_device_token("kindle-x", "0" * 64)

    store.note_device_use("kindle-x", "198.51.100.7", "/opds", "ok")
    store.note_device_use("kindle-x", "203.0.113.9", "/opds/new", "ok")
    store.note_device_use("kindle-x", "198.51.100.7", "/opds", "ok")

    rows = store.device_access_log("kindle-x")
    assert len(rows) == 3, "history was overwritten instead of appended"
    assert {r["ip"] for r in rows} == {"198.51.100.7", "203.0.113.9"}


# --- finding: password rotation did not invalidate sessions ---------------


def test_rotating_the_password_kills_existing_sessions(cfg, tmp_path):
    """Rotation is the thing you do *because* a passphrase leaked. Before this,
    an attacker's cookie stayed valid for the full 90-day absolute cap, so
    rotation changed what the next login typed and revoked nothing.
    """
    from kindle_hub import auth
    from kindle_hub.catalog.store import Store

    store = Store(tmp_path / "sessions.db")
    store.migrate()

    old_hash = auth.hash_password("the-leaked-passphrase")
    old_epoch = auth.password_epoch(old_hash)

    sid = auth.new_session_id()
    store.create_session(auth.session_id_digest(sid), "pytest", pw_epoch=old_epoch)

    assert store.touch_session(
        auth.session_id_digest(sid), 30, 90, pw_epoch=old_epoch
    ) is True

    new_epoch = auth.password_epoch(auth.hash_password("the-new-passphrase"))
    assert new_epoch != old_epoch
    assert store.touch_session(
        auth.session_id_digest(sid), 30, 90, pw_epoch=new_epoch
    ) is False, "session survived a password rotation"

    # And it must be gone, not merely refused once.
    assert store.touch_session(
        auth.session_id_digest(sid), 30, 90, pw_epoch=old_epoch
    ) is False


def test_web_login_still_works(client):
    """Guard against the epoch plumbing breaking the ordinary happy path."""
    page = client.get("/login")
    assert page.status_code == 200
    resp = client.post(
        "/login",
        data={"password": WEB_PASSWORD, "csrf": _csrf(client, page)},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.data[:400]


def _csrf(client, page) -> str:
    import re

    m = re.search(rb'name="csrf"[^>]*value="([^"]+)"', page.data)
    if m:
        return m.group(1).decode()
    return client.get_cookie("hub_csrf").value if client.get_cookie("hub_csrf") else ""


# --- finding: argon2 arena x threads exceeded the container cap -----------


def test_argon2_memory_fits_the_container_budget():
    """4 concurrent verifies must fit inside mem_limit with room for the
    interpreter. At the original 64 MiB the arithmetic was 4 x 64 = 256 MiB
    against a 256 MiB hard cap with swap disabled, so five unauthenticated
    requests OOM-killed the hub in a restart loop.
    """
    from kindle_hub.auth import _hasher

    mib = _hasher.memory_cost / 1024
    assert mib <= 24, f"argon2 memory_cost is {mib} MiB; 4 concurrent will not fit"
    assert _hasher.time_cost >= 2
    assert _hasher.parallelism == 1  # single vCPU box


def test_concurrent_verifies_are_bounded():
    """Belt and braces: the app must not depend on an nginx file it does not
    own for a memory bound. The missing-config finding was exactly that.
    """
    from kindle_hub.auth import _verify_slots

    assert _verify_slots._initial_value <= 2
