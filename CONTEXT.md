# Mustarrd

An IPTV catchup DVR. It browses past EPG programs on Xtream Codes servers and
downloads catchup/timeshift streams, with optional commercial detection and
re-encoding before the finished file lands in the completed folder.

## Language

### Recording

**Time slot**:
A recording the user defines by hand as a channel plus a start and end time,
rather than by picking a program out of the guide. For when the guide has no
entry for what you want, or when you only want part of a much longer airing.
Always fetched from the provider's catchup archive: a time slot whose end is
still in the future waits until the airing has landed in the archive and is
then fetched like any other. It never captures the live stream.
_Avoid_: manual recording (collides with "manual download"), custom recording,
time range

**Ad-hoc download**:
A recording the user starts on the spot from a program already in the guide, as
opposed to one that waits on a schedule. Both are fetched from the catchup
archive.
_Avoid_: manual download (ambiguous now that [[time slot]] recording exists),
instant download

**Interrupted recording**:
A recording whose capture stopped early - the provider connection dropped and
the bounded retries ran out - but which left a file ffprobe can open. Mustarrd
keeps it: it is post-processed, named and moved to the completed folder like a
completed recording, and only its status differs. A partial ffprobe cannot open,
including a zero-byte one, is not an interrupted recording; it is deleted and
the download is Failed. Distinct from a **completed recording with warnings**,
which finished but probes oddly - a recording can be both.
_Avoid_: partial recording (that's the file on disk mid-capture), aborted,
truncated, incomplete

**Short-file failure**:
An opt-in classification (**Mark short downloads as failed**, off by default) of
a catchup recording whose raw capture measures more than five seconds shorter
than the requested padded window: it is Failed rather than Completed. The
transfer itself succeeded, which is what separates it from an **interrupted
recording**. It does not enable recovery by itself: automatic re-downloads
require **Auto-retry failed downloads**. Unknown measurements and VOD do not
qualify. A finalized short Failed recording keeps a playable file and announces
both `recording.failed` and `recording.completed`.
_Avoid_: short recording (says nothing about the status), truncated

**Deferred retry capture**:
A raw short capture held without Commercial Skip, remuxing or transcoding while
bounded automatic recovery is still possible: at most three retries after the
original capture, at least ten minutes apart, within the archive window and
disk limits. When recovery ends the longest held attempt is finalized, under the
recording's original name, and the others are removed. A held capture always
ends: when retries run out or are switched off, when the linked schedule was
cancelled, or after six hours if the provider cannot be reached. Exact file
ownership is persisted across restart; superseded attempts are removed only
after the kept file has landed and probes as playable. Old retry ownership is
never inferred from filename patterns.
_Avoid_: held download, pending retry (a retry in the queue is Pending)

**Catchup URL style**:
Which of the two shapes a provider serves catchup links in: the **path style**
(`/timeshift/{user}/{pass}/{duration}/{start}/{id}.ts`) or the **query style**
(`/streaming/timeshift.php?username=…`). A per-account setting picks one, or
leaves it on **Automatic**, in which case the first provider refusal makes
Mustarrd retry in the other style and remember the answer as the account's
**resolved style**. The setting is the user's choice; the resolved style is what
probing worked out, and the two are stored separately so probing can never
overwrite a deliberate choice.
_Avoid_: catchup format, timeshift mode, URL type

**Retry window**:
The total time one download will spend waiting for the provider to come back
after network errors mid-recording, before it gives up. Spent in growing waits
between reconnect attempts, each of which resumes from the bytes already on
disk. Counts waiting only, across the whole download, and does not reset when
a retry succeeds. Distinct from the Auto-retry failed downloads option,
which re-queues a download that has already failed.
_Avoid_: retry count, retry limit, backoff (those are how the window is spent,
not what the user sets), outage timer

**Raw download duration**:
The measured length of catchup media immediately after capture, before trimming,
Commercial Skip or container conversion. Compared with the requested duration,
including explicit pre/post padding, with a five-second timestamp tolerance.
Stored separately from the final file's recorded duration and retained after
processing; a new capture attempt clears the previous observation. Missing or
implausible measurements remain uncertain, never replaced with the requested
length. This is a duration estimate, not proof of uninterrupted or correct content.

**Buffer trim outcome**:
The saved result of the excess-tail step on the current raw capture. A confirmed
trim is recorded before Commercial Skip or conversion and is independent of the
final file duration and overall download status. Old rows without this evidence
remain unknown; matching final length never backfills a successful trim. A new
capture resets the result. A file already at the target on recovery is reported
separately rather than assumed to have been trimmed.

**Recording rule**:
A saved instruction to record every **airing** of a show on one channel of
one account, so nobody has to pick each episode by hand. It matches on the
programme title (exactly, by "contains", or by a regular expression that only
an admin may save), optionally only on certain weekdays, with its own padding.
A rule does no recording itself: it creates ordinary scheduled recordings,
when it is saved and after every guide refresh. Saving one also catches what
is on air and what finished in the last day, if catchup still has it; a guide
refresh only looks forward. Deleting a rule deletes the recordings it
scheduled that have not started and keeps everything else as history.
_Avoid_: series rule, season pass, series link (it matches a title, it knows
nothing about seasons), recurring recording (that's what the user gets, and
the name of the tab), time slot (that's a clock time, not a title)

**Airing**:
One showing of a programme: this account, this channel, this start and stop.
It is what a recording rule schedules, and its identity is what stops the
same showing being recorded twice. An airing already scheduled, recording or
recorded, by hand or by any rule, is taken. A **moved listing** is the same
airing, not a new one.
_Avoid_: episode (two airings can be the same episode), programme (that's the
guide entry; a repeat is the same programme and a different airing), slot

**Moved listing**:
A guide entry the provider retimed between refreshes: a match overran, or
8:00 was corrected to 8:05. The guide keeps the old entry and gains the new
one, so one airing shows up twice. Two entries on a channel are the same
airing when they have the same title and overlap for more than half of the
shorter one, which still keeps back-to-back episodes apart. A rule's
recording that has not started is moved onto the newer entry; anything
already started, made by hand, or cancelled is left as it is and simply not
recorded again.
_Avoid_: shifted airing, duplicate listing, retime

### Commercial Skip

**Commercial Skip**:
The feature (and Settings section) that runs Comskip over a recording to find
advertising. It has three modes: Off, Mark, and Cut.
_Avoid_: Comskip (that's the tool, not the feature), ad-detection

**Mark** (mode):
Comskip detects commercials and Mustarrd, without cutting the video, writes an
EDL sidecar (for generic players) and embeds commercial-break chapter markers in
the container (for Plex). The original content stays intact. Chapters require an
MKV/MP4 container; on Keep .ts, Mark produces the sidecar only.
_Avoid_: soft cut, EDL mode, sidecar mode

**Commercial chapter**:
A chapter marker embedded in the finished MKV/MP4 at a commercial boundary, so a
Plex client can jump the break by chapter. Mustarrd's stand-in for Plex
commercial skip, which Plex does not offer for non-DVR library files.
_Avoid_: marker (overloaded by Plex's own DB markers), ad break

**Cut** (mode):
Comskip detects commercials and FFmpeg physically removes them, producing an
altered, shorter video file. The pre-existing behaviour.
_Avoid_: remove, hard cut, strip

**EDL sidecar**:
The `.edl` (Edit Decision List) file Comskip emits, named to match the finished
video and placed alongside it, listing commercial segments so players can skip
them. Produced only in Mark mode.
_Avoid_: cutlist, skip file, chapters

### Guide

**Structured guide metadata**:
Everything a provider's XMLTV entry says about a program beyond its title,
description and timing: the episode subtitle, the full category list, the season
and episode number, and any external identifiers (Gracenote, TVDB, TMDB, IMDb).
Owned end to end by one module (`backend/guide_metadata.py`), which defines the
field list once for the database, the model and the guide payload.
_Avoid_: extra fields, EPG extras, rich metadata

**Primary category**:
The first category the provider listed for a program. Kept in its own column so
everything that read a program's single category before still works; the rest of
the list lives alongside it.
_Avoid_: main genre, default category

### Preview

**Preview**:
Watching a provider stream in the browser before or instead of downloading it,
to answer "is this the right thing?". Applies to live channels, catchup
programs, and VOD. Time-limited and concurrency-limited by design: it is not a
TV client.
_Avoid_: playback (that's watching a finished recording), streaming, watch

**Direct preview** (mode):
A preview where the browser receives the provider's original broadcast stream
and decodes it itself. Cheap for the server, but only works when the browser
can handle both the container and the codecs.
_Avoid_: raw preview, passthrough, TS preview

**Converted preview** (mode):
A preview where the backend re-packages the provider stream into a format the
browser can decode before sending it. Used when a Direct preview is impossible
or has failed. Costs the server real work, so it is entered by fallback rather
than by default.
_Avoid_: transcoded preview (it usually only converts the audio), compatibility
mode (that's the user-facing wording, not the concept), server-side preview

### Poster art

**Poster art**:
The picture that stands for a finished recording: the show's poster for an
episode, the film's poster for a film. Looked up after the recording is
already finished, from a **poster source**, and used twice: as the thumbnail
in History and as a **library poster** saved next to the recording. Having
none is a normal state, not a failure. One poster per recording, though every
episode of a show normally ends up with the same one.
_Avoid_: artwork (fine in the UI, but it also means fanart, banners and logos,
which Mustarrd does not fetch), cover, thumbnail (that's only the small
rendition History shows), logo (that's a channel's)

**Poster source**:
A service Mustarrd asks for a poster: TMDB (only with the user's key), then
TVMaze. Asked by ID when the [[structured guide metadata]] has one, otherwise
by name, and a name match must be exact.
_Avoid_: provider (that's the IPTV service), scraper, agent

**Library poster**:
The copy of the poster saved in the completed folder, named the way Kodi and
Jellyfin look for it: `poster.jpg` in a show's own folder, or the recording's
name ending in `-poster.jpg` anywhere else.
_Avoid_: sidecar (kept for the `.nfo` and EDL files that describe one
recording; a show's `poster.jpg` is shared by all its episodes)

### Integrations

**Recording event**:
Something that happened to one recording, announced once from a single seam so
anything that wants to react does not have to find every place a status is
written. Five of them: **recording started**, **recording completed**,
**recording failed**, **recording cancelled**, and **post-processing
completed**. An event is an announcement, not a request: nothing it reaches can
change, delay or fail the recording it describes.
_Avoid_: status change, notification (that's a message shown to a person), hook

**Webhook**:
An address the user gives Mustarrd so it can POST a JSON message there when a
particular recording event happens. One address per event; leaving it empty
turns that one off. A webhook is fire-and-forget - it is not retried, and a
receiver that is down, slow or unhappy is logged and forgotten. Usually points
at something on the same network, such as a Plex or Jellyfin library refresh.
_Avoid_: callback, hook, script (Mustarrd never runs a user-supplied script),
integration

**Notification**:
A problem shown to a person under the bell in the app shell: an account
expiring or expired, an account the guide refresh could not connect to, or a
recording that failed. One per problem, not one per occurrence: a guide refresh
that keeps failing updates the same notification. It disappears on its own when
the problem does (the account is renewed, the connection comes back, a retry
succeeds). **Dismissing** hides it for that person only, until the problem
comes back or gets worse. Admins see every notification; a download-only user
sees only failures of recordings they asked for. In-app only; anything sent off
the box is a [[webhook]].
_Avoid_: alert, toast (that's the transient pop-up), event (that's a
[[recording event]])
