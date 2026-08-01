# Shared proxy headers for the kindle-os hub.
#
# X-Real-IP is the one the app trusts. It is $remote_addr -- the actual peer
# as nginx sees it -- and a client cannot influence it.
#
# X-Forwarded-For uses $proxy_add_x_forwarded_for, which APPENDS the real peer
# to whatever the client already sent. That means the FIRST element is
# attacker-controlled and only the LAST is ours. The app reads X-Real-IP first
# and falls back to the last XFF element; it used to read the first, which let
# anyone holding a stolen device token forge their address in the per-token
# access log -- the only compensating control the design has for device theft.
#
# If you ever put another proxy or a CDN in front of this, revisit both lines.
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;

proxy_http_version 1.1;
proxy_set_header   Connection "";

# No redirect rewriting. Kindle paths must never return a 3xx: luasocket drops
# the Authorization header on redirect, so KOReader silently shows an empty
# catalog instead of an error. The app guards this itself; nginx must not
# reintroduce one.
proxy_redirect off;
