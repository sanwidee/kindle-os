"""Background build loop.

One flock-guarded thread sweeps the inbox every HUB_BUILD_INTERVAL seconds.
The lock means exactly one process owns the loop even if the worker count is
ever raised, and it means `kindle-hub build` run by hand while the service is
up simply declines rather than racing it.

Unchanged files cost one stat plus one read and hash. On the target hardware
with a few hundred documents that is a few tens of milliseconds, which is why
there is no inotify dependency here.
"""

from __future__ import annotations

import fcntl
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .catalog.store import Store
from .config import Config
from .convert import mdrender, pipeline
from .ingest import scanner

log = logging.getLogger("kindle_hub.builder")


@dataclass
class SweepResult:
    scanned: int = 0
    built: int = 0
    deleted: int = 0
    errors: int = 0
    seconds: float = 0.0


class BuildLock:
    """Advisory flock on a file in the state dir. Non-blocking."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            return False
        return True

    def release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


def sweep(cfg: Config, store: Store, force: bool = False) -> SweepResult:
    """One pass over the inbox. Safe to call directly (CLI) or from the loop."""
    started = time.monotonic()
    result = SweepResult()
    md = mdrender.build_parser()

    known = store.get_source_shas()
    found = scanner.scan(cfg.inbox_dir)
    result.scanned = len(found)
    seen: set[str] = set()

    for source in found:
        seen.add(source.relpath)
        if not force and known.get(source.relpath) == source.source_sha:
            continue
        try:
            built = pipeline.build(cfg, source, md=md)
            store.upsert_document(built.document, built.html_wide)
            result.built += 1
        except Exception:
            result.errors += 1
            log.exception("build failed for %s", source.relpath)

    for relpath in set(known) - seen:
        doc_id = store.delete_document(relpath)
        if doc_id:
            result.deleted += 1
            log.info("removed %s from the catalog (files kept for the grace period)",
                     relpath)

    for relpath, doc_id in store.due_for_gc(cfg.gc_grace_days):
        pipeline.remove_document_files(cfg, doc_id)
        store.forget_gc(relpath)

    result.seconds = time.monotonic() - started
    if result.built or result.deleted or result.errors:
        log.info(
            "sweep: %d scanned, %d built, %d deleted, %d errors, %.2fs",
            result.scanned, result.built, result.deleted, result.errors, result.seconds,
        )
    store.set_meta("last_sweep", str(int(time.time())))
    return result


class Builder:
    """Owns the background thread. Started by the app factory unless the
    process is running a one-shot CLI command."""

    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self.lock = BuildLock(cfg.lock_path)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.owns_lock = False
        self.last_result: SweepResult | None = None

    def start(self) -> None:
        if not self.lock.acquire():
            log.info("another process owns the build lock; not starting the sweep loop")
            return
        self.owns_lock = True
        self._thread = threading.Thread(target=self._run, name="builder", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self.lock.release()
        self.owns_lock = False

    def request_rebuild(self) -> None:
        """Poked by POST /admin/rebuild. Wakes the loop rather than building
        inline, so an admin click cannot tie up the request thread."""
        self._force_next = True
        self._wake.set()

    _force_next = False

    def _run(self) -> None:
        if self.cfg.build_on_start:
            self._safe_sweep(force=False)
        while not self._stop.is_set():
            self._wake.wait(timeout=self.cfg.build_interval_seconds)
            self._wake.clear()
            if self._stop.is_set():
                break
            force, self._force_next = self._force_next, False
            self._safe_sweep(force=force)

    def _safe_sweep(self, force: bool) -> None:
        try:
            self.last_result = sweep(self.cfg, self.store, force=force)
        except Exception:
            log.exception("sweep raised; the loop continues")
