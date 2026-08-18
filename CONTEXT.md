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
