-- kindle-os hub catalog. Applied idempotently at startup.
-- Six tables, hand-written SQL, no ORM.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS documents (
    doc_id      TEXT PRIMARY KEY,          -- first 12 hex of uuid, public URL component
    uuid        TEXT NOT NULL UNIQUE,      -- uuid5(NS, relpath), stable across rebuilds
    relpath     TEXT NOT NULL UNIQUE,      -- path under the inbox, e.g. notes/foo.md
    collection  TEXT NOT NULL,             -- top-level inbox directory
    title       TEXT NOT NULL,
    slug        TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    author      TEXT NOT NULL DEFAULT '',
    issued      TEXT,                      -- YYYY-MM-DD from front matter, or NULL
    language    TEXT NOT NULL DEFAULT 'en',
    source_sha  TEXT NOT NULL,             -- sha256 of the markdown bytes
    build_sha   TEXT NOT NULL,             -- sha256(source bytes + renderer config)
    epub_name   TEXT NOT NULL,             -- <slug>.<build_sha8>.epub
    epub_bytes  INTEGER NOT NULL,
    html_wide   TEXT NOT NULL,             -- cached wide render for the web reader
    created     TEXT NOT NULL,             -- ISO8601 Z
    updated     TEXT NOT NULL              -- ISO8601 Z, drives Last-Modified
);

CREATE INDEX IF NOT EXISTS idx_documents_updated ON documents(updated DESC);
CREATE INDEX IF NOT EXISTS idx_documents_collection ON documents(collection, title);

CREATE TABLE IF NOT EXISTS document_tags (
    doc_id  TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    tag     TEXT NOT NULL,
    PRIMARY KEY (doc_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_document_tags_tag ON document_tags(tag);

-- Web door. Stores sha256 of the session id, never the id itself, so a
-- read of this table does not yield usable cookies.
-- pw_epoch binds a session to the password that minted it.
--
-- SECURITY FIX (review: crypto-and-session, finding 3). Sessions used to be
-- keyed on id_hash alone and validated on age only, so rotating a leaked
-- passphrase left every existing cookie valid for the full 90-day absolute
-- cap. Rotation therefore revoked nothing -- which is the one thing anyone
-- rotates a password to achieve. Now a changed HUB_WEB_PASSWORD_HASH changes
-- the epoch, and every session minted under the old one fails validation on
-- its next request.
CREATE TABLE IF NOT EXISTS sessions (
    id_hash     TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    user_agent  TEXT NOT NULL DEFAULT '',
    pw_epoch    TEXT NOT NULL DEFAULT ''
);

-- Kindle door. One row per device. Stores sha256 of a ~144-bit random token;
-- a fast hash is correct here (see docs/auth design) because the token has
-- far more entropy than any password.
CREATE TABLE IF NOT EXISTS device_tokens (
    name          TEXT PRIMARY KEY,        -- Basic-auth username, e.g. kindle-pw4
    token_sha256  TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_seen     TEXT,
    last_ip       TEXT,
    hits          INTEGER NOT NULL DEFAULT 0
);

-- Tombstones for revoked device tokens.
--
-- SECURITY FIX (review: credential-exposure, finding 1). Revocation used to be
-- a plain DELETE from device_tokens, but HUB_OPDS_TOKENS in secrets.env is
-- re-seeded into that table on every create_app() and every `build`. So a
-- token revoked because the Kindle was lost came back to life -- silently --
-- at the next restart, OOM-restart, or config edit. Worse, the documented
-- procedure for rotating the web password is "edit secrets.env and restart",
-- which is exactly the action that resurrected it.
--
-- A tombstone is checked before seeding, so revocation outlives the env.
-- Reusing a revoked NAME requires clearing the tombstone deliberately
-- (`token unrevoke`), which is the point: it cannot happen by accident.
CREATE TABLE IF NOT EXISTS revoked_device_tokens (
    name          TEXT NOT NULL,
    token_sha256  TEXT NOT NULL,
    revoked_at    TEXT NOT NULL,
    reason        TEXT,
    PRIMARY KEY (name, token_sha256)
);

-- Append-only access log for device tokens.
--
-- SECURITY FIX (review: credential-exposure, finding 2). device_tokens.last_ip
-- is a single overwritten column, so the next legitimate catalog open erased
-- any trace of an intruder -- one entry deep. The design leans on this log as
-- the sole compensating control for MITM capture and USB token theft, so it
-- has to actually retain history.
CREATE TABLE IF NOT EXISTS device_access (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL,
    at        TEXT NOT NULL,
    ip        TEXT NOT NULL,
    path      TEXT,
    outcome   TEXT NOT NULL DEFAULT 'ok'    -- ok | denied
);

CREATE INDEX IF NOT EXISTS idx_device_access_name_at
    ON device_access (name, at DESC);

-- Deleted documents keep their EPUB on disk for a grace period so a download
-- already in flight cannot 404 mid-transfer.
CREATE TABLE IF NOT EXISTS gc_queue (
    relpath     TEXT PRIMARY KEY,
    doc_id      TEXT NOT NULL,
    deleted_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
