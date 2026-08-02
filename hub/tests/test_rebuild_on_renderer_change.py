"""Regression test: a renderer change must reach documents already built.

This bug was silent in both directions. Nothing errored, no log line
appeared, and the library simply stayed on the previous renderer forever.
It only surfaced because covers were added and none of the existing EPUBs
grew one -- if the change had been subtler (a column budget, a spacing
tweak) it would still be undetected.
"""

from __future__ import annotations

from dataclasses import replace


def test_bumping_renderer_version_rebuilds_untouched_documents(cfg, tmp_path):
    """The markdown does not change; the renderer config does. The document
    must be rebuilt anyway, because build_sha covers both."""
    from kindle_hub.builder import sweep
    from kindle_hub.catalog.store import Store

    inbox = tmp_path / "inbox" / "notes"
    inbox.mkdir(parents=True)
    (inbox / "doc.md").write_text(
        "---\ntitle: Unchanged document\n---\n\n# Heading\n\nBody text.\n",
        encoding="utf-8",
    )

    c1 = replace(
        cfg,
        inbox_dir=tmp_path / "inbox",
        library_dir=tmp_path / "lib",
        state_dir=tmp_path / "state",
        renderer_version="1",
    )
    c1.ensure_dirs()
    store = Store(c1.db_path)
    store.migrate()

    first = sweep(c1, store)
    assert first.built == 1, "first pass should build the document"

    # Same config, same bytes: nothing to do.
    again = sweep(c1, store)
    assert again.built == 0, "an unchanged document should not be rebuilt"

    # Renderer bumped, markdown untouched. This is the case that used to be
    # skipped, leaving the library on the old renderer with no signal.
    c2 = replace(c1, renderer_version="2")
    after_bump = sweep(c2, store)
    assert after_bump.built == 1, (
        "bumping the renderer version must rebuild; comparing source_sha "
        "instead of build_sha silently skipped this"
    )

    # And it settles again rather than rebuilding on every sweep.
    settled = sweep(c2, store)
    assert settled.built == 0, "rebuild should be one-shot, not every sweep"
