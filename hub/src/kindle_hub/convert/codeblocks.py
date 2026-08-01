"""Build-time code reflow for e-ink.

The problem: a Kindle at a comfortable font size fits roughly 40-55 monospace
characters. Real code lines are 80-120. crengine has no horizontal scroll, so
something has to give at build time.

Rejected: shrinking the font (hits the legibility floor after a few
characters), and plain `word-wrap: break-word` (wraps mid-identifier and
destroys the indentation that carries the code's structure).

Chosen: reflow to a fixed column budget, breaking at token-ish boundaries,
with continuation lines indented and marked so a wrap is visually distinct
from a genuine new statement.

The web reader gets the original, unwrapped source. A browser can scroll
horizontally, so it should.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CONTINUATION_MARK = "↳ "  # ↳
TAB_WIDTH = 4

# Atoms are "a run of non-separator characters plus any separators that
# immediately follow it". Breaking between atoms keeps identifiers, numbers
# and string fragments intact; breaking inside one is the fallback for a
# single token longer than the whole budget (long URLs, base64, hashes).
_ATOM_RE = re.compile(r"\s+|[^\s,;:()\[\]{}<>=&|]+[,;:()\[\]{}<>=&|]*|[,;:()\[\]{}<>=&|]+")


@dataclass
class ReflowResult:
    text: str
    wrapped_lines: int
    truncated: bool
    original_lines: int


def _split_atoms(text: str) -> list[str]:
    atoms = _ATOM_RE.findall(text)
    return atoms or ([text] if text else [])


def _hard_chunks(atom: str, width: int) -> list[str]:
    return [atom[i:i + width] for i in range(0, len(atom), width)] or [atom]


def reflow_line(line: str, columns: int) -> list[str]:
    """Reflow one source line into <= `columns` wide pieces."""
    line = line.expandtabs(TAB_WIDTH).rstrip()
    if len(line) <= columns:
        return [line]

    indent = line[: len(line) - len(line.lstrip())]
    body = line[len(indent):]
    # Continuation lines sit two spaces deeper than the original, plus the
    # marker, so the eye can tell "this is the same statement" at a glance.
    cont_prefix = indent + "  " + CONTINUATION_MARK
    first_budget = max(columns - len(indent), 8)
    cont_budget = max(columns - len(cont_prefix), 8)

    out: list[str] = []
    current = ""
    budget = first_budget
    prefix = indent

    def flush() -> None:
        nonlocal current, budget, prefix
        out.append((prefix + current).rstrip())
        current = ""
        prefix = cont_prefix
        budget = cont_budget

    for atom in _split_atoms(body):
        if atom.isspace() and not current:
            continue  # never start a continuation line with padding
        if len(atom) > budget:
            if current:
                flush()
            if len(atom) > cont_budget:
                chunks = _hard_chunks(atom, cont_budget if out else first_budget)
                for chunk in chunks[:-1]:
                    current = chunk
                    flush()
                current = chunks[-1]
                continue
        if len(current) + len(atom) > budget and current:
            flush()
            if atom.isspace():
                # The wrap already provides the separation; carrying the
                # source's space over would indent the continuation by one
                # extra column.
                continue
        current += atom
    if current.strip():
        flush()
    elif not out:
        out.append(indent)
    return out


def reflow(source: str, columns: int = 64, max_lines: int = 400) -> ReflowResult:
    """Reflow a whole fence. Returns the reflowed text plus enough metadata
    for the renderer to explain itself to the reader."""
    lines = source.replace("\r\n", "\n").rstrip("\n").split("\n")
    original_lines = len(lines)
    truncated = False
    if original_lines > max_lines:
        lines = lines[:max_lines]
        truncated = True

    out: list[str] = []
    wrapped = 0
    for line in lines:
        pieces = reflow_line(line, columns)
        if len(pieces) > 1:
            wrapped += 1
        out.extend(pieces)

    return ReflowResult(
        text="\n".join(out),
        wrapped_lines=wrapped,
        truncated=truncated,
        original_lines=original_lines,
    )
