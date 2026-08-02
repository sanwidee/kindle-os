"""SQLite catalog store.

Deliberately no ORM. Six tables, hand-written SQL, WAL mode. One connection
per thread (Flask serves this on a gthread worker, plus the builder thread),
created lazily and kept for the life of the thread.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

_local = threading.local()


def utcnow() -> str:
    """ISO8601 with a literal Z. Every timestamp in the DB uses this shape so
    string comparison equals chronological comparison."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


@dataclass
class Document:
    doc_id: str
    uuid: str
    relpath: str
    collection: str
    title: str
    slug: str
    summary: str
    author: str
    issued: str | None
    language: str
    source_sha: str
    build_sha: str
    epub_name: str
    epub_bytes: int
    created: str
    updated: str
    tags: list[str] = field(default_factory=list)

    @property
    def epub_href(self) -> str:
        """Path component of the acquisition URL.

        Constraints, all traceable to verified KOReader behaviour:
        same origin as the feed, ends in a real .epub extension, no query
        string (getFiletype checks the filename suffix first).
        """
        return f"/d/{self.doc_id}/{self.epub_name}"

    @property
    def reader_href(self) -> str:
        return f"/d/{self.doc_id}/"

    @property
    def epub_relpath(self) -> str:
        """Path relative to the library root -- what X-Accel-Redirect appends
        to the internal nginx location."""
        return f"{self.doc_id}/{self.epub_name}"


def _row_to_document(row: sqlite3.Row, tags: list[str] | None = None) -> Document:
    return Document(
        doc_id=row["doc_id"],
        uuid=row["uuid"],
        relpath=row["relpath"],
        collection=row["collection"],
        title=row["title"],
        slug=row["slug"],
        summary=row["summary"],
        author=row["author"],
        issued=row["issued"],
        language=row["language"],
        source_sha=row["source_sha"],
        build_sha=row["build_sha"],
        epub_name=row["epub_name"],
        epub_bytes=row["epub_bytes"],
        created=row["created"],
        updated=row["updated"],
        tags=tags or [],
    )


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    # -- connection ------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        existing = getattr(_local, "conn", None)
        if existing is not None and getattr(_local, "path", None) == str(self.db_path):
            return existing
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        _local.conn = conn
        _local.path = str(self.db_path)
        return conn

    def migrate(self) -> None:
        sql = resources.files("kindle_hub.catalog").joinpath("schema.sql").read_text("utf-8")
        self.conn.executescript(sql)
        self.conn.commit()

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    # -- documents -------------------------------------------------------

    def get_source_shas(self) -> dict[str, str]:
        """relpath -> source_sha for the whole library.

        Kept for callers that genuinely want "did the markdown change". The
        builder does NOT use this to decide what to rebuild -- see
        get_build_shas below for why.
        """
        rows = self._execute("SELECT relpath, source_sha FROM documents").fetchall()
        return {r["relpath"]: r["source_sha"] for r in rows}

    def get_build_shas(self) -> dict[str, str]:
        """relpath -> build_sha for the whole library.

        build_sha covers the markdown AND every renderer knob (column
        budgets, e-ink width, HUB_RENDERER_VERSION), so comparing it is what
        makes a converter change actually reach documents already on disk.

        BUG FIXED HERE: the builder used to compare source_sha instead. The
        markdown had not changed, so every document was skipped and the
        library silently stayed on the old renderer -- the exact
        "mixed-vintage library" that compute_build_sha's docstring claims
        bumping HUB_RENDERER_VERSION prevents. Adding covers is what surfaced
        it: nothing rebuilt, and every cover was missing with no error
        anywhere. Any earlier renderer change had the same silent outcome.
        """
        rows = self._execute("SELECT relpath, build_sha FROM documents").fetchall()
        return {r["relpath"]: r["build_sha"] for r in rows}

    def upsert_document(self, doc: Document, html_wide: str) -> None:
        now = utcnow()
        existing = self._execute(
            "SELECT created FROM documents WHERE relpath = ?", (doc.relpath,)
        ).fetchone()
        created = existing["created"] if existing else now
        self._execute(
            """
            INSERT INTO documents (doc_id, uuid, relpath, collection, title, slug,
                                   summary, author, issued, language, source_sha,
                                   build_sha, epub_name, epub_bytes, html_wide,
                                   created, updated)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(relpath) DO UPDATE SET
                doc_id=excluded.doc_id, uuid=excluded.uuid,
                collection=excluded.collection, title=excluded.title,
                slug=excluded.slug, summary=excluded.summary,
                author=excluded.author, issued=excluded.issued,
                language=excluded.language, source_sha=excluded.source_sha,
                build_sha=excluded.build_sha, epub_name=excluded.epub_name,
                epub_bytes=excluded.epub_bytes, html_wide=excluded.html_wide,
                updated=excluded.updated
            """,
            (
                doc.doc_id, doc.uuid, doc.relpath, doc.collection, doc.title, doc.slug,
                doc.summary, doc.author, doc.issued, doc.language, doc.source_sha,
                doc.build_sha, doc.epub_name, doc.epub_bytes, html_wide, created, now,
            ),
        )
        self._execute("DELETE FROM document_tags WHERE doc_id = ?", (doc.doc_id,))
        self.conn.executemany(
            "INSERT OR IGNORE INTO document_tags (doc_id, tag) VALUES (?, ?)",
            [(doc.doc_id, tag) for tag in doc.tags],
        )
        self._execute("DELETE FROM gc_queue WHERE relpath = ?", (doc.relpath,))
        self.conn.commit()

    def delete_document(self, relpath: str) -> str | None:
        """Drop the row so the document leaves every feed immediately, and
        queue its EPUB directory for deletion after the grace period."""
        row = self._execute(
            "SELECT doc_id FROM documents WHERE relpath = ?", (relpath,)
        ).fetchone()
        if row is None:
            return None
        doc_id = row["doc_id"]
        self._execute("DELETE FROM documents WHERE relpath = ?", (relpath,))
        self._execute(
            "INSERT OR REPLACE INTO gc_queue (relpath, doc_id, deleted_at) VALUES (?,?,?)",
            (relpath, doc_id, utcnow()),
        )
        self.conn.commit()
        return doc_id

    def due_for_gc(self, grace_days: int) -> list[str]:
        cutoff = (datetime.now(UTC) - timedelta(days=grace_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        rows = self._execute(
            "SELECT relpath, doc_id FROM gc_queue WHERE deleted_at < ?", (cutoff,)
        ).fetchall()
        return [(r["relpath"], r["doc_id"]) for r in rows]  # type: ignore[return-value]

    def forget_gc(self, relpath: str) -> None:
        self._execute("DELETE FROM gc_queue WHERE relpath = ?", (relpath,))
        self.conn.commit()

    def get_document(self, doc_id: str) -> Document | None:
        row = self._execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
        if row is None:
            return None
        return _row_to_document(row, self._tags_for(doc_id))

    def get_document_html(self, doc_id: str) -> str | None:
        row = self._execute(
            "SELECT html_wide FROM documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        return row["html_wide"] if row else None

    def _tags_for(self, doc_id: str) -> list[str]:
        rows = self._execute(
            "SELECT tag FROM document_tags WHERE doc_id = ? ORDER BY tag", (doc_id,)
        ).fetchall()
        return [r["tag"] for r in rows]

    def _hydrate(self, rows: Iterable[sqlite3.Row]) -> list[Document]:
        rows = list(rows)
        if not rows:
            return []
        ids = [r["doc_id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        tag_rows = self._execute(
            "SELECT doc_id, tag FROM document_tags "
            f"WHERE doc_id IN ({placeholders}) ORDER BY tag",
            ids,
        ).fetchall()
        by_id: dict[str, list[str]] = {i: [] for i in ids}
        for tr in tag_rows:
            by_id[tr["doc_id"]].append(tr["tag"])
        return [_row_to_document(r, by_id[r["doc_id"]]) for r in rows]

    # -- shelves ---------------------------------------------------------

    _SHELF_SQL = {
        "new": ("SELECT * FROM documents ORDER BY updated DESC, title ASC", ()),
        "all": ("SELECT * FROM documents ORDER BY title COLLATE NOCASE ASC", ()),
    }

    def shelf(
        self, kind: str, key: str | None = None, limit: int | None = None, offset: int = 0
    ) -> tuple[list[Document], int]:
        """Return (page of documents, total count) for a shelf."""
        if kind in self._SHELF_SQL:
            sql, params = self._SHELF_SQL[kind]
            count_sql = "SELECT COUNT(*) AS n FROM documents"
            count_params: Sequence[Any] = ()
        elif kind == "collection":
            sql = ("SELECT * FROM documents WHERE collection = ? "
                   "ORDER BY updated DESC, title ASC")
            params = (key,)
            count_sql = "SELECT COUNT(*) AS n FROM documents WHERE collection = ?"
            count_params = (key,)
        elif kind == "tag":
            sql = ("SELECT d.* FROM documents d JOIN document_tags t ON t.doc_id = d.doc_id "
                   "WHERE t.tag = ? ORDER BY d.updated DESC, d.title ASC")
            params = (key,)
            count_sql = ("SELECT COUNT(*) AS n FROM documents d "
                         "JOIN document_tags t ON t.doc_id = d.doc_id WHERE t.tag = ?")
            count_params = (key,)
        elif kind == "search":
            # LIKE is fine at this scale (hundreds of documents, one reader).
            # FTS5 would be the move somewhere north of ~10k documents.
            needle = f"%{(key or '').strip()}%"
            sql = ("SELECT * FROM documents WHERE title LIKE ? OR summary LIKE ? "
                   "ORDER BY updated DESC, title ASC")
            params = (needle, needle)
            count_sql = ("SELECT COUNT(*) AS n FROM documents "
                         "WHERE title LIKE ? OR summary LIKE ?")
            count_params = (needle, needle)
        else:
            raise ValueError(f"unknown shelf kind {kind!r}")

        total = self._execute(count_sql, count_params).fetchone()["n"]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params = (*params, limit, offset)
        return self._hydrate(self._execute(sql, params).fetchall()), total

    def shelf_last_modified(self, kind: str, key: str | None = None) -> str:
        """MAX(updated) over the shelf.

        This is the single highest-leverage server-side value in the whole
        OPDS design: KOReader keys its in-memory feed cache on the
        Last-Modified it gets back from a preflight HEAD.
        """
        if kind in ("new", "all", "search"):
            row = self._execute("SELECT MAX(updated) AS m FROM documents").fetchone()
        elif kind == "collection":
            row = self._execute(
                "SELECT MAX(updated) AS m FROM documents WHERE collection = ?", (key,)
            ).fetchone()
        elif kind == "tag":
            row = self._execute(
                "SELECT MAX(d.updated) AS m FROM documents d "
                "JOIN document_tags t ON t.doc_id = d.doc_id WHERE t.tag = ?",
                (key,),
            ).fetchone()
        else:
            raise ValueError(f"unknown shelf kind {kind!r}")
        return row["m"] or "1970-01-01T00:00:00Z"

    def library_last_modified(self) -> str:
        row = self._execute("SELECT MAX(updated) AS m FROM documents").fetchone()
        return row["m"] or "1970-01-01T00:00:00Z"

    def collections(self) -> list[tuple[str, int, str]]:
        rows = self._execute(
            "SELECT collection, COUNT(*) AS n, MAX(updated) AS m FROM documents "
            "GROUP BY collection ORDER BY collection"
        ).fetchall()
        return [(r["collection"], r["n"], r["m"]) for r in rows]

    def tags(self) -> list[tuple[str, int, str]]:
        rows = self._execute(
            "SELECT t.tag AS tag, COUNT(*) AS n, MAX(d.updated) AS m "
            "FROM document_tags t JOIN documents d ON d.doc_id = t.doc_id "
            "GROUP BY t.tag ORDER BY t.tag"
        ).fetchall()
        return [(r["tag"], r["n"], r["m"]) for r in rows]

    def count(self) -> int:
        return self._execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]

    # -- sessions --------------------------------------------------------

    def create_session(self, id_hash: str, user_agent: str, pw_epoch: str = "") -> None:
        now = utcnow()
        self._execute(
            "INSERT OR REPLACE INTO sessions "
            "(id_hash, created_at, last_seen, user_agent, pw_epoch) VALUES (?,?,?,?,?)",
            (id_hash, now, now, user_agent[:200], pw_epoch),
        )
        self.conn.commit()

    def touch_session(
        self,
        id_hash: str,
        idle_days: int,
        absolute_days: int,
        pw_epoch: str | None = None,
    ) -> bool:
        """Validate and slide a session. Returns False if it is unknown, has
        aged out under either the idle timeout or the absolute cap, or was
        minted under a different web password.

        The pw_epoch check is what makes password rotation actually revoke
        access rather than merely change what the next login needs to type.
        """
        row = self._execute(
            "SELECT created_at, last_seen, pw_epoch FROM sessions WHERE id_hash = ?",
            (id_hash,),
        ).fetchone()
        if row is None:
            return False
        if pw_epoch is not None and (row["pw_epoch"] or "") != pw_epoch:
            self.drop_session(id_hash)
            return False
        now = datetime.now(UTC)
        if now - parse_ts(row["last_seen"]) > timedelta(days=idle_days):
            self.drop_session(id_hash)
            return False
        if now - parse_ts(row["created_at"]) > timedelta(days=absolute_days):
            self.drop_session(id_hash)
            return False
        # Only write once a day; a write per request would be pointless churn
        # on the WAL for a sliding window measured in weeks.
        if now - parse_ts(row["last_seen"]) > timedelta(hours=24):
            self._execute(
                "UPDATE sessions SET last_seen = ? WHERE id_hash = ?", (utcnow(), id_hash)
            )
            self.conn.commit()
        return True

    def drop_session(self, id_hash: str) -> None:
        self._execute("DELETE FROM sessions WHERE id_hash = ?", (id_hash,))
        self.conn.commit()

    def drop_all_sessions(self) -> int:
        cur = self._execute("DELETE FROM sessions")
        self.conn.commit()
        return cur.rowcount

    def session_count(self) -> int:
        return self._execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]

    # -- device tokens ---------------------------------------------------

    def is_token_revoked(self, name: str, token_sha256: str) -> bool:
        """True if this exact (name, digest) pair has been revoked.

        Checked before every seed so that a revoked token cannot be brought
        back by the env, and before every auth so a race cannot slip through.
        """
        row = self._execute(
            "SELECT 1 FROM revoked_device_tokens WHERE name = ? AND token_sha256 = ?",
            (name, token_sha256),
        ).fetchone()
        return row is not None

    def upsert_device_token(self, name: str, token_sha256: str) -> bool:
        """Register a device token. Returns False if it is tombstoned.

        Refusing here is what makes revocation durable: seeding runs on every
        startup from secrets.env, and without this check a revoked digest
        reappears the moment the container restarts.
        """
        if self.is_token_revoked(name, token_sha256):
            return False
        self._execute(
            "INSERT INTO device_tokens (name, token_sha256, created_at) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET token_sha256 = excluded.token_sha256",
            (name, token_sha256, utcnow()),
        )
        self.conn.commit()
        return True

    def revoke_device_token(self, name: str, reason: str | None = None) -> bool:
        """Tombstone the token, then delete the live row. Order matters: if we
        crash between the two, the tombstone already blocks re-seeding."""
        row = self._execute(
            "SELECT token_sha256 FROM device_tokens WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return False
        self._execute(
            "INSERT OR IGNORE INTO revoked_device_tokens "
            "(name, token_sha256, revoked_at, reason) VALUES (?,?,?,?)",
            (name, row["token_sha256"], utcnow(), reason),
        )
        self._execute("DELETE FROM device_tokens WHERE name = ?", (name,))
        self.conn.commit()
        return True

    def unrevoke_device_name(self, name: str) -> int:
        """Clear tombstones for a name so it can be reused. Deliberately a
        separate, explicit action -- never part of seeding or startup."""
        cur = self._execute(
            "DELETE FROM revoked_device_tokens WHERE name = ?", (name,)
        )
        self.conn.commit()
        return cur.rowcount

    def revoked_device_tokens(self) -> list[sqlite3.Row]:
        return self._execute(
            "SELECT name, revoked_at, reason FROM revoked_device_tokens "
            "ORDER BY revoked_at DESC"
        ).fetchall()

    def device_token_digest(self, name: str) -> str | None:
        row = self._execute(
            "SELECT token_sha256 FROM device_tokens WHERE name = ?", (name,)
        ).fetchone()
        return row["token_sha256"] if row else None

    def note_device_use(
        self, name: str, ip: str, path: str | None = None, outcome: str = "ok"
    ) -> None:
        """Per-token access logging. The auth design leans on this: MITM and
        device theft cannot be prevented, so anomalous use has to be visible.

        Writes an append-only row as well as the rolling summary columns. The
        summary alone was one entry deep, so a single legitimate request after
        an intrusion erased the evidence of it.
        """
        now = utcnow()
        self._execute(
            "INSERT INTO device_access (name, at, ip, path, outcome) VALUES (?,?,?,?,?)",
            (name, now, ip[:64], (path or "")[:200], outcome),
        )
        if outcome == "ok":
            self._execute(
                "UPDATE device_tokens SET last_seen = ?, last_ip = ?, hits = hits + 1 "
                "WHERE name = ?",
                (now, ip[:64], name),
            )
        self.conn.commit()

    def device_access_log(self, name: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        if name:
            return self._execute(
                "SELECT name, at, ip, path, outcome FROM device_access "
                "WHERE name = ? ORDER BY at DESC LIMIT ?",
                (name, limit),
            ).fetchall()
        return self._execute(
            "SELECT name, at, ip, path, outcome FROM device_access "
            "ORDER BY at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def prune_device_access(self, keep: int = 5000) -> int:
        """Keep the log bounded. Unbounded append on a 40 GB box with SQLite is
        fine for a long time, but 'a long time' is not a retention policy."""
        cur = self._execute(
            "DELETE FROM device_access WHERE id NOT IN "
            "(SELECT id FROM device_access ORDER BY id DESC LIMIT ?)",
            (keep,),
        )
        self.conn.commit()
        return cur.rowcount

    def device_tokens(self) -> list[sqlite3.Row]:
        return self._execute(
            "SELECT name, created_at, last_seen, last_ip, hits FROM device_tokens ORDER BY name"
        ).fetchall()

    # -- meta ------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        self._execute(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
