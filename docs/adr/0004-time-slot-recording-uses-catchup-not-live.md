# Time slot recording fetches from catchup, never captures live

Status: accepted

A time slot lets the user record a channel between two hand-picked times, including
times that have not aired yet. The obvious reading of "record 8pm to 10pm tonight"
is that Mustarrd opens the live stream at 8pm and writes it to disk for two hours.
We deliberately do not do this: a time slot whose end is still in the future simply
waits until that window has landed in the provider's catchup archive, and is then
fetched exactly like a time slot in the past. There is one code path, and it is the
existing catchup pipeline.

## Considered options

**Capture the live stream for future time slots.** Rejected. It is a second,
entirely separate recording engine — a long-lived connection held open for hours,
with its own reconnect, buffering, partial-file, and restart-recovery behaviour,
none of which the catchup downloader needs because catchup is a bounded file
transfer. It also degrades differently: a dropped live connection loses content
permanently, whereas a failed catchup fetch can simply be retried while the archive
window lasts. Mustarrd is a catchup DVR; live capture is a different product.

**Capture live only when the channel has no catchup archive.** Rejected for the
same reason plus a worse one: it makes recording behaviour depend on a provider
capability the user cannot see, so the same action would silently mean two
different things on two different channels.

## Consequences

- Recordings are never available while the airing is in progress. A time slot
  ending at 10pm produces a file some time after 10pm, once the provider publishes
  it. This is the same delay scheduled recordings already have.
- Time slots are offered only on catchup-capable channels. On a channel with no
  archive the feature cannot work at all, so the entry point is hidden rather than
  offered and left to fail later.
- A time slot can expire unrecorded if the provider's archive window passes before
  the fetch succeeds — the same failure mode, with the same error message, that
  scheduled recordings already have.
- If live capture is ever wanted, it should be a separate, explicitly named feature
  rather than a mode of this one.
