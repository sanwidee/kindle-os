"""Inbox scanner.

The inbox is a plain directory contract: markdown files in subdirectories,
dotfiles ignored, no database coupling, no lock protocol. rsync writes to a
temp name starting with "." and renames on completion, so skipping dotfiles
is the entire partial-write defence.

Nothing here knows or cares what wrote the files. A Phase-2 HTTP ingest
endpoint would be a second writer against the same contract.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

MARKDOWN_SUFFIXES = {".md", ".markdown"}

# Refuse to build anything absurd. A 4 MB markdown file is a bug upstream,
# and converting it would tie up the single vCPU for no good reason.
MAX_SOURCE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class SourceFile:
    relpath: str  # posix, inbox-relative
    path: Path
    source_sha: str
    size: int


def _sha256_file(path: Path) -> tuple[str, bytes]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), data


def scan(inbox: Path) -> list[SourceFile]:
    """Walk the inbox and return every eligible markdown file with its hash.

    Uses os.scandir rather than an inotify dependency: at a few hundred
    documents this is tens of milliseconds, and a watchdog daemon is a
    permanent process on a 1 vCPU box to save a 60 second delay nobody feels.
    """
    found: list[SourceFile] = []
    if not inbox.is_dir():
        return found

    stack = [inbox]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except (PermissionError, FileNotFoundError):
            continue
        for entry in entries:
            if entry.name.startswith("."):
                continue  # rsync temp files, .DS_Store, ._ sidecars
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            path = Path(entry.path)
            if path.suffix.lower() not in MARKDOWN_SUFFIXES:
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            if size == 0 or size > MAX_SOURCE_BYTES:
                continue
            try:
                digest, _ = _sha256_file(path)
            except OSError:
                continue
            relpath = path.relative_to(inbox).as_posix()
            found.append(SourceFile(relpath=relpath, path=path, source_sha=digest, size=size))

    found.sort(key=lambda f: f.relpath)
    return found


def read_source(path: Path) -> str:
    """Read markdown as UTF-8, tolerating the odd stray byte rather than
    failing a whole build over one bad character."""
    return path.read_bytes().decode("utf-8", errors="replace")
