# VOD preview source relay is guarded by token plus peer address, not by its own listener

## Context

A VOD preview must seek: nobody decides whether a two-hour film is the right one
from its first thirty seconds. Seeking means FFmpeg has to issue byte-range
requests against the source itself, and it cannot be handed the provider URL to
do that with — the URL embeds the account username and password, and anything in
FFmpeg's argv is readable by every user on the host via `ps`.

So the provider URL stays in-process behind an opaque token, and FFmpeg is
pointed at a relay endpoint on `http://127.0.0.1:<port>/api/vod/preview/source/
<token>`. The question is what stops anyone other than our own FFmpeg from
calling that endpoint.

Today the relay is a route on the main FastAPI app, guarded by three things: an
unguessable 256-bit token, a short TTL with revocation when the preview session
closes, and a check that the caller's peer address is loopback.

The peer-address check is worth less than it looks, though measurement narrowed
how much. Uvicorn's proxy-headers middleware is on by default and rewrites
`scope["client"]` from `X-Forwarded-For` when the immediate peer is trusted
(127.0.0.1 by default), so behind a same-host reverse proxy a forwarded request
arrives with the *real* client address, not 127.0.0.1, and is refused. Where the
check does collapse is a proxy that forwards from loopback without setting any
`X-Forwarded-*` header; there every request looks local. In that topology the
token is the only guard.

## Decision

Keep the relay on the main app, guarded by token + peer address, and additionally
refuse any request carrying proxy headers (`X-Forwarded-For`, `X-Forwarded-Host`,
`X-Real-IP`, `Forwarded`). FFmpeg sends none of them; a proxy forwarding a
request almost always adds them.

## Considered options

- **A separate loopback-only listener for the relay** — bind a second HTTP
  server to `127.0.0.1:0` and serve the relay route there alone. This is
  strictly stronger: "only local callers" becomes a property the kernel enforces
  through the bind address, rather than application logic inspecting a peer
  address it cannot trust. A proxy fronting the app on its public port cannot
  reach the relay at all, because the relay is not on that port. Rejected *for
  now* on cost, not on merit: it adds a second server lifecycle (startup,
  ephemeral port allocation, shutdown, desktop and Docker modes) for a residual
  risk the token already bounds. This is the option to take if the relay ever
  carries something worth more than one title.
- **A Unix domain socket** — would give the same kernel-enforced guarantee with
  filesystem permissions. Rejected: FFmpeg's HTTP client cannot read HTTP over a
  Unix socket.

## Consequences

- Behind a reverse proxy that sets `X-Forwarded-*` (the normal case for nginx,
  Caddy and Traefik), forwarded requests are refused twice over: uvicorn
  rewrites the peer address, and the header check catches what it does not.
  Where a proxy forwards from loopback setting no headers at all, the effective
  guard is the token.
- The relay URL names the port read off the socket the request arrived on, so it
  is correct behind a proxy on a different public port. It is wrong only if the
  app is bound to a non-loopback address exclusively (`--host 192.168.x.x`),
  where 127.0.0.1 is not listening at all; Docker, the desktop build and
  `main.py` all bind 0.0.0.0 or 127.0.0.1.
- The token itself is in FFmpeg's argv, so a local user on the host can read it
  from `ps` and fetch the bytes. This is inherent to the design and accepted:
  the token is worth one title for at most fifteen minutes, whereas the
  credentials it replaced are worth the whole account indefinitely. Shrinking
  that prize is the entire point of the relay.
