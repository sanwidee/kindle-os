#!/usr/bin/env bash
#
# Post-deploy assertions for the kindle-os hub.
#
# WHY: the security review's worst finding was not a bug in the code. It was
# that the single most important access control lived in an nginx file that
# had never been written -- and every health signal the system had said
# "fine". App tests passed, `kindle_hub doctor` printed x-accel True,
# /admin/health said "xaccel": true, the Kindle worked, the browser worked.
# A config typo and a full public leak looked identical from the inside.
#
# This script exists to make that failure loud. It probes from OUTSIDE, over
# the real hostname, with no credentials, and asserts on what an anonymous
# stranger actually receives. Run it after every deploy and after every nginx
# edit. A red line here means private notes are readable by the internet.
#
# Usage:
#   deploy/verify.sh                       # probe the live host
#   HOST=kindle.weedlabs.online deploy/verify.sh
#   SAMPLE_EPUB=abc123/notes.deadbeef.epub deploy/verify.sh
#
# Run it from your Mac, NOT from the VPS: from the box, 127.0.0.1 bypasses
# exactly the layer under test.

set -uo pipefail

HOST="${HOST:-kindle.weedlabs.online}"
BASE="https://${HOST}"
SAMPLE_EPUB="${SAMPLE_EPUB:-probe/does-not-exist.epub}"

pass=0
fail=0

ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fail=$((fail+1)); }
note() { printf '        %s\n' "$1"; }

# code_for <path> [extra curl args...]
code_for() {
  local path="$1"; shift
  curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "$@" "${BASE}${path}" 2>/dev/null || echo "000"
}

printf '\nProbing %s as an anonymous client\n\n' "$BASE"

# --- 1. THE ONE THAT MATTERS ------------------------------------------------
# /_epub/ must be `internal`. nginx answers 404 for an internal location
# requested from outside. Anything 2xx or 3xx here is a full library leak.
printf 'Content isolation\n'
c=$(code_for "/_epub/${SAMPLE_EPUB}")
case "$c" in
  404) ok  "/_epub/ refuses external requests (404) -- 'internal' is in effect" ;;
  2*)  bad "/_epub/ SERVED CONTENT (HTTP $c) -- the 'internal' directive is MISSING"
       note "Every EPUB is downloadable without credentials RIGHT NOW."
       note "Fix deploy/nginx/kindle-hub.conf, reload nginx, re-run this." ;;
  3*)  bad "/_epub/ returned a redirect (HTTP $c) -- location block is wrong" ;;
  000) bad "/_epub/ unreachable -- host down, DNS, or TLS failure" ;;
  *)   bad "/_epub/ returned HTTP $c -- expected 404" ;;
esac

# A bare prefix request should not list anything either.
c=$(code_for "/_epub/")
[ "$c" = "404" ] && ok "/_epub/ prefix itself is not listable" \
                 || bad "/_epub/ prefix returned HTTP $c -- expected 404"

# --- 2. both doors actually require credentials -----------------------------
printf '\nAuthentication\n'
for p in /opds /opds/new /opds/all; do
  c=$(code_for "$p")
  [ "$c" = "401" ] && ok "$p requires auth (401)" \
                   || bad "$p returned HTTP $c -- expected 401"
done

# --- 3. no 3xx on any Kindle path -------------------------------------------
# luasocket drops the Authorization header across a redirect, so KOReader
# renders a silent empty catalog rather than an error. A redirect here is a
# support nightmare that looks like "the feed is broken".
printf '\nKindle protocol constraints\n'
for p in /opds /opds/new /opds/search; do
  c=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 \
        -u 'probe:wrong-on-purpose' "${BASE}${p}" 2>/dev/null || echo "000")
  case "$c" in
    3*) bad "$p returned $c to a bad-credential request -- MUST NOT redirect" ;;
    401) ok "$p answers bad credentials with a plain 401" ;;
    *)  bad "$p returned HTTP $c to bad credentials -- expected 401" ;;
  esac
done

# The 401 must carry a Basic challenge, or KOReader will not prompt.
h=$(curl -sSI --max-time 20 "${BASE}/opds" 2>/dev/null | tr -d '\r' | grep -i '^www-authenticate:' || true)
case "$h" in
  *[Bb]asic*) ok "401 carries a WWW-Authenticate: Basic challenge" ;;
  "")         bad "401 has no WWW-Authenticate header -- KOReader will not prompt" ;;
  *)          bad "WWW-Authenticate is not Basic: $h" ;;
esac

# --- 4. the web door must not be reachable unauthenticated ------------------
printf '\nWeb door\n'
c=$(code_for "/")
case "$c" in
  200) bad "/ served content with no session -- the web door is open" ;;
  30*|401) ok "/ is gated (HTTP $c)" ;;
  *)   bad "/ returned HTTP $c" ;;
esac

c=$(code_for "/admin/health")
case "$c" in
  401|403|404) ok "/admin/health is not public (HTTP $c)" ;;
  200) bad "/admin/health is PUBLIC -- it names device tokens and their last IPs" ;;
  *)   bad "/admin/health returned HTTP $c" ;;
esac

# --- 5. rate limiting on the expensive endpoint -----------------------------
# /login runs argon2. The app bounds concurrency internally now, but the nginx
# limiter is still the first line and its absence is worth knowing about.
printf '\nRate limiting\n'
hit429=0
for _ in $(seq 1 25); do
  c=$(code_for "/login")
  [ "$c" = "429" ] && { hit429=1; break; }
done
[ "$hit429" = "1" ] && ok "/login is rate limited (saw 429)" \
                    || bad "25 rapid /login requests, no 429 -- limit_req is not active"

# --- 6. TLS sanity ----------------------------------------------------------
printf '\nTLS\n'
if curl -sS -o /dev/null --max-time 20 "${BASE}/opds" 2>/dev/null || [ $? -eq 22 ]; then
  ok "TLS chain validates from this machine"
  note "The Kindle does NOT validate it: KOReader sets verify=\"none\" and"
  note "ships no CA bundle. That exposure is accepted, not fixed here."
else
  bad "TLS validation failed from this machine -- check the certificate chain"
fi

c=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "http://${HOST}/opds" 2>/dev/null || echo "000")
case "$c" in
  30*) ok "plain HTTP redirects to HTTPS (HTTP $c)" ;;
  *)   bad "http://${HOST}/opds returned $c -- expected a redirect" ;;
esac

# --- 7. the authenticated happy path ---------------------------------------
#
# ADDED AFTER A MISS. Every check above passed on a deploy where the Kindle
# door was completely unusable: the OPDS feed listed books, and every download
# returned 403 because nginx could not read the library directory (the app had
# authenticated fine and handed off via X-Accel-Redirect). Testing only that
# strangers are kept OUT says nothing about whether the owner gets IN.
#
# Needs a device token. Mint a throwaway one, export it, run this, revoke it:
#   python -m kindle_hub mint-token probe-temp
#   OPDS_USER=probe-temp OPDS_PASS=<token> deploy/verify.sh
#   python -m kindle_hub token rm probe-temp
printf '\nAuthenticated path\n'
if [ -n "${OPDS_USER:-}" ] && [ -n "${OPDS_PASS:-}" ]; then
  feed=$(mktemp)
  c=$(curl -sS -u "${OPDS_USER}:${OPDS_PASS}" -o "$feed" \
        -w '%{http_code}' --max-time 30 "${BASE}/opds/new" 2>/dev/null || echo "000")
  if [ "$c" = "200" ]; then
    ok "OPDS feed served to a valid token"

    if grep -q 'profile=opds-catalog' "$feed" 2>/dev/null || grep -q '<feed' "$feed" 2>/dev/null; then
      ok "response parses as an Atom feed"
    else
      bad "200 but the body is not an Atom feed -- KOReader will show nothing"
    fi

    acq=$(grep -oE 'href="[^"]*\.epub"' "$feed" 2>/dev/null | head -1 | sed 's/href="//;s/"//')
    if [ -n "$acq" ]; then
      epub=$(mktemp)
      c=$(curl -sS -u "${OPDS_USER}:${OPDS_PASS}" -o "$epub" \
            -w '%{http_code}' --max-time 60 "$acq" 2>/dev/null || echo "000")
      if [ "$c" = "200" ]; then
        ok "acquisition link downloads with the same token"
        # An EPUB is a zip whose first entry must be an uncompressed
        # "mimetype". A 403 error page also returns 200-shaped bytes to a
        # careless check, so verify the actual container.
        # 64 bytes, not 30. The marker begins at byte 30 exactly: 4 bytes of
        # zip signature + a 26-byte local file header, then the "mimetype"
        # filename and its content. Reading 30 sliced the string in half and
        # reported every valid EPUB as broken.
        if head -c 64 "$epub" | grep -q "mimetypeapplication/epub+zip"; then
          ok "downloaded bytes are a well-formed EPUB"
        else
          bad "download is not an EPUB -- got $(file -b "$epub" 2>/dev/null | head -c 40)"
        fi
      else
        bad "acquisition link returned HTTP $c to a VALID token"
        note "The feed lists books the Kindle cannot download. Check that"
        note "nginx can read /srv/kindle-os/library (X-Accel-Redirect opens"
        note "the file as the nginx user, not as the app user)."
      fi
      rm -f "$epub"
    else
      note "no .epub acquisition link in the feed yet (empty library?) -- skipped"
    fi
  else
    bad "OPDS feed returned HTTP $c to a valid token"
  fi
  rm -f "$feed"
else
  note "SKIPPED -- set OPDS_USER and OPDS_PASS to test the authenticated path."
  note "Without it this script only proves strangers are kept out, not that"
  note "the Kindle actually works. That gap hid a real 403 once already."
fi

printf '\n----------------------------------------\n'
printf '  %d passed, %d failed\n\n' "$pass" "$fail"

if [ "$fail" -gt 0 ]; then
  printf 'DO NOT consider this deploy done. Fix the failures above.\n\n'
  exit 1
fi
exit 0
