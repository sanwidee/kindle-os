# kindle-os

Turning a jailbroken Kindle into a general-purpose reading endpoint: a personal
system for getting long-form text — notes, work documents, Claude Code session
output, and manga — onto e-ink, on purpose, instead of reading it on a phone.

**Status: design phase. Nothing is deployed. No code has been written yet.**
This repository currently contains documentation only.

---

## Why

A Kindle is the best reading surface most people already own and the one they use
least. The screen is calm, the battery lasts weeks, nothing on it notifies you.
The obstacles are practical rather than fundamental: getting arbitrary text onto
the device is awkward, the stock software is a storefront first and a reader
second, and anything that isn't a purchased ebook arrives by email-to-Kindle or a
USB cable.

Jailbreaking plus [KOReader](https://github.com/koreader/koreader) removes most of
that. What remains is a delivery problem, which is what this project is.

## The three pillars

**1. Personal reading system.** One place holding the long-form material worth
reading on e-ink rather than on a screen that also has messages on it. Reachable
from a browser when convenient, from the Kindle when preferable.

**2. Claude Code output on e-ink.** A Claude Code session often produces something
worth reading properly — an audit, a plan, a long explanation, a code review.
Today that lands in terminal scrollback and dies there. The idea: a session ships
a markdown file to a hub, and a minute later it is on the Kindle as an EPUB
typeset for the panel. Code reflowed to fit the column, wide tables restructured
into records, images grayscaled, no color that turns to mush on a 16-level
display.

**3. Manga.** The Kindle is a genuinely good manga reader and a poor manga
*acquisition* device. The plan is a self-hosted library server exposing an OPDS
catalog KOReader can browse, sticking to publisher-official and clearly-licensed
sources. `docs/manga.md` covers what the legal landscape actually looks like,
including the honest conclusion about which routes exist and which do not.

## Architecture in one paragraph

A small hub service runs on a VPS behind nginx with TLS. Markdown lands in an
inbox directory; a background builder converts it into e-ink-tuned EPUBs and
indexes them in SQLite. One content store is exposed through two deliberately
different doors: an HTML login with a session cookie for browsers, and an OPDS 1.2
Atom feed protected by HTTP Basic for KOReader — Basic being the only auth
mechanism KOReader's OPDS client supports. File bytes are never served without an
auth check structurally in front of them. Details, and why each choice is what it
is, live in [`docs/architecture.md`](docs/architecture.md).

## Phases

**Phase 1 — the hub (designed, not built).** Markdown in, EPUB out, OPDS feed,
two-door auth, deployed behind an existing nginx. Ingest is rsync over SSH with a
restricted key, so nothing new listens on the network beyond the one vhost.

**Phase 2 — ingest and workflow.** An authenticated HTTP ingest endpoint so a
Claude Code session running anywhere, not only on one laptop, can publish. The
inbox is a plain directory contract, so this is a second writer against the same
interface rather than a rewrite.

**Phase 3 — manga.** Suwayomi-Server alongside the hub, its own OPDS catalog, CBZ
on disk so the library survives a broken source. Deliberately last: heaviest
component on a small box, least essential.

**Phase 4 — the rest.** Read-progress sync, an offline snapshot of a saved-article
queue, whatever the device turns out to be good at once the plumbing exists.

## Repository layout

```
README.md              this file
docs/architecture.md   hub design, auth model, OPDS channel, future seams
docs/device.md         what is on the Kindle, and what it constrains
docs/manga.md          manga pillar: options, legality, verdict
hub/                   the hub service (not yet written)
```

Everything under `docs/` is written in Indonesian — those are working notes. This
README is the English entry point.

## Getting started

There is nothing to run yet. If you are picking this up:

1. Read [`docs/device.md`](docs/device.md) first. Every constraint in the design
   traces back to a property of the device or of KOReader's OPDS client, and the
   design looks arbitrary until you know them.
2. Read [`docs/architecture.md`](docs/architecture.md) for the hub design and the
   reasoning behind the two-door auth model.
3. Phase 1 build order is converter first, offline, no server involved. The
   highest-value early check is sideloading a generated EPUB over USB and reading
   it on the actual device — that validates the whole typography layer while the
   cost of being wrong is still a stylesheet edit.

## A note on verification

These documents distinguish what was checked from what was assumed. Claims
verified against source code, a live device, or a running system are stated as
facts; everything else is marked UNVERIFIED and carried forward as an explicit
test step. Several load-bearing conclusions — KOReader's TLS behavior, its OPDS
parser quirks, its credential handling across redirects — come from reading the
v2025.10 source rather than from running it. Treat them accordingly.

## Public repository

This repo is public. It contains no credentials, no server addresses, and no
infrastructure identifiers. Host-specific values appear as placeholders such as
`<hub-host>`. Real values live in a private operations note and in environment
files that are never committed; only `.env.example` belongs here.

## License and scope

Personal project, no support, no promises. Everything it depends on — KOReader,
Suwayomi, KCC — is other people's free software. This is glue.
