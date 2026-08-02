"""Markdown parsing and the two renderer profiles.

One parse, two renders. That asymmetry is most of the reason the hub has two
doors rather than one:

  narrow (EPUB)  -- code reflowed to a column budget, wide tables transposed
                    to records, images grayscaled and downscaled, emoji
                    stripped, XHTML-safe output.
  wide (browser) -- original code lines, real tables in a scroll container,
                    images as processed but full size in a scrollable page.

markdown-it-py is here for its token stream, not its speed: the same
`parse()` output feeds both renderers, which differ by a handful of
overridden rules. Parsing twice, or post-processing HTML with BeautifulSoup,
would both be worse.

Deliberate omissions: no pygments (see the e-ink note on `fence` below), no
linkify-it-py (table and strikethrough are enabled explicitly on the
commonmark preset rather than using the gfm-like preset, which drags linkify
in as a dependency).
"""

from __future__ import annotations

import html
from collections.abc import Callable
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdown_it.token import Token
from mdit_py_plugins.deflist import deflist_plugin
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.tasklists import tasklists_plugin

from . import tables
from .codeblocks import reflow

# env keys the renderers read
ENV_CFG = "hub_cfg"
ENV_IMAGE_RESOLVER = "hub_image_resolver"
# (kind, content) -> (ResolvedImage | None, caption). Set by the pipeline;
# absent in tests that render markdown without a figure backend, which is why
# every call site treats it as optional.
ENV_FIGURE = "hub_figure"

# An image resolver takes the raw markdown src and returns the href to use in
# the output, or None if the image could not be used at all.
ImageResolver = Callable[[str], "ResolvedImage | None"]


@dataclass(frozen=True)
class ResolvedImage:
    href: str
    width: int
    height: int


@dataclass
class Section:
    """One spine item in the EPUB. The spine is split on H1 so the table of
    contents is real and page-turn latency stays low on slow hardware."""

    title: str
    html: str
    anchor: str


def build_parser() -> MarkdownIt:
    md = (
        MarkdownIt("commonmark")
        .enable(["table", "strikethrough"])
        .use(footnote_plugin)
        .use(deflist_plugin)
        .use(tasklists_plugin, enabled=False)
    )
    # xhtmlOut keeps <br />, <hr /> and <img /> self-closed, which the EPUB
    # container requires. textclean.self_close_void_tags is the safety net for
    # anything the plugins emit raw.
    md.options["xhtmlOut"] = True
    md.options["breaks"] = False
    md.options["typographer"] = False
    return md


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


class _BaseRenderer(RendererHTML):
    """Shared image handling. Both profiles refuse remote images and both
    wrap images in a block container so crengine does not inline-squeeze
    them."""

    def image(self, tokens, idx, options, env):  # noqa: N802 (markdown-it naming)
        token = tokens[idx]
        src = token.attrGet("src") or ""
        alt = self.renderInlineAsText(token.children or [], options, env)
        resolver: ImageResolver | None = env.get(ENV_IMAGE_RESOLVER)
        resolved = resolver(src) if resolver else None
        if resolved is None:
            label = _escape(alt or "image")
            # Named HTML entities other than the five XML built-ins are NOT
            # valid in EPUB XHTML: &mdash; here would make the whole section
            # fail the wellformedness check and degrade to escaped text.
            return (
                f'<span class="placeholder">[image not available: {label} '
                f"- {_escape(src)}]</span>"
            )
        return (
            f'<img src="{_escape(resolved.href)}" alt="{_escape(alt)}" '
            f'width="{resolved.width}" height="{resolved.height}" />'
        )


# Fenced languages that are rendered as pictures rather than as listings.
FIGURE_LANGS = frozenset({"dot", "graph", "mindmap", "bars", "chart"})


def _figure_html(kind: str, content: str, env, caption_class: str = "figure") -> str | None:
    """Render a figure fence, or return None to fall through to a code block.

    Falling through matters: if graphviz is missing or the diagram source is
    malformed, the reader should still see the content as text rather than a
    hole in the page. A diagram is an upgrade, never a dependency.
    """
    make = env.get(ENV_FIGURE)
    if make is None:
        return None
    try:
        resolved, caption = make(kind, content)
    except Exception:  # noqa: BLE001 -- cosmetic; text fallback is fine
        return None
    if resolved is None:
        return None
    parts = [
        f'<div class="{caption_class}">',
        f'<img src="{_escape(resolved.href)}" alt="{_escape(caption or kind)}" '
        f'width="{resolved.width}" height="{resolved.height}" />',
    ]
    if caption:
        parts.append(f'<p class="figure-caption">{_escape(caption)}</p>')
    parts.append("</div>")
    return "".join(parts)


class NarrowRenderer(_BaseRenderer):
    """EPUB profile."""

    def fence(self, tokens, idx, options, env):  # noqa: N802
        lang = (tokens[idx].info or "").strip().split(" ")[0].lower()
        if lang in FIGURE_LANGS:
            html = _figure_html(lang, tokens[idx].content, env)
            if html is not None:
                return html
        return self._code(tokens[idx].content, tokens[idx].info, env)

    def code_block(self, tokens, idx, options, env):  # noqa: N802
        return self._code(tokens[idx].content, "", env)

    def _code(self, content: str, info: str, env) -> str:
        cfg = env[ENV_CFG]
        lang = (info or "").strip().split(" ")[0]
        result = reflow(content, cfg.code_columns, cfg.code_max_lines)

        # No syntax highlighting, and this is an improvement rather than a
        # concession: on a 16-level grayscale panel a syntax theme's colours
        # collapse into near-identical grays, contributing noise and no
        # information. Structure carries the meaning instead -- a left border,
        # monospace, and a small language label.
        parts = ['<div class="code">']
        if lang:
            parts.append(f'<p class="code-lang">{_escape(lang)}</p>')
        parts.append(f"<pre><code>{_escape(result.text)}</code></pre>")
        if result.truncated:
            parts.append(
                f'<p class="code-note">Truncated at {cfg.code_max_lines} lines '
                f"(of {result.original_lines}). Full listing in the web reader.</p>"
            )
        parts.append("</div>")
        return "".join(parts)


class WideRenderer(_BaseRenderer):
    """Browser profile: original source, real grids, horizontal scroll."""

    def fence(self, tokens, idx, options, env):  # noqa: N802
        lang = (tokens[idx].info or "").strip().split(" ")[0].lower()
        if lang in FIGURE_LANGS:
            html = _figure_html(lang, tokens[idx].content, env)
            if html is not None:
                return html
        return self._code(tokens[idx].content, tokens[idx].info)

    def code_block(self, tokens, idx, options, env):  # noqa: N802
        return self._code(tokens[idx].content, "")

    def _code(self, content: str, info: str) -> str:
        lang = (info or "").strip().split(" ")[0]
        parts = ['<div class="code">']
        if lang:
            parts.append(f'<p class="code-lang">{_escape(lang)}</p>')
        parts.append(f"<pre><code>{_escape(content.rstrip())}</code></pre>")
        parts.append("</div>")
        return "".join(parts)


def render_wide(md: MarkdownIt, tokens: list[Token], env: dict) -> str:
    renderer = WideRenderer()
    return renderer.render(tokens, md.options, env)


def split_sections(
    md: MarkdownIt, tokens: list[Token], env: dict, default_title: str
) -> list[Section]:
    """Render the narrow profile as one HTML fragment per top-level section.

    Split on H1. Content before the first H1 becomes a leading section titled
    after the document itself, so a file that opens with prose is not lost.
    """
    renderer = NarrowRenderer()
    cfg = env[ENV_CFG]

    boundaries: list[int] = []
    for idx, token in enumerate(tokens):
        if token.type == "heading_open" and token.tag == "h1":
            boundaries.append(idx)

    chunks: list[tuple[str, list[Token]]] = []
    if not boundaries:
        chunks.append((default_title, tokens))
    else:
        if boundaries[0] > 0:
            chunks.append((default_title, tokens[: boundaries[0]]))
        for n, start in enumerate(boundaries):
            end = boundaries[n + 1] if n + 1 < len(boundaries) else len(tokens)
            span = tokens[start:end]
            title = default_title
            if len(span) > 1 and span[1].type == "inline":
                title = span[1].content.strip() or default_title
            chunks.append((title, span))

    sections: list[Section] = []
    for n, (title, span) in enumerate(chunks):
        if not span:
            continue
        prepared = tables.transform(span, renderer, md.options, env, cfg.table_columns)
        body = renderer.render(prepared, md.options, env)
        if not body.strip():
            continue
        sections.append(Section(title=title, html=body, anchor=f"s{n:03d}"))

    if not sections:
        sections.append(
            Section(title=default_title, html="<p><em>(empty document)</em></p>",
                    anchor="s000")
        )
    return sections
