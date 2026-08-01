# kindle-os hub

Markdown goes into a directory. An e-ink EPUB and an OPDS feed come out.

A Claude Code session writes `notes/2026-08-01-vps-audit.md`, rsyncs it to the
inbox, and within a minute it is readable two ways: on the Kindle through
KOReader's OPDS browser, and in any browser through a small web reader.

One content store, two doors, because the two clients have nothing in common:

| | web door | kindle door |
|---|---|---|
| who | a browser, anywhere | KOReader v2025.10 on the Kindle |
| auth | one shared password, argon2id, server-side session cookie | HTTP Basic with a per-device random token |
| content | original code lines, real tables, horizontal scroll | code reflowed to 64 columns, wide tables transposed to records |
| why | it is the boring, solved shape for one human | it is the only mechanism KOReader supports |

The Kindle door is not a design choice. KOReader's OPDS client speaks
preemptive HTTP Basic over Atom XML and nothing else: no cookies, no bearer
header, no form login, no OPDS 2.0. Put a login redirect in front of it and it
follows the redirect, feeds the HTML to its Atom parser, and shows an empty
catalog with no error at all.

## Layout

```
hub/
├── requirements.txt        pinned runtime deps
├── pyproject.toml          package metadata, ruff + pytest config
├── Dockerfile              python:3.12-slim, non-root, gunicorn
├── docker-compose.yml      127.0.0.1 bind, 256 MB cap, capped logs
├── .env.example            every variable, documented, no real values
├── tests/                  the KOReader constraints, as assertions
└── src/kindle_hub/
    ├── app.py              Flask factory, security headers, no-3xx guard
    ├── config.py           env-driven, validated at startup
    ├── builder.py          flock-guarded 60s inbox sweep
    ├── __main__.py         CLI: serve build doctor hash-password mint-token
    ├── auth/               argon2id, sessions, device tokens, decorators
    ├── ingest/             scandir sweep, front matter
    ├── convert/            markdown -> two renders -> deterministic EPUB
    ├── catalog/            SQLite, six tables, hand-written SQL
    ├── web/                opds.py, reader.py, admin.py
    ├── templates/          Atom XML + reader HTML
    └── assets/             epub-eink.css (ships inside the EPUB), reader.css
```

## Run it locally

Python 3.12. On macOS `python3` may well be 3.9, so name the interpreter.

```bash
cd hub
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

Make somewhere for the data and a document to convert:

```bash
mkdir -p data/inbox/notes data/library data/state
cat > data/inbox/notes/hello.md <<'EOF'
---
title: Hello from the inbox
summary: A first document.
tags: [test]
---

# Hello

Some prose, and a code block that is far too wide for a Kindle panel at any
comfortable font size, which is the whole reason the converter exists.
EOF
```

Generate a password hash and a device token. Both commands run locally and
neither writes a secret anywhere:

```bash
python -m kindle_hub hash-password        # prompts, prints HUB_WEB_PASSWORD_HASH=...
python -m kindle_hub mint-token kindle-pw4  # prints the token ONCE, plus its digest
```

Then a dev environment. Note `HUB_COOKIE_SECURE=0`: over plain http the
browser drops a `__Host-` cookie, and the login would look broken for no
visible reason.

```bash
cp .env.example .env       # then edit it
export HUB_INBOX_DIR=$PWD/data/inbox
export HUB_LIBRARY_DIR=$PWD/data/library
export HUB_STATE_DIR=$PWD/data/state
export HUB_PUBLIC_ORIGIN=http://127.0.0.1:8090
export HUB_COOKIE_SECURE=0          # local http only
export HUB_USE_XACCEL=0             # no nginx in front, so Flask serves bytes
export HUB_SESSION_SIGNING_KEY=$(openssl rand -base64 32)
export HUB_WEB_PASSWORD_HASH='<paste from hash-password>'
export HUB_OPDS_TOKENS='kindle-pw4:<paste digest from mint-token>'

python -m kindle_hub build     # one sweep, then exit
python -m kindle_hub serve     # http://127.0.0.1:8090
```

Open <http://127.0.0.1:8090>, sign in with the password you hashed. Check the
feed with the device token:

```bash
curl -u kindle-pw4:<token> http://127.0.0.1:8090/opds/new
curl -u kindle-pw4:<token> -I http://127.0.0.1:8090/opds/new   # Last-Modified must be present
curl -i http://127.0.0.1:8090/opds/new                          # must be 401, never a redirect
```

Sideload an EPUB onto the actual Kindle over USB before any server exists.
That check validates the whole e-ink stylesheet while the cost of being wrong
is one CSS edit:

```bash
python -m kindle_hub render-one data/inbox/notes/hello.md --out /tmp/hello.epub
```

Other commands:

```bash
python -m kindle_hub doctor          # what this process can actually see
python -m kindle_hub build --force   # rebuild everything, ignoring hashes
python -m kindle_hub token list      # registered devices, last seen, hit count
python -m kindle_hub token rm kindle-pw4   # revoke; effective next request
python -m kindle_hub logout-all      # wipe every server-side session
```

## Container

```bash
docker compose build
docker compose up -d
docker compose logs -f
```

`docker-compose.yml` carries three comments you should read before editing it.
The important one: Docker publishes past firewalld on this box, so the port
binding must stay `127.0.0.1:8090:8090`, and the way to verify that is from
another machine (`nc -zv <ip> 8090` must fail), not from `firewall-cmd`.

## What nginx has to provide

Written by whoever owns `deploy/`, but the app depends on two things:

```nginx
location / {
    proxy_pass http://127.0.0.1:8090;
    proxy_set_header Host              $host;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

# The app returns X-Accel-Redirect pointing here. `internal` makes nginx
# refuse any external request for this prefix, so there is no URL an
# unauthenticated client can guess that reaches a file.
location /_epub/ {
    internal;
    alias /srv/kindle-os/library/;
}
```

Also expected: `limit_req` on `/login` (fail2ban on the box jails sshd only,
so the rate limit here is doing real work), `listen 443 ssl http2;` in the
1.14.1 dialect rather than the `http2 on;` form, and no redirect of any kind
on `/opds` or on a `.epub` URL.

## Things worth knowing before you change something

**No route on a Kindle path may return a 3xx.** `app._no_redirect_guard`
turns any that appears into a loud log line and a plain 401, and
`tests/test_koreader_constraints.py` asserts it. luasocket drops
`user`/`password` across a redirect hop, so a redirected acquisition URL
arrives unauthenticated.

**Every feed route must answer HEAD with an accurate `Last-Modified`.**
KOReader keys its in-memory feed cache on
`"opds|catalog|<url>|<last-modified>"`. Get this wrong and the shelf pins to
whatever it saw first, which reads exactly like a server bug.

**The feed may not contain `<!--`, `<![CDATA[` or `<?xml-stylesheet`.**
KOReader regex-mangles all three before its parser sees them.
`opds._guard` raises rather than shipping one.

**Acquisition hrefs end in `.epub` and carry no query string.** `getFiletype`
checks the filename suffix before the MIME type, and a link matching neither
is dropped from the download dialog without a message.

**The EPUB build is byte-deterministic** (fixed zip timestamps, fixed entry
order, `mimetype` first and stored). The build hash goes in the filename,
which is how KOReader tells a revision apart from the copy already on the
device. Breaking determinism means every rebuild looks like a new book.

**Images are never fetched over the network.** A build-time HTTP fetch would
be an SSRF vector from a semi-production box. Remote references render as a
labelled placeholder.

## Known unknowns

These are carried forward from the design and none of them is settled. Each
has a marked comment at the relevant seam in the code.

- **Kindle model and panel width.** `HUB_EINK_MAX_WIDTH=1072` assumes a
  PW4/PW5. Confirm under KOReader → Help → About.
- **Whether omitting `Last-Modified` really pins the feed cache** for the
  session. Inferred from the cache-key construction in source, never executed.
- **Glyph coverage for ☐ / ☑** in KOReader's bundled fonts. If they show as
  tofu, change two constants in `convert/textclean.py` to `[ ]` and `[x]`.
- **crengine's SVG support.** Phase 1 emits a placeholder rather than adding
  cairosvg for a rare case.
- **Whether KOReader resolves root-relative feed hrefs** in every code path.
  The default emits absolute URLs, which cannot be resolved wrongly.
- **Real container RSS under load.** The 45-75 MB figure is an estimate from
  comparable Flask services, not a measurement.

## Residual risks that are accepted, not solved

Stated plainly because pretending otherwise would be worse.

KOReader validates no TLS certificate (`verify="none"`, and there is no knob
to change it), so an active on-path attacker on a Wi-Fi network the Kindle
joins can capture the device token. It also writes OPDS credentials in
plaintext on a device that mounts as unencrypted USB storage, so anyone who
borrows or steals the Kindle reads that token. Neither can be fixed on the
device. Both are bounded by what the token is: read-only, single-purpose,
never the web password, and revocable with one row delete. Treat loss of
physical control of the Kindle as compromise of that token and rotate.

The box is shared. Root-level compromise of any other service on it reads the
artifact store in plaintext. This design does not defend against that; the
escape hatch is a dedicated box, not more auth layers here.
