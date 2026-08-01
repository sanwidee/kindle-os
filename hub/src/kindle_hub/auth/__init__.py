"""Auth primitives shared by both doors.

ONE content store, TWO doors:

  web door    -- shared password -> argon2id verify -> server-side session,
                 cookie is __Host- / Secure / HttpOnly / SameSite=Lax.
  kindle door -- HTTP Basic with a per-device random token, because that is
                 the only mechanism KOReader's OPDS client supports.

The two doors never share a credential. Rationale, at length, in the auth
design; the short version is that the Kindle credential is retyped once on an
e-ink keyboard and then sits in PLAINTEXT in KOReader's settings file on a
device that mounts as unencrypted USB mass storage. It must therefore be
low-value, single-purpose, and revocable in one row.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from dataclasses import dataclass

from argon2 import PasswordHasher

__all__ = [
    "Principal",
    "WEB",
    "DEVICE",
    "hash_password",
    "verify_password",
    "password_epoch",
    "token_digest",
    "digests_equal",
    "new_session_id",
    "session_id_digest",
    "new_device_token",
]

WEB = "web"
DEVICE = "device"


@dataclass(frozen=True)
class Principal:
    """Who is making this request, and through which door.

    `kind` is WEB (browser session) or DEVICE (Kindle Basic token).
    `name` is "web" for the single human, or the device token name.
    """

    kind: str
    name: str

    @property
    def is_web(self) -> bool:
        return self.kind == WEB

    @property
    def is_device(self) -> bool:
        return self.kind == DEVICE


# --- web password ---------------------------------------------------------
#
# argon2id, time_cost=2, memory_cost=19 MiB, parallelism=1 -- the OWASP
# "second choice" profile.
#
# SECURITY FIX (review: crypto-and-session, auth-bypass). This was previously
# memory_cost=65536 (64 MiB), justified by a claim that nginx limit_req made
# concurrent verifies impossible. That was wrong twice over: the nginx config
# did not exist, and even the intended `burst=5 nodelay` admits more
# simultaneous requests than the container can survive. With gunicorn running
# --threads 4 inside a 256 MiB cgroup with swap accounting disabled, four
# concurrent 64 MiB arenas plus interpreter baseline is an OOM kill that any
# unauthenticated caller can trigger with five requests.
#
# Two independent changes close it, because one of them must not depend on a
# file in another directory:
#   1. 19 MiB per verify, so 4 concurrent verifies fit the cap with headroom.
#   2. _verify_slots below, so concurrency is bounded inside the app itself.
_hasher = PasswordHasher(
    time_cost=2, memory_cost=19456, parallelism=1, hash_len=32, salt_len=16
)

# Bound concurrent argon2 verifies regardless of thread count or any external
# rate limiter. Two slots keeps a legitimate login responsive while a flood is
# in progress; the rest queue rather than allocate.
_verify_slots = threading.BoundedSemaphore(2)

# How long a caller will wait for a verify slot before being rejected. Past
# this we shed load instead of parking a worker thread indefinitely.
_VERIFY_WAIT_SECONDS = 5.0


def hash_password(password: str) -> str:
    """Produce the argon2id PHC string that goes into HUB_WEB_PASSWORD_HASH.

    Run this on your own machine, not on the server -- the plaintext password
    should never appear in the server's shell history or process table.
    """
    return _hasher.hash(password)


def verify_password(stored_hash: str, candidate: str) -> bool:
    """Constant-time-ish verify. argon2-cffi handles the comparison itself;
    any failure mode (wrong password, malformed hash) returns the same False
    so nothing distinguishable leaks to the caller.

    Verifies are serialised through a bounded semaphore: argon2-cffi releases
    the GIL while hashing, so without this every worker thread can allocate an
    arena at the same time. If no slot frees up in time we return False rather
    than wait -- a flood then costs the attacker requests without costing us
    memory. A legitimate login retried a moment later succeeds.
    """
    if not _verify_slots.acquire(timeout=_VERIFY_WAIT_SECONDS):
        return False
    try:
        return _hasher.verify(stored_hash, candidate)
    except Exception:
        return False
    finally:
        _verify_slots.release()


def password_epoch(stored_hash: str) -> str:
    """Short stable fingerprint of the CURRENT web password hash.

    Stamped into every session row at login and re-checked on each request, so
    that changing HUB_WEB_PASSWORD_HASH invalidates every session minted under
    the old password. Without this, rotating a leaked passphrase left existing
    cookies valid for the full 90-day absolute cap -- i.e. rotation did not
    actually revoke anything (review: crypto-and-session, finding 3).

    It is a digest of a secret-derived value, never shown to the client.
    """
    return hashlib.sha256(stored_hash.encode("utf-8")).hexdigest()[:32]


# --- device tokens --------------------------------------------------------


def new_device_token(nbytes: int = 24) -> str:
    """32 characters of base64 from 24 random bytes -- ~192 bits before
    encoding, comfortably beyond anything guessable. Print once, type once
    into KOReader, store once in the password manager, never again."""
    return secrets.token_urlsafe(nbytes)


def token_digest(token: str) -> str:
    """sha256 hex of a device token.

    A fast hash is the right call here, unlike for the web password: the token
    is machine-generated with ~144+ bits of entropy, so there is no dictionary
    to slow down. argon2 would buy nothing and would burn the single vCPU on
    every OPDS request (KOReader sends Basic preemptively on every request).
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def digests_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


# --- sessions -------------------------------------------------------------


def new_session_id() -> str:
    """160 bits of randomness, urlsafe-base64. Comfortably over the 128-bit
    floor in the design."""
    return secrets.token_urlsafe(20)


def session_id_digest(session_id: str) -> str:
    """Only the digest is stored. A dump of the sessions table therefore does
    not yield usable cookies."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()
