#!/usr/bin/env python3
"""Convert a published artifact's HTML into hub-ready markdown.

WHY A SCRIPT AND NOT BY HAND
----------------------------
The artifacts are designed HTML pages: 20-60 KB each, most of which is a
frame runtime, a stylesheet, and layout scaffolding. Retyping thirty of them
by hand is slow, and worse, it invites silent drift between what the artifact
says and what the EPUB says. A converter is deterministic and rerunnable.

WHAT IT THROWS AWAY, AND WHY THAT IS CORRECT
--------------------------------------------
Everything that only exists to make a browser page look like a browser page:
the frame runtime, <style>, <script>, theme toggles, and the wrapper divs.
On e-ink a card with a coloured left border is a paragraph; a pill is a word.
Structure survives, decoration does not.

Tables, headings, lists, and emphasis survive because they carry meaning.
Colour-coded status (a red pill, a green badge) loses its colour, so where a
class name carries the meaning it is turned into a text prefix instead --
otherwise a "risk" and a "win" would read identically in greyscale.

Usage:
    artifact_to_md.py IN.html OUT.md --collection catatan [--tags a,b]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import html2text

# Class-name fragments whose colour carried the meaning. Greyscale drops the
# colour, so the meaning has to become a word or it is simply lost.
SEMANTIC_PREFIX = [
    (re.compile(r"\b(bad|lose|danger|risk|red|critical|kill)\b", re.I), "RISIKO: "),
    (re.compile(r"\b(good|win|green|success|ok)\b", re.I), "BAIK: "),
    (re.compile(r"\b(warn|amber|gold|caution)\b", re.I), "PERHATIAN: "),
]


def strip_chrome(html: str) -> str:
    """Remove everything that is presentation rather than content."""
    # The frame runtime is a single enormous <script>; style blocks are next.
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.S | re.I)
    html = re.sub(r"<style\b[^>]*>.*?</style>", "", html, flags=re.S | re.I)
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    # Theme toggles and similar controls are meaningless in a document.
    html = re.sub(r"<button\b[^>]*>.*?</button>", "", html, flags=re.S | re.I)
    html = re.sub(r"\sstyle=\"[^\"]*\"", "", html)
    html = re.sub(r"\sonclick=\"[^\"]*\"", "", html)
    return html


def annotate_semantics(html: str) -> str:
    """Turn colour-coded classes into words before the colour is discarded."""
    def repl(m: re.Match) -> str:
        classes = m.group(1)
        for pattern, prefix in SEMANTIC_PREFIX:
            if pattern.search(classes):
                return f'{m.group(0)}<span>{prefix}</span>'
        return m.group(0)

    return re.sub(r'<(?:div|li|td|p|span)\b[^>]*class="([^"]+)"[^>]*>', repl, html)


def extract_title(html: str) -> str:
    m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    m = re.search(r"<h1\b[^>]*>(.*?)</h1>", html, re.S | re.I)
    if m:
        return re.sub(r"<[^>]+>", "", m.group(1)).strip()
    return "Untitled"


def to_markdown(html: str) -> str:
    h = html2text.HTML2Text()
    h.body_width = 0          # never hard-wrap; the reader reflows
    h.ignore_images = True    # artifacts carry no meaningful raster content
    h.ignore_emphasis = False
    h.protect_links = True
    h.unicode_snob = True
    h.mark_code = False
    md = h.handle(html)

    # html2text leaves a lot of blank-line noise behind stripped chrome.
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = re.sub(r"^\s*\*\s*$", "", md, flags=re.M)
    # Its table output uses a leading pipe style that renders fine, but stray
    # single-cell rows from layout divs do not; drop those.
    md = re.sub(r"^\|\s*\|\s*$", "", md, flags=re.M)
    return md.strip() + "\n"


def frontmatter(title: str, summary: str, tags: list[str], collection: str) -> str:
    tag_list = ", ".join(tags)
    safe_title = title.replace('"', "'")
    safe_summary = summary.replace('"', "'")[:300]
    return (
        "---\n"
        f'title: "{safe_title}"\n'
        f'summary: "{safe_summary}"\n'
        f"tags: [{tag_list}]\n"
        f"collection: {collection}\n"
        "---\n\n"
    )


def first_paragraph(md: str) -> str:
    for line in md.splitlines():
        s = line.strip()
        if len(s) > 60 and not s.startswith(("#", "|", "-", "*", ">")):
            return re.sub(r"[*_`\[\]]", "", s)
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("infile")
    ap.add_argument("outfile")
    ap.add_argument("--collection", default="catatan")
    ap.add_argument("--tags", default="")
    ap.add_argument("--title", default="")
    args = ap.parse_args()

    raw = Path(args.infile).read_text("utf-8", errors="replace")
    title = args.title or extract_title(raw)

    cleaned = annotate_semantics(strip_chrome(raw))
    md = to_markdown(cleaned)

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    out = frontmatter(title, first_paragraph(md), tags, args.collection) + md
    Path(args.outfile).write_text(out, "utf-8")

    words = len(md.split())

    # LOW-YIELD GUARD.
    #
    # Some artifacts are applications, not documents: the content lives in a
    # JavaScript array and the DOM is built at runtime. Stripping <script>
    # then leaves only the static shell -- a title, a couple of headings, and
    # nothing else. The conversion "succeeds", the file is written, and the
    # document silently arrives on the Kindle with its substance missing.
    #
    # The Weedlabs Server Map is exactly this: 59 KB of HTML holding an
    # inventory of 58 services, which converted to 177 words. Nothing errored.
    #
    # Ratio, not absolute size, is the tell: a real document converts to
    # roughly 1 word per 25-60 bytes of source. Far past that means the words
    # were never in the HTML to begin with.
    ratio = len(raw) / max(words, 1)
    if ratio > 120:
        print(
            f"WARNING  {Path(args.outfile).name}: {words} words from "
            f"{len(raw) // 1024} KB of HTML ({ratio:.0f} bytes/word).\n"
            f"         Likely a JS-rendered artifact -- the content is in a "
            f"<script>, not the markup.\n"
            f"         Convert this one by hand or extract the data array.",
            file=sys.stderr,
        )

    print(f"{Path(args.outfile).name:<44} {words:>6} words  {len(out):>7} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
