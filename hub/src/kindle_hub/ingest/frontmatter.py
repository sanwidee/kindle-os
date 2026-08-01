"""YAML front matter split, with sane defaults for everything.

Deliberately splits the front matter off the source text *before* markdown-it
sees it, rather than using the mdit_py_plugins front_matter plugin: we need
the body as a separate string anyway (to hash it, and to render it twice),
and doing the split here keeps the parser configuration free of a plugin
whose only job would be to produce a token we immediately discard.

Every field is optional. A document with no front matter at all still gets a
title (from the first H1, or the filename) so nothing is ever unreachable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import yaml

_FM_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})[-_]")


@dataclass
class FrontMatter:
    title: str
    summary: str = ""
    author: str = "Claude Code"
    language: str = "en"
    issued: str | None = None
    collection: str = "misc"
    tags: list[str] = field(default_factory=list)


def slugify(text: str, fallback: str = "document") -> str:
    slug = _SLUG_STRIP.sub("-", text.lower()).strip("-")
    slug = slug[:60].strip("-")
    return slug or fallback


def _as_tag_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(";", ",").split(",")]
    elif isinstance(value, (list, tuple)):
        parts = [str(p).strip() for p in value]
    else:
        parts = [str(value).strip()]
    seen: list[str] = []
    for part in parts:
        tag = slugify(part, fallback="")
        if tag and tag not in seen:
            seen.append(tag)
    return seen


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    return str(value)


def split(source: str, relpath: str) -> tuple[FrontMatter, str]:
    """Return (front matter, markdown body).

    `relpath` is the inbox-relative path, used for the collection name and as
    the last-resort title.
    """
    match = _FM_RE.match(source)
    body = source
    raw: dict[str, object] = {}
    if match:
        body = source[match.end():]
        try:
            loaded = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            # Malformed front matter is not worth failing a build over. Treat
            # the document as if it had none; the title fallback still works.
            loaded = None
        if isinstance(loaded, dict):
            raw = loaded

    path = PurePosixPath(relpath)
    default_collection = path.parts[0] if len(path.parts) > 1 else "misc"

    title = _as_text(raw.get("title"))
    if not title:
        h1 = _H1_RE.search(body)
        title = h1.group(1).strip() if h1 else path.stem.replace("-", " ").replace("_", " ")

    issued = raw.get("date") or raw.get("issued")
    issued_str = str(issued)[:10] if issued else None
    if issued_str is None:
        # Claude Code sessions habitually name files 2026-08-01-thing.md.
        prefix = _DATE_PREFIX.match(path.stem)
        if prefix:
            issued_str = prefix.group(1)

    return (
        FrontMatter(
            title=title.strip()[:200],
            summary=_as_text(raw.get("summary") or raw.get("description"))[:500],
            author=_as_text(raw.get("author")) or "Claude Code",
            language=_as_text(raw.get("lang") or raw.get("language")) or "en",
            issued=issued_str,
            collection=slugify(_as_text(raw.get("collection")) or default_collection, "misc"),
            tags=_as_tag_list(raw.get("tags")),
        ),
        body,
    )
