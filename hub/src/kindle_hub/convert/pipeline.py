"""Orchestrates one document build: markdown in, EPUB + cached HTML out."""

from __future__ import annotations

import hashlib
import logging
import shutil
import time
import uuid
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from ..catalog.store import Document, utcnow
from ..config import Config
from ..ingest import frontmatter, scanner
from . import cover, epub, images, mdrender, textclean
from .mdrender import ENV_CFG, ENV_IMAGE_RESOLVER, ResolvedImage

log = logging.getLogger("kindle_hub.pipeline")

# Fixed namespace so document identity survives a rebuild, a container
# recreate, and a move to another box. Do not regenerate this.
DOC_NAMESPACE = uuid.UUID("6f1a8c1e-9a2c-5f4b-8d3e-2b7c4a10d9f2")


@dataclass
class BuildResult:
    document: Document
    html_wide: str
    rebuilt: bool


def _load_css(name: str) -> str:
    return resources.files("kindle_hub.assets").joinpath(name).read_text("utf-8")


def compute_build_sha(source_bytes: bytes, cfg: Config) -> str:
    """Covers the markdown plus every knob that changes the output, so
    bumping HUB_RENDERER_VERSION (or a column budget) rebuilds everything on
    purpose rather than leaving a mixed-vintage library."""
    h = hashlib.sha256()
    h.update(source_bytes)
    h.update(b"\x00")
    h.update(
        "|".join(
            str(x)
            for x in (
                cfg.renderer_version,
                cfg.code_columns,
                cfg.code_max_lines,
                cfg.table_columns,
                cfg.eink_max_width,
                cfg.strip_emoji,
            )
        ).encode("utf-8")
    )
    return h.hexdigest()


def document_identity(relpath: str) -> tuple[str, str]:
    """(full uuid string, 12-hex public doc_id).

    12 hex characters is 48 bits. That is not access control -- auth is
    enforced on every request regardless -- but it is enough to make the
    library non-enumerable, and `documents.doc_id` is a PRIMARY KEY so a
    collision surfaces as an integrity error rather than silently
    overwriting someone's document.
    """
    doc_uuid = uuid.uuid5(DOC_NAMESPACE, relpath)
    return str(doc_uuid), doc_uuid.hex[:12]


class _ImageCollector:
    """Resolves markdown image references once, caching by source path so a
    logo referenced twenty times is processed once."""

    def __init__(self, cfg: Config, source_dir: Path):
        self.cfg = cfg
        self.source_dir = source_dir
        self.processed: dict[str, images.ProcessedImage] = {}
        self.failures: list[str] = []

    def _get(self, src: str) -> images.ProcessedImage | None:
        if src in self.processed:
            return self.processed[src]
        if images.is_remote(src):
            # Never fetch. An http(s) reference renders as a labelled
            # placeholder with the URL as visible text.
            self.failures.append(src)
            return None
        path = images.resolve_local(src, self.source_dir, self.cfg.inbox_dir)
        if path is None:
            self.failures.append(src)
            return None
        if path.suffix.lower() == ".svg":
            # UNVERIFIED: crengine's SVG support was never investigated.
            # Rasterizing would mean cairosvg, a large dependency for a rare
            # case, so Phase 1 emits a placeholder instead.
            self.failures.append(src)
            return None
        result = images.process(path, self.cfg.eink_max_width)
        if result is None:
            self.failures.append(src)
            return None
        self.processed[src] = result
        return result

    def epub_resolver(self, src: str) -> ResolvedImage | None:
        result = self._get(src)
        if result is None:
            return None
        # XHTML lives at EPUB/text/, images at EPUB/images/.
        return ResolvedImage(f"../images/{result.name}", result.width, result.height)

    def web_resolver(self, doc_id: str):
        def resolve(src: str) -> ResolvedImage | None:
            result = self._get(src)
            if result is None:
                return None
            return ResolvedImage(
                f"/d/{doc_id}/media/{result.name}", result.width, result.height
            )

        return resolve


def build(cfg: Config, source: scanner.SourceFile, md=None) -> BuildResult:
    """Convert one markdown file. Writes the EPUB and any media, and returns
    the catalog row to upsert."""
    md = md or mdrender.build_parser()
    started = time.monotonic()

    raw_bytes = source.path.read_bytes()
    text = raw_bytes.decode("utf-8", errors="replace")
    fm, body = frontmatter.split(text, source.relpath)

    doc_uuid, doc_id = document_identity(source.relpath)
    build_sha = compute_build_sha(raw_bytes, cfg)
    slug = frontmatter.slugify(fm.title, fallback=Path(source.relpath).stem)

    tokens = md.parse(body)
    collector = _ImageCollector(cfg, source.path.parent)

    epub_env = {ENV_CFG: cfg, ENV_IMAGE_RESOLVER: collector.epub_resolver}
    sections = mdrender.split_sections(md, tokens, epub_env, default_title=fm.title)

    web_env = {ENV_CFG: cfg, ENV_IMAGE_RESOLVER: collector.web_resolver(doc_id)}
    html_wide = mdrender.render_wide(md, tokens, web_env)

    epub_sections = [
        epub.EpubSection(
            title=s.title,
            html=textclean.xhtml_safe(s.html, strip_emoji_flag=cfg.strip_emoji),
            anchor=s.anchor,
        )
        for s in sections
    ]

    epub_name = (
        f"{slug}.epub" if cfg.stable_filenames else f"{slug}.{build_sha[:8]}.epub"
    )
    doc_dir = cfg.library_dir / doc_id
    epub_path = doc_dir / epub_name

    # Generated cover. A shelf of thirty identical grey placeholders is hard
    # to navigate in KOReader's thumbnail grid; a cover per document fixes
    # that for the cost of ~30 KB and a few milliseconds.
    #
    # Failure here must never lose a document: a cover is a nicety and the
    # text is the point. If Pillow is missing, fonts are absent, or anything
    # else goes wrong, log it and build the EPUB without one.
    cover_img = None
    try:
        cover_img = epub.EpubImage(
            "cover.png",
            cover.render_cover(
                fm.title,
                doc_id=doc_id,
                collection=fm.collection or "",
                date=(fm.issued or "")[:10],
            ),
        )
    except Exception as exc:  # noqa: BLE001 -- cosmetic, never fatal
        log.warning("cover generation failed for %s: %s", source.relpath, exc)

    spec = epub.EpubSpec(
        identifier=f"urn:uuid:{doc_uuid}",
        title=fm.title,
        language=fm.language,
        author=fm.author,
        modified=utcnow(),
        css=_load_css("epub-eink.css"),
        sections=epub_sections,
        images=[epub.EpubImage(p.name, p.data) for p in collector.processed.values()],
        description=fm.summary,
        cover=cover_img,
    )
    epub_bytes = epub.write_epub(spec, epub_path)

    # Media for the web reader lives next to the EPUB and is served through
    # the same X-Accel-Redirect path, so it inherits the same auth check.
    media_dir = doc_dir / "media"
    if collector.processed:
        media_dir.mkdir(parents=True, exist_ok=True)
        for processed in collector.processed.values():
            target = media_dir / processed.name
            if not target.exists():
                target.write_bytes(processed.data)

    _prune_old_epubs(doc_dir, keep=epub_name, grace_days=cfg.gc_grace_days)

    if collector.failures:
        log.info(
            "%s: %d image reference(s) rendered as placeholders",
            source.relpath, len(collector.failures),
        )
    log.info(
        "built %s (%d sections, %d bytes) in %.0f ms",
        source.relpath, len(epub_sections), epub_bytes,
        (time.monotonic() - started) * 1000,
    )

    document = Document(
        doc_id=doc_id,
        uuid=doc_uuid,
        relpath=source.relpath,
        collection=fm.collection,
        title=fm.title,
        slug=slug,
        summary=fm.summary,
        author=fm.author,
        issued=fm.issued,
        language=fm.language,
        source_sha=source.source_sha,
        build_sha=build_sha,
        epub_name=epub_name,
        epub_bytes=epub_bytes,
        created=utcnow(),
        updated=utcnow(),
        tags=fm.tags,
    )
    return BuildResult(document=document, html_wide=html_wide, rebuilt=True)


def _prune_old_epubs(doc_dir: Path, keep: str, grace_days: int) -> None:
    """A revision arrives as a new filename. Old builds hang around for the
    grace period so a download already in flight cannot 404, then go."""
    if not doc_dir.is_dir():
        return
    cutoff = time.time() - grace_days * 86400
    for entry in doc_dir.glob("*.epub"):
        if entry.name == keep:
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            pass


def remove_document_files(cfg: Config, doc_id: str) -> None:
    shutil.rmtree(cfg.library_dir / doc_id, ignore_errors=True)
