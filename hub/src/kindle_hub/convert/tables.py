"""Table handling: measure first, transpose if the grid will not fit.

A clipped grid on a Kindle reads as nothing at all, so a table that is too
wide is converted to record form: one block per row, first column as the
heading, the rest as a definition list. This is the standard narrow-viewport
pattern and it reads well on e-ink.

Implemented as a token-stream pre-pass rather than as renderer rules. A
markdown-it table is a *sequence* of tokens (table_open, thead_open, tr_open,
th_open, inline, ...), and a rule that fires on table_open cannot skip the
tokens that follow it. Splicing the whole span out and replacing it with a
single pre-rendered html_block token is both simpler and easier to test.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from markdown_it.token import Token

# Only the five XML built-in entities are legal in EPUB XHTML. Anything like
# &mdash; would fail the wellformedness check in epub.py and degrade the
# section to escaped plain text.
NESTED_TABLE_PLACEHOLDER = (
    '<p class="placeholder">[nested table omitted - see the web reader]</p>'
)


@dataclass
class ParsedTable:
    head: list[str]  # rendered inline HTML per header cell
    rows: list[list[str]]
    head_text: list[str]  # plain text, for width measurement
    rows_text: list[list[str]]


def find_tables(tokens: list[Token]) -> list[tuple[int, int]]:
    """Return (start, end_inclusive) index pairs for each top-level table."""
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(tokens):
        if tokens[i].type == "table_open":
            depth = 0
            for j in range(i, len(tokens)):
                if tokens[j].type == "table_open":
                    depth += 1
                elif tokens[j].type == "table_close":
                    depth -= 1
                    if depth == 0:
                        spans.append((i, j))
                        i = j
                        break
            else:
                break  # unbalanced; leave the rest alone
        i += 1
    return spans


def parse_table(tokens: list[Token], renderer, options, env) -> ParsedTable:
    """Pull header and body cells out of a table token span."""
    head: list[str] = []
    rows: list[list[str]] = []
    head_text: list[str] = []
    rows_text: list[list[str]] = []

    in_head = False
    current: list[str] = []
    current_text: list[str] = []

    for idx, token in enumerate(tokens):
        if token.type == "thead_open":
            in_head = True
        elif token.type == "thead_close":
            in_head = False
        elif token.type == "tr_open":
            current, current_text = [], []
        elif token.type == "tr_close":
            if in_head:
                head, head_text = current, current_text
            else:
                rows.append(current)
                rows_text.append(current_text)
        elif token.type in ("th_open", "td_open"):
            inline = tokens[idx + 1] if idx + 1 < len(tokens) else None
            if inline is not None and inline.type == "inline":
                current.append(renderer.renderInline(inline.children or [], options, env))
                current_text.append(inline.content.strip())
            else:
                current.append("")
                current_text.append("")

    return ParsedTable(head=head, rows=rows, head_text=head_text, rows_text=rows_text)


def estimate_width(table: ParsedTable) -> int:
    """Rendered width in monospace-ish columns: sum of the widest cell per
    column, plus separators and padding."""
    grid = [table.head_text] + table.rows_text
    if not grid or not grid[0]:
        return 0
    ncols = max(len(row) for row in grid)
    widths = [0] * ncols
    for row in grid:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    return sum(widths) + 3 * (ncols - 1) + 4


def render_records(table: ParsedTable) -> str:
    """Transpose to record form for narrow screens."""
    headers = table.head or [f"Column {i + 1}" for i in range(
        max((len(r) for r in table.rows), default=0)
    )]
    parts = ['<div class="records">']
    parts.append(
        '<p class="records-note">Wide table shown as records. '
        "The grid is available in the web reader.</p>"
    )
    for row, row_text in zip(table.rows, table.rows_text, strict=False):
        label = row[0] if row else ""
        parts.append('<div class="record">')
        parts.append(f'<p class="record-key">{label}</p>')
        parts.append("<dl>")
        for i, cell in enumerate(row[1:], start=1):
            key = headers[i] if i < len(headers) else f"Column {i + 1}"
            if i < len(row_text) and not row_text[i]:
                continue  # skip empty cells; they add nothing but page turns
            parts.append(f"<dt>{key}</dt><dd>{cell}</dd>")
        parts.append("</dl></div>")
    parts.append("</div>")
    return "".join(parts)


def transform(
    tokens: list[Token], renderer, options, env, max_columns: int
) -> list[Token]:
    """Replace every too-wide table span with a pre-rendered records block.

    Tables that fit are left untouched, so the normal renderer emits a real
    <table>.
    """
    spans = find_tables(tokens)
    if not spans:
        return tokens

    out = list(tokens)
    for start, end in reversed(spans):
        span = out[start:end + 1]
        # A nested table shows up as a second table_open inside the span.
        if sum(1 for t in span if t.type == "table_open") > 1:
            replacement = _html_token(NESTED_TABLE_PLACEHOLDER)
            out[start:end + 1] = [replacement]
            continue
        table = parse_table(span, renderer, options, env)
        if estimate_width(table) <= max_columns:
            continue
        out[start:end + 1] = [_html_token(render_records(table))]
    return out


def _html_token(markup: str) -> Token:
    token = Token("html_block", "", 0)
    token.content = markup
    token.block = True
    return token


def escape(text: str) -> str:
    return html.escape(text, quote=False)
