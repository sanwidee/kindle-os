"""Text hygiene for Kindle fonts.

Two jobs: strip glyphs the device cannot draw, and make the output valid
XHTML so the EPUB container is well formed.
"""

from __future__ import annotations

import re

# Emoji and pictographic ranges. Kindle's bundled fonts have no coverage, so
# these render as tofu boxes. (This also happens to match San's standing
# no-emoji rule.)
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # pictographs, emoticons, transport, symbols
    "\U00002600-\U000027BF"  # misc symbols + dingbats
    "\U0001F1E6-\U0001F1FF"  # regional indicators (flags)
    "\U00002190-\U000021FF"  # arrows -- EXCEPT the ones we allow below
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0000200D"             # zero-width joiner
    "]+",
    flags=re.UNICODE,
)

# Glyphs we deliberately keep even though they fall inside a stripped range.
# U+2610/U+2611 are in here because the emoji ranges below would otherwise eat
# the task-list checkboxes we just substituted in.
_KEEP = {
    "↳",  # ↳ the code-continuation marker
    "→",  # → common in prose
    "←",  # ←
    "☐",  # ☐ unchecked task
    "☑",  # ☑ checked task
}

_VOID_TAGS = ("br", "hr", "img", "input", "meta", "link", "col", "source", "wbr")
_VOID_RE = re.compile(
    r"<(" + "|".join(_VOID_TAGS) + r")\b([^>]*?)(?<!/)>", flags=re.IGNORECASE
)

# mdit_py_plugins' tasklists renders a bare <input type="checkbox">, which is
# not valid XHTML and which a Kindle draws as an empty box anyway.
#
# UNVERIFIED: glyph coverage for U+2610 / U+2611 in KOReader's bundled fonts
# was never confirmed on device. If these show as tofu, change the two
# constants below to "[ ]" and "[x]" -- there is nothing else to change.
CHECKBOX_UNCHECKED = "☐"  # ☐
CHECKBOX_CHECKED = "☑"  # ☑

_CHECKBOX_RE = re.compile(
    r'<input\b[^>]*class="[^"]*task-list-item-checkbox[^"]*"[^>]*>', re.IGNORECASE
)


def strip_emoji(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        return "".join(ch for ch in match.group(0) if ch in _KEEP)

    return _EMOJI_RE.sub(repl, text)


def replace_task_checkboxes(html: str) -> str:
    def repl(match: re.Match[str]) -> str:
        checked = "checked" in match.group(0).lower()
        glyph = CHECKBOX_CHECKED if checked else CHECKBOX_UNCHECKED
        return f'<span class="task">{glyph}</span>'

    return _CHECKBOX_RE.sub(repl, html)


def self_close_void_tags(html: str) -> str:
    """Safety net for XHTML wellformedness.

    markdown-it with xhtmlOut=True already self-closes its own void elements,
    but plugin output and any raw HTML in the source do not necessarily.
    """
    return _VOID_RE.sub(lambda m: f"<{m.group(1)}{m.group(2)} />", html)


def xhtml_safe(html: str, strip_emoji_flag: bool = True) -> str:
    out = replace_task_checkboxes(html)
    out = self_close_void_tags(out)
    if strip_emoji_flag:
        out = strip_emoji(out)
    return out
