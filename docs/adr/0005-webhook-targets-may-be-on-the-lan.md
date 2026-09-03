# Webhook targets may be on the LAN, so the SSRF guard blocks ranges rather than requiring a public address

## Context

Recording webhooks (#431) let a user paste a URL that the server then POSTs to.
That is the classic server-side request forgery shape, and the repo already has
a guard for it: `services/logo_cache.py` resolves the host and refuses unless
*every* resolved address is globally routable (`ipaddress.is_global`). It is the
right rule there — a channel logo comes from the provider, on the internet, and
nothing in the app should be fetching a logo from `127.0.0.1`.

Copying that rule to webhooks breaks the main use case. The reason to have a
webhook at all is to poke something else on the same network: a Plex or
Jellyfin library refresh at `http://192.168.1.50:32400/...`, an arr container,
a Home Assistant instance, a self-hosted ntfy. Every one of those is an RFC1918
address, and several are loopback for people running the whole stack in one
Docker host. A public-address-only rule rejects all of them and leaves the
feature useful only for hosted services like Discord.

The threat a webhook actually carries is also different from a logo fetch. A
logo URL arrives from an untrusted provider playlist. A webhook URL is typed by
an authenticated admin into Settings, behind the existing role check and CSRF
protection. The person configuring it already has more direct ways to make the
server do things than talking it into a POST. What is still worth blocking is
the small set of addresses that are never a real webhook and are the actual
prize in an SSRF: the cloud instance metadata endpoint at `169.254.169.254`,
and the rest of link-local, unspecified, multicast and reserved space.

## Decision

Webhook delivery uses its own guard, not the logo cache's:

- Scheme must be `http` or `https`; a host must be present.
- Private and loopback addresses are **allowed**. This is the deliberate
  divergence from `logo_cache`, and it is the point of the feature.
- Link-local (including `169.254.169.254` and `fe80::/10`), unspecified,
  multicast and reserved addresses are **refused**, both when the URL is a
  literal IP at save time and after DNS resolution immediately before the
  request goes out — so a name that resolves to the metadata endpoint is
  refused too.
- Redirects are not followed. A webhook receiver has no reason to redirect, and
  following one would let a permitted host hand the request to a forbidden
  address.
- The request is bounded: a ten second total timeout, a capped response read,
  and no retries.
- The URL is redacted to scheme, host and port in logs, because a webhook URL
  is very often a bearer token in a path.

## Consequences

An admin can point a webhook at anything on their own network, which is what
they wanted, and at the loopback interface of the box the app runs on. An
attacker who has already got admin — or who has got an admin to paste a URL —
can use a webhook to send an empty-bodied POST to a LAN service and learn
nothing back, since the response is discarded. That is the residual risk we are
accepting, and it is bounded by the admin gate in front of Settings.

The cloud metadata endpoint, the highest-value SSRF target and the one that
needs no credentials to exploit, stays unreachable.

This guard is not shared with `logo_cache`, on purpose. The two have different
callers and different correct answers, and parameterising one function with a
boolean would hide that. If a third outbound-URL feature appears, revisit.

## Considered options

- **Reuse `_validate_logo_url` as-is.** Rejected: it refuses the LAN, which is
  where the webhook targets live.
- **Add an "allow private addresses" checkbox in Settings.** Rejected for now:
  it asks the user a security question they cannot evaluate, and the safe
  answer is the one that breaks the feature, so everyone would tick the box.
- **Follow the `plex_outbound_policy` shape — an explicit named policy setting
  with a conservative default.** This is the closest existing precedent and
  remains the upgrade path if we ever need to lock webhooks down in a hosted
  deployment. Not built now because there is exactly one sensible policy today.
- **No address guard at all.** Rejected: the metadata endpoint is worth
  blocking for the cost of four boolean checks.
