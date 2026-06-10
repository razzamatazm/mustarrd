# Changelog

All notable changes to Mustarrd are listed here. Most recent changes are at the top.

---

## 2026-06-09

### Added: GPU-accelerated encoding in Docker, with a plain-language GPU status panel in Settings

**What you would notice:** The Docker image can now encode recordings on your GPU. If your server has an Intel, AMD, or NVIDIA GPU visible to the container, re-encoding finished recordings is much faster and uses far less CPU. The Settings page tells you where you stand in plain language: a green "GPU encoding ready" badge with the detected vendor when everything works, a note when the driver was set manually, and a clear explanation when no GPU is available (for example, Docker Desktop on macOS and Windows does not share the host GPU with containers, so encoding falls back to the CPU; slower, but everything still works).

**What changed:** The Docker image now installs `jellyfin-ffmpeg7` from the Jellyfin apt repository (bundles VAAPI, QSV, NVENC, and AMF encoder builds) instead of copying ffmpeg binaries out of the LinuxServer ffmpeg image. Compatibility symlinks keep `/usr/local/bin/ffmpeg` and `ffprobe` working, and `CATCHUP_FFMPEG_PATH`, `CATCHUP_FFPROBE_PATH`, `LIBVA_DRIVERS_PATH`, and `LD_LIBRARY_PATH` point at the bundled tools and drivers. The Settings frontend renders the VAAPI diagnostics the backend already exposes via `/api/settings/tools` as a status badge, mapping the kernel driver or PCI vendor id to a friendly vendor name and explaining each detection state (auto-detected, manual override, device missing, sysfs unavailable).

---

### Added: Back up your scheduled recordings, and let failed downloads retry themselves

**What you would notice:** Two additions around scheduled recordings. First, the Scheduled Recordings page has new Export and Import buttons: Export saves your schedules to a JSON file, and Import restores them later — handy before reinstalling, when moving to another machine, or simply as a backup. Importing runs each entry through the same checks as scheduling normally does, so programs that are already scheduled, have already ended, or belong to an IPTV account this server does not have are skipped — and the result tells you exactly how many schedules were created and what was skipped and why. Admins export everyone's schedules; other users export and restore only their own. Second, admins get a small "Auto-retry failed downloads" switch on the same page (off by default). With it on, a catchup download that fails — for example because your provider hiccuped — is retried automatically: up to 3 times, at least 10 minutes apart, and only while the program is still inside the channel's catchup window, so Mustarrd never wastes attempts on programs that have aged out. Recordings you cancelled are never retried behind your back.

**What changed:** New export/import endpoints on the schedules API using a versioned JSON document that carries only the fields needed to recreate a schedule (channel, program identity and times, account reference by name or id, padding, custom filename) and no internal state; import reuses the existing schedule creation path so duplicates, ended programs and unknown accounts are skipped and reported in a summary. A new `auto_retry_failed_downloads` app setting (default off) gates a sweep in the scheduler loop that re-queues failed catchup downloads through the existing retry mechanism; each download tracks its automatic attempts (`retry_count`, `last_retry_at`, added via the usual startup migration). Unknown or unreachable catchup windows are treated conservatively (no retry), and downloads linked to cancelled or completed schedules are left alone.

---

### Added: Recording duration on cards, folder health check, smoother in-browser playback, and corrupt-file warnings

**What you would notice:** Four quality-of-life improvements around recordings. First, completed recordings on the Downloads page now show how long the recorded file actually is (for example "Recording duration: 1h 02m") — the real length of what landed on disk, not just what the TV guide said. Second, there is a new folder health check: the app can report exactly where your download and completed recordings folders point, whether each one exists, and whether it can actually write there — with a plain reason when it can't, such as a disconnected drive or network share, a read-only mount, no write permission, or a full disk (a small status panel for the Settings page is included and will appear there shortly). Third, playing a recording in your browser no longer waits for the whole file: playback starts right away and you can seek/skip around the video. Fourth, every finished recording gets a quick automatic health check; if the file looks unplayable (broken container, no audio/video streams, zero length), the recording is kept but clearly flagged with "Completed with warnings: file may be corrupt" so you can check it before the catchup window expires. This check can be turned off via the app settings if you prefer.

**What changed:** On completion, the finished file is probed with ffprobe and its real duration is stored on the download record (`recorded_duration_seconds`, added via the usual startup migration) and shown on completed download cards. A new admin endpoint `GET /api/settings/folders/status` resolves the recording folders the same way the download pipeline does and runs a create-and-delete write test in each, returning a specific failure reason; a `FoldersStatus` component renders it. The file-serve endpoint's HTTP Range support (206 partial content, `Content-Range`, `Accept-Ranges`, 416 on invalid ranges) is now pinned by regression tests so browser seeking keeps working. The post-download integrity check (container parses, at least one stream, nonzero duration, with the existing ffprobe timeout protection) writes a warning to the download's status message without ever failing or deleting the recording, and can be disabled with the new `integrity_check_enabled` app setting (on by default).

---

### Added: Star your favorite channels and preview programs before downloading in Browse

**What you would notice:** Two additions to the Browse page. First, every channel in the channel list now has a star button — starred channels jump to the top of the list so the channels you record from most are always one click away. Stars are personal: each signed-in user (including download-only users) keeps their own favorites, and they are remembered across sessions and devices. Second, programs in the channel guide that can still be downloaded — and the program airing right now — show a small "Preview" button. Clicking it opens a player so you can check you have the right program (or watch the live channel) before queueing a download. Whether the preview actually plays depends on your browser's support for the provider's broadcast format; if it stays black, downloads are unaffected. To keep your provider connection limits safe, at most two previews can run at the same time.

**What changed:** Stars are stored server-side in a new `starred_channels` table keyed per user, with a toggle endpoint and a `starred` flag in the channel list response; the list sorts starred channels first while keeping the provider's order otherwise. Previews are relayed through a new authenticated backend endpoint that opens the live or timeshift stream on the server and proxies the bytes to the browser, so the provider URL — which contains your account credentials — never reaches the page. The proxy caps concurrent previews at two, limits each preview to five minutes, and closes the provider connection as soon as the preview window is closed.

---

### Added: Account cards warn before your subscription expires and show what each provider supports

**What you would notice:** On the Accounts page, each account card now shows a yellow "Expires in N days" warning badge during the last week of your subscription (hover over it to see the exact date), so a renewal doesn't sneak up on you. Each card also shows a compact summary of what the provider actually offers: how many channels have catchup, the longest archive window seen (for example "Catchup: 1,234 channels · up to 7 days"), and whether the provider has a VOD (movies/series) library. These summaries come from data Mustarrd already collects during its regular guide refresh, so the Accounts page no longer asks your provider for the full channel list every time you open it — the page loads faster and puts less load on your provider. The summaries appear after the first guide refresh following this update.

**What changed:** The EPG ingest now records a per-account capabilities summary (catchup-enabled channel count, maximum archive days, VOD availability via one category lookup per refresh) on the account row, and the accounts API returns those fields. The frontend reads them from the accounts payload instead of fetching every channel from the provider on each page load, and shows an "expires soon" badge when the cached subscription expiry date is 7 days away or less.

---

### Fixed: The app no longer freezes during big file moves, guide refreshes, or when a network drive misbehaves — and large providers load reliably

**What you would notice:** Several situations that used to make the whole app momentarily unresponsive — frozen download progress, stalled WebSocket updates, pages not loading — are gone. Moving a finished multi-gigabyte recording into a completed folder on another drive (for example a NAS) no longer freezes everything for the duration of the copy. Refreshing the guide from a provider with a large compressed guide file no longer stalls the app while it unpacks. A slow or hung network drive no longer hangs the scheduler or downloads while free space is checked — the check now gives up after 10 seconds. Two more fixes for network-drive and big-provider setups: if your download folder sits on a drive that is not mounted, scheduled recordings now wait for it to come back (with a clear "folder is missing or unreachable" status) instead of silently recording onto the wrong disk and filling it up; and on providers with very large channel lists, loading channels and the program guide no longer times out after 30 seconds — those requests now get the same 90-second budget that movie/series lists already had. Browsing the guide and catchup lists is also faster, because each page load now asks the provider for its channel list once instead of twice.

**What changed:** Completed-folder moves, free-space checks (scheduler poll, startup recovery, and the per-download size preflight), and XMLTV gzip decompression all run in worker threads instead of on the event loop, and the shared free-space probe is read-only with a 10-second timeout — it no longer creates a missing download folder (which landed on the container's root filesystem when a NAS was unmounted) and reports the check as unavailable so the scheduler holds recordings in a paused state until the folder returns. The Xtream client's channel-list and per-channel EPG requests use a 90-second timeout matching the VOD endpoints. The Browse EPG and Catchup endpoints pass the already-resolved channel archive window into the EPG service instead of letting it fetch the full channel list a second time. Backend only, no frontend changes.

---

### Fixed: Scheduled recordings and season downloads are now safer when several things happen at once

**What you would notice:** A handful of rare but annoying scheduling problems are gone. A database hiccup at the moment a scheduled recording fires can no longer leave a stray download running behind a schedule that fires again later, so you will not see the same program downloading twice. When many scheduled recordings come due at the same time, Mustarrd now asks your provider for its channel list just once per account instead of once per recording, and it keeps a running estimate of how much disk each recording will need, so a burst of recordings can no longer blow through your minimum free space setting. Downloading a whole season is now all-or-nothing: if one episode clashes with a download that is already running, you get a single clear message naming the conflicting episode and nothing is queued, instead of a half-queued season. And if someone else in your household already scheduled a program, trying to schedule it again now tells you immediately that it is already scheduled by another user instead of accepting it and quietly failing later.

**What changed:** Schedule dispatch now commits the download row and the schedule update in one transaction and only enqueues the download after that commit succeeds. The scheduler caches each account's provider channel list for the duration of a polling tick, and subtracts the estimated size of recordings it just dispatched from the free-space check for the rest of the tick. The series download endpoint builds the whole batch first, runs one batched duplicate check, and commits all episodes in a single transaction. Schedule duplicate detection now considers every user's active schedules, not just your own, and returns a clear 409 message. Backend only, no frontend changes.

---

### Fixed: Commercial skip and transcoding no longer get skipped after a restart when the download and completed folders are the same

**What you would notice:** If you point the download folder and the completed folder at the same directory, restarting Mustarrd while a recording was waiting for (or in the middle of) commercial detection or transcoding used to mark that recording as completed without ever processing it — you would end up with the raw recording, commercials and all. After this fix, the recording is picked up again after the restart and goes through commercial skip and transcoding as configured. Saving Settings with both folders set to the same path now also logs a warning so the setup is easy to spot in the Logs page.

**What changed:** The restart-recovery check that treated "the file is already in the completed folder" as proof that post-processing had finished is no longer trusted when the download and completed folders resolve to the same directory — in that case the raw file lives there before processing has run, so the recording is re-queued for post-processing instead. Moving a finished file "to the completed folder" was already a no-op when both folders match, so nothing else changes. Backend only, no user settings or configuration were changed.

---

### Fixed: First-run setup stays local-only behind a reverse proxy, and provider download links are checked more strictly

**What you would notice:** Two security tighten-ups. First, if you run Mustarrd behind a reverse proxy in Docker (for example Unraid with Nginx Proxy Manager) and expose it to the internet, the initial "set admin password" screen was reachable by anyone on the internet before you finished setup — the app saw every proxied visitor as the proxy's local address and waved them through the "local network only" restriction. Now the app looks at the real visitor address reported by the proxy, so only visitors on your local/private network can complete first-run setup (unless you explicitly enable remote setup with `CATCHUP_ALLOW_REMOTE_SETUP`). Direct local access without a proxy works exactly as before. Second, when your IPTV provider supplies a direct download link for a movie or episode, Mustarrd only trusts it if it points back at your provider's server — that check now requires the port to match too, not just the server name, so a link to a different service on the same machine is no longer trusted.

**What changed:** The initial-setup endpoint now treats the forwarded client address (`X-Forwarded-For` / `X-Real-IP`) as the effective client when a request arrives through a proxy, and only consults those headers when the direct connection itself comes from a local/private address so they cannot be spoofed from the internet. The VOD direct-source trust check now compares host and port (resolving the default ports 80/443) and rejects URLs with invalid ports. Backend only, no settings or configuration were changed.

---

### Fixed: Downloads now survive brief provider hiccups, avoid pointless re-downloads, and show live progress for every stream type

**What you would notice:** Four reliability improvements to downloading. First, a brief network glitch (the provider dropping the connection or timing out for a moment) no longer instantly fails your recording — Mustarrd now retries a few times with a short pause, picking up from where the transfer left off instead of starting over. Second, if Mustarrd restarts while a recording was finishing and the file on disk turns out to be complete already, it keeps the finished file instead of downloading the whole thing again — even with providers that don't support resuming. Third, if a recording is too big to fit on your disk (counting the minimum free space you configured in Settings), the download now fails immediately with a clear message instead of streaming for hours and dying when the disk fills up. Fourth, recordings from providers that don't report a file size used to sit frozen at 0% on the Downloads page until they finished; they now show the running download size as the transfer progresses.

**What changed:** The download manager now retries transient network errors up to 3 times with backoff, resuming via HTTP Range when the provider supports it. When a provider ignores a resume request but its reported content length shows the file on disk is already complete, the existing file is kept. Before streaming begins, the reported content length is checked against free space on the download folder plus the minimum-free-space setting. Streams without a content length now persist and broadcast their byte count every 8 MB, and crash recovery was hardened so a partially transferred stream is never mistaken for a finished one. Backend only, no frontend changes.

---

### Fixed: Catchup windows are now calculated correctly, and Browse EPG shows exactly what your provider can still serve

**What you would notice:** Three related problems around how far back you can record are fixed. First, if your provider reports its archive length in hours (for example 168 for a 7-day archive), Mustarrd treated that number as days and advertised weeks of catchup that always failed to download; those channels now show the correct window (168 becomes 7 days). Second, channels with archives longer than 14 days were silently cut off at 14 in Browse EPG; the guide now goes back as far as the channel's actual archive allows. Third, programs that have aged out of the provider's archive no longer look downloadable: they appear greyed out with an "Expired" badge and a tooltip explaining they are outside the channel's catchup window, instead of queuing downloads that are guaranteed to fail.

**What changed:** The shared archive-duration helper now treats `tv_archive_duration` values above 30 as hours and converts them to days, and every consumer (EPG ingest, channel listing, EPG and catchup endpoints, the scheduler) goes through that helper. The `days_back` validation cap on the channel EPG and catchup endpoints was raised from 14 to 365, with the channel's real archive duration still applied as the effective limit. The Browse EPG page now requests the channel's full archive window and greys out past programs that ended before the window began.

---

### Fixed: Recordings are no longer lost when a cancel or an error arrives just as a download finishes

**What you would notice:** Pressing Cancel at the exact moment a recording finished downloading could delete the fully-downloaded file and mark the recording as cancelled, even though every byte was already on disk. Similarly, a hiccup at the very end of a download or during post-processing — for example the database briefly failing while saving the final status, or a crash right after the file was moved into your completed folder — could leave the recording stuck, marked as failed, or queued for a full re-download even though the finished file was sitting safely in the completed folder (and retrying such a "failed" recording could overwrite or delete the good file). After this fix, once the last byte has been written, the recording is always kept: it is moved to the completed folder and shown as completed, regardless of late cancels, database errors, or post-processing crashes.

**What changed:** The download manager now remembers when a transfer has fully finished and when the file has been moved to the completed folder. Cancel and error handlers check this before touching the file, finalize the recording as completed (retrying on a fresh database session if the original one broke), and never delete a file that reached the completed folder. Backend only, no user settings or configuration were changed.

---

### Fixed: Temporary files are now cleaned up when post-processing fails or is cancelled

**What you would notice:** Cancelling a recording during commercial detection or transcoding, or having post-processing fail partway through, no longer leaves junk behind in your download folder. Previously, half-written video files, Comskip working files (`.edl`, `.txt`, `.logo` and similar), and intermediate `_comskip_input` files could silently pile up and eat disk space. Cancelling now also actually stops the background FFmpeg helpers instead of leaving them running and writing to disk. In addition: empty Show/Season folders are removed from the download folder after a series episode moves to your completed folder; post-processing now checks free disk space before starting a transcode and fails with a clear error instead of dying mid-write and leaving a partial file; and a corrupt recording can no longer freeze post-processing forever while probing the file.

**What changed:** The post-processing pipeline cleans up its working files (Comskip sidecars, segment temp files, and partial transcode outputs) on every exit path — success, failure, and cancellation — while always keeping the raw recording in the download folder on failure. FFmpeg subprocesses used for Comskip input preparation and segment extraction are terminated when a job is cancelled. The file probe (ffprobe) now has a 60-second timeout with a clean fallback. A disk-space preflight (using the existing minimum free space setting) runs before any transcode or commercial removal starts. After moving a series episode to the completed folder, now-empty Show/Season directories are removed up to — but never including — the configured download folder. Backend only, no user settings or configuration were changed.

---

### Fixed: Guide (EPG) refresh is more resilient — no more wiped guides, missing channels, or wrong-time downloads

**What you would notice:** Several guide problems that could appear after an EPG refresh are gone:

- A "Force refresh" no longer wipes your entire guide when the provider serves a truncated or broken guide file. Previously, the old guide rows were deleted before the new file was checked, so a half-written file from the provider could leave the guide empty until the provider recovered. Now the existing guide is kept and whatever can be read from the broken file is merged in.
- If two channels in your provider's list share the same guide ID (common for HD/SD pairs of the same channel), both channels now show the program guide. Previously one of them silently ended up with an empty guide, and a warning is now logged so you can spot the duplicate in the Logs page.
- Channels whose programs appear before the channel definitions inside the provider's guide file are no longer skipped. Some providers order their guide files this way, which used to result in those channels showing no programs.
- When the provider's API is completely down during the guide gap-filling step (backfill), Mustarrd no longer pretends the backfill succeeded. It retries on the next refresh instead of waiting out a 6+ hour cooldown with gaps in your guide.
- If a guide backfill is interrupted (app restart or shutdown), the channels that were already fetched are kept, so the next refresh picks up where it left off instead of redoing everything.
- Downloads of programs whose guide entry was missing the provider's local start time no longer fetch the wrong time window. Those entries are now repaired automatically on the next guide refresh, so the catchup download lines up with the actual show instead of being shifted by the provider's timezone offset.

**What changed:** Six fixes in the EPG ingest service: the force-refresh delete is now gated on the guide file parsing cleanly; channels sharing a guide ID each receive the programme data (with a warning logged); the guide file is scanned for channel definitions before programmes are matched; a backfill where every channel fetch fails is not recorded as complete; backfill progress is committed per channel; and the guide upsert now repairs rows that were stored without the provider-local start time. Backend only, no user settings or configuration were changed.

---

### Fixed: A recording scheduled near the edge of your provider's catchup window no longer fails with a cryptic error

**What you would notice:** If you scheduled a recording for a show that started just outside your provider's catchup window (for example, a two-hour movie that began 7 days and 2 hours ago on a 7-day provider), Mustarrd would attempt the download anyway and then fail with a confusing 404-style error from your provider. After this fix, Mustarrd checks whether the program's start time is still within the window before attempting the download. If it is not, the recording is immediately marked as failed with a plain message: "Program is no longer available for catchup. It aired about 7 days ago, past the 7-day catchup window." No download is attempted and your download queue is not cluttered with a doomed job.

**What changed:** The scheduler now checks the program start time (not the end time) against your provider's catchup window before dispatching a download. Pre-padding is also included in the check: if the padded start falls outside the window, the schedule is marked failed rather than sending a URL the provider cannot serve. Backend only, no frontend changes.

---

### Improved: Your selected Scheduled Recordings tab is now remembered when you share a link or reload the page

**What you would notice:** On the Scheduled Recordings page, the two tabs (Upcoming and History) are now reflected in the page address. If you navigate to the History tab and copy the URL, sharing it opens History directly. Reloading keeps you on the tab you were viewing instead of always jumping back to Upcoming. The Downloads page already worked this way; Scheduled Recordings now matches it.

**What changed:** The Scheduled Recordings page was updated to read and write the active tab to a `?tab=` query parameter in the URL. Frontend only, no backend changes.

---

### Fixed: Recordings no longer download the wrong hour when your provider uses timezone abbreviations in the program guide

**What you would notice:** If your IPTV provider includes timezone names like EST, PST, or CET in their program guide data, Mustarrd was silently falling back to UTC when building the download URL. A show listed at 8:30 PM EST would download content starting at 8:30 PM UTC instead, which is five hours off. After this fix, Mustarrd correctly strips the timezone abbreviation before parsing the timestamp, so the right hour of content is fetched.

**What changed:** The download URL builder was updated to strip trailing 2-to-5-letter timezone abbreviations before parsing provider timestamps. The code already handled numeric offsets like `+0200`; this extends it to cover letter abbreviations. Backend only, no frontend changes.

---

### Improved: Browse EPG no longer shows two errors when your provider is offline

**What you would notice:** When your IPTV provider is unreachable, the Browse EPG page was showing the same error message in both the channel list on the left and the program guide panel on the right. The right panel now shows a neutral placeholder instead: a video icon and the message "Channel guide will appear here once your provider is connected." The actionable error with a link to Settings stays in the channel list, which is where the problem actually is.

**What changed:** The right panel of Browse EPG was updated to show a neutral placeholder instead of repeating the error when no channel is selected or the provider is offline. Frontend only, no backend changes.

---

### Improved: Plex Integration settings panel now matches the layout of all other settings sections

**What you would notice:** The Plex Integration section in Settings previously had no subtitle and its Connect Plex Account button appeared in a muted brownish color instead of the orange used everywhere else. It now shows a short description below the heading and the button is filled orange, matching every other primary action button in the app.

**What changed:** The Plex Integration panel title size and subtitle were updated to match all other settings panels. The Connect Plex Account button style was changed from light to filled. Frontend only, no backend changes.

---

### Improved: Your selected Downloads tab is now remembered when you share a link or reload the page

**What you would notice:** On the Downloads page, the three tabs (Active, History, Upcoming) are now reflected in the page address. If you navigate to the History tab and copy the URL, sharing it will open the History tab directly. Reloading the page keeps you on the tab you were viewing instead of always jumping back to Active.

**What changed:** The Downloads page was updated to read and write the active tab to a `?tab=` query parameter in the URL. Frontend only, no backend changes.

---

### Improved: Downloads history cards now show the full aired time range

**What you would notice:** Each card in Downloads > History used to show only the start time and how long the recording ran. It now shows "Aired: [start time] - [end time] ([duration])", matching the format already used on Scheduled Recordings history cards. This makes it easier to confirm exactly what window was captured.

**What changed:** The Downloads history card layout was updated to display both the start and end air times. Frontend only, no backend changes.

---

### Fixed: Cancelling a scheduled recording mid-dispatch no longer launches the download anyway

**What you would notice:** There was a narrow timing window where pressing Cancel on a pending scheduled recording had no effect: if the cancel arrived while the recorder was in the middle of queuing the download, the download would launch anyway and the recording would proceed as if you had not cancelled. After this fix, if you cancel during that window, the queued download is stopped and the recording stays cancelled.

**What changed:** The scheduler now re-checks the recording status after queuing a download and cancels it if the status changed to CANCELLED in the meantime. Each scheduled recording in a batch is also committed individually, so a cancel arriving during batch dispatch cannot be silently overwritten. Backend only, no frontend changes.

---

### Fixed: Commercial removal no longer leaves a leftover temp file when it fails

**What you would notice:** When commercial removal ran on a recording in TS format and failed at the step where it assembled the final file, a temporary file named something like `My Show S01E01_postproc_tmp.ts` would remain in your recordings folder with no indication of what it was. After this fix, that temp file is deleted when the operation fails, keeping your recordings folder clean.

**What changed:** The post-processor was updated to clean up the temporary work file when the final concat step fails. Backend only, no frontend changes.

---

### Fixed: Clicking Retry on a failed download no longer accidentally re-activates a cancelled scheduled recording

**What you would notice:** If you had cancelled a scheduled recording and later used the Retry button on its associated failed download entry, Mustarrd would silently mark the schedule as active again. This caused a confusing error if you then tried to reschedule the same program. After this fix, Retry leaves cancelled and completed schedules alone and only updates the status for schedules that are genuinely still pending.

**What changed:** The retry function was updated to skip the schedule status update when the linked schedule is already in CANCELLED or COMPLETED state. Backend only, no frontend changes.

---

### Fixed: Downloaded file names are no longer scrambled by hidden characters injected by your IPTV provider

**What you would notice:** Some IPTV providers inject invisible Unicode directional characters into program titles. These characters can cause the file name to appear reversed or garbled in your terminal, file manager, Plex library, or Jellyfin library. For example, a file genuinely named `My Show S01E01.ts` could display as `st.10E10S wohS yM`. After this fix, those hidden characters are stripped before the file is saved to disk, so names display and match correctly.

**What changed:** The filename cleanup function was extended to strip nine additional invisible Unicode control characters that control text direction. Backend only, no user settings or configuration were changed.

---

### Fixed: Disabling Plex integration in Settings now actually prevents Plex users from logging in

**What you would notice:** If you go to Settings and turn off the Plex integration, Plex users can no longer log in. Before this fix, flipping that setting had no effect: Plex users could still complete the login process even after an admin disabled Plex. Now the Plex login button is hidden from the login screen, and any attempt to start the Plex login flow is rejected with a permission error.

**What changed:** Three places in the login code now check whether Plex is enabled before allowing the login process to proceed. Backend only, no user settings or configuration were changed.

---

### Fixed: The Plex login endpoint is now rate-limited to protect your Plex account

**What you would notice:** No visible change during normal use. If someone repeatedly hits the Plex login endpoint very quickly (for example, an automated script against an internet-exposed Mustarrd instance), they now receive an error after 10 attempts within the rate window. Without this limit, a flood of requests could cause Plex.tv to block Mustarrd's shared client ID, making Plex login fail with "Plex PIN create failed" for all legitimate users until the window expires.

**What changed:** The same rate limit that already protects password login and Plex PIN completion was extended to cover the start of the Plex login flow. Backend only, no user settings or configuration were changed.

---

### Fixed: Session verification now consistently checks user roles against the database

**What you would notice:** No visible change during normal use. This closes a small security gap in how Mustarrd confirms your identity on each request.

**What changed:** When you log in, your role (admin or download-only user) is stored in a session token. On each request, the server checks that token to confirm you are still allowed in. The admin path already verified your role against the database on every request. The download-only path was missing that same check: it confirmed your user ID but trusted the role stored in the cookie without verifying it in the database. Both paths now verify role against the database consistently. Backend only, no user settings or configuration were changed.

---

### Improved: Settings panels now explain what each section does

**What you would notice:** Two settings panels that previously showed only a title with no explanation now include a short description. Settings > Accounts now reads "Connect your IPTV providers. Mustarrd reads your channel list and program guide from these accounts." Settings > Users now reads "Can browse channels, schedule recordings, and download programs. Cannot access Settings." Every other settings panel already had a description; these two were the only ones missing one.

**What changed:** A description line was added below the title in the Accounts section and the Users section of Settings. The Accounts section button layout was also adjusted so the buttons align to the top of the title instead of vertically centering. Frontend only, no backend changes.

---

### Fixed: Scheduling the same program twice no longer creates duplicate recordings for providers without guide data

**What you would notice:** Some IPTV providers do not include EPG IDs with their program data. Previously, if you accidentally scheduled the same program twice using such a provider, Mustarrd would create two scheduled recordings and attempt to download both at the same time, potentially corrupting the output file. Now, scheduling the same program twice returns "This program is already scheduled," matching the behavior you would see with a provider that includes full guide data.

**What changed:** The schedule creation check was extended to cover programs that have neither an EPG ID nor a program ID. It now deduplicates on account, channel, and the program's start and stop times. Backend only, no frontend changes.

---

### Improved: Cancelled scheduled recordings now explain what happened and link to Browse EPG

**What you would notice:** Before this change, a cancelled recording in Scheduled > History showed the show title, channel, CANCELLED badge, and air time with no explanation. If the recording was cancelled before downloading started and had no specific system message, the card gave no indication of what to do next. Now a note appears: "Cancelled before downloading. If this program is still in your provider's catchup window, you can find it in Browse EPG." The words "Browse EPG" are a clickable link that takes you directly there.

**What changed:** The Scheduled > History page was updated to show a plain-language explanation and a Browse EPG link on cancelled cards that have no other details. Frontend only, no backend changes.

---

### Fixed: Corrupted or far-future timestamps from your IPTV provider no longer crash the server

**What you would notice:** No visible change during normal use. Previously, if your IPTV provider sent a program timestamp so far in the future that it could not be represented as a valid date (for example, a year so large it overflows the server), Mustarrd would crash with a generic server error (HTTP 500) when you tried to queue a download or create a scheduled recording for that program. It now returns a clear "invalid input" error (HTTP 400) instead, and the rest of your downloads are unaffected.

**What changed:** Two server-side functions that convert provider timestamps to dates were updated to handle impossible values instead of crashing. Backend only, no user settings or configuration were changed.

---

### Improved: Plex settings page now shows clear instructions when no Plex account is connected

**What you would notice:** Settings > Plex Integration used to show empty dropdowns for Plex Server, Connection URI, and Libraries to Refresh even when you had never linked a Plex account. The form appeared broken with no explanation of what to do next. Now, when no account is connected, the form is hidden and replaced with a single message explaining that you need to click "Connect Plex Account" first. The Disconnect and Refresh Servers buttons are also hidden until an account is linked. Once you connect, the full form reappears as normal.

**What changed:** The Plex section of Settings now checks whether a Plex account is linked before showing the configuration form. Frontend only, no backend or settings changes.

---

### Fixed: Program guide no longer wipes itself when a program description contains XML-like text

**What you would notice:** If your IPTV provider includes program descriptions that happen to contain the text `<programme` (for example, a description that mentions XML or EPG formats), a forced guide refresh would silently delete your entire program guide and then show nothing. The Browse EPG page would appear empty until the next automatic refresh ran. After this fix, those descriptions are handled correctly and the guide is not affected.

**What changed:** The guide import code was updated so that text appearing inside description blocks cannot be mistaken for real guide data. The guard that decides whether to clear the guide before a fresh import now ignores text inside those blocks. Backend only, no user settings or configuration were changed.

---

### Improved: Security settings now requires you to confirm your new password before saving

**What you would notice:** Settings > Security used to have two password fields: Current Admin Password and New Admin Password. If you mistyped the new password there was no warning, and you could lock yourself out of Mustarrd with a password you never intended to set. Now there is a third field, Confirm New Admin Password. If the two new password fields do not match, a red "Passwords do not match" message appears as you type so you can correct the typo before saving.

**What changed:** A confirm password field was added to the Security section of Settings. The section description was also updated to clarify that the password controls access to the whole app, not just Settings. Frontend only, no backend changes.

---

### Improved: Variable chips on the File Naming settings page now click to insert

**What you would notice:** On Settings > File Naming, the small chips showing available variables (like `{show}`, `{season}`, `{date}`) used to be display-only. You could hover over them to read a tooltip, but clicking did nothing. Now clicking any chip inserts it directly into the template field at your cursor position, so you can build a template without typing the variable names by hand. A "Variables:" label has also been added so the chips are easier to find at a glance.

**What changed:** The variable chips in the File Naming section of Settings now respond to clicks. Focus and cursor position are restored after each insertion so you can click multiple chips in a row without extra steps. Frontend only, no backend changes.

---

### Fixed: Program guide now loads correctly for providers that return guide data in a simpler format

**What you would notice:** Some IPTV providers send their program guide (EPG) data as a plain list instead of the standard wrapped format. When that happened, Mustarrd would silently show no programs in Browse EPG for those channels. There was no error message, just an empty guide. After this fix, Mustarrd accepts both formats and the guide loads normally.

**What changed:** The guide-fetching code was updated to recognize and handle the bare-list response format in addition to the standard wrapped format. This is the same fix that was applied to other parts of the guide in a prior update. Backend only, no user settings or configuration were changed.

---

### Fixed: Your recording is kept safe when your completed folder runs out of space during the file move

**What you would notice:** On Unraid or NAS setups where your downloads folder and your completed folder are on separate drives or shares, a full completed-folder drive could cause Mustarrd to delete the original recording after it failed to copy it across. You would end up with no file and no explanation. After this fix, if the completed folder is full, Mustarrd leaves your recording in the downloads folder untouched and marks the download Failed with the message "Not enough space in the completed recordings folder. Your recording is safe in the downloads folder."

**What changed:** The download manager was updated to detect disk-full errors during the file move and preserve the source recording instead of deleting it. Backend only, no user settings or configuration were changed.

---

### Fixed: Channels no longer silently vanish from the program guide when your provider uses different text encodings for the same channel name

**What you would notice:** Some IPTV providers send channel names with invisible characters (zero-width spaces, byte-order marks) or use slightly different Unicode representations of the same name at different times. Previously, Mustarrd's channel-matching logic treated these as different channels, so all guide data for those channels was silently dropped. Your guide would appear empty for certain channels even though your provider was sending data. After this fix, invisible characters are stripped and encoding differences are normalized before matching, so the guide loads correctly for all channels regardless of how the provider encodes the name.

**What changed:** The channel name normalization step in the guide import was updated to strip invisible Unicode characters and apply standard Unicode normalization before comparing names. No user settings or configuration were changed.

---

### Fixed: Completed recording no longer deleted when a database glitch occurs right after the file is saved

**What you would notice:** On Unraid or NAS setups where the database file sits on a network share, a recording could disappear from your completed folder at the exact moment the share went briefly offline or the disk filled up. The download would finish and the file would move to your completed folder, but a split-second database error caused Mustarrd to delete its own completed recording. You would see the download marked as Failed with no file to show for it, even though the recording finished successfully. After this fix, if a database error fires after the file is already in your completed folder, Mustarrd leaves the file alone and marks the download Completed.

**What changed:** The download error handler was updated so that when a recording has already moved to the completed folder, a subsequent database error no longer deletes the file. Backend only, no user settings or configuration were changed.

---

### Improved: Settings now shows tool availability errors as quiet detail instead of a second alarm

**What you would notice:** Settings > Post-Processing used to show a red badge (FFMPEG UNAVAILABLE or COMSKIP UNAVAILABLE) followed immediately by another line of red text with a technical error message. For a non-technical user, the page looked like it had four separate problems. The error detail text is now displayed in gray, so the red badge remains the clear headline and the technical message reads as supporting detail below it.

**What changed:** Two color changes in the Settings page. No logic, settings values, or recording behavior was changed.

---

### Improved: Browse EPG right panel now shows a clear error when your provider cannot be reached

**What you would notice:** In Browse EPG, when your IPTV provider is temporarily unreachable, the right panel (where the channel guide appears after you select a channel) used to show a plain video icon and the message "Channel guide will appear here once your provider is connected." That message implied your provider was not set up yet, which was confusing if you had already configured it. After this fix, the right panel shows a red alert icon and the message "Could not reach your provider." Admins see a link to Settings > Accounts so you can check your credentials without leaving the page. Non-admin users see a prompt to contact their administrator. Both panels in Browse EPG now give the same clear signal when the provider is unreachable.

**What changed:** The right panel in Browse EPG was updated to detect when the channel list fails to load and show an appropriate error instead of the generic placeholder message used when no provider has been configured. Frontend only, no backend or settings changes.

---

### Improved: Settings now warns you when your program guide sync fails

**What you would notice:** The Settings > Guide section used to show "Last synced: Today at 5:55 AM" even when your program guide was failing to update. If your IPTV provider became unreachable or your credentials expired, you would see a reassuring timestamp with no hint that your guide was going stale. After this fix, the label changes to "Last sync attempt" and an orange alert appears with a direct link to the Accounts section whenever a real sync failure occurs. If the sync was successful, the section continues to show the normal "Last synced" timestamp with no alert.

A related false-alarm was also fixed: a single channel timing out during a guide refresh used to trigger the sync failure alert even though the rest of the guide updated correctly. That no longer happens.

**What changed:** The backend was updated so that per-channel timeout errors during guide backfill no longer count as a full sync failure. The Settings > Guide section in the frontend was updated to show the alert and the updated label only when the sync genuinely fails. No user settings or configuration were changed.

---

### Improved: Failed scheduled recordings now have a Retry button in Scheduled Recordings history

**What you would notice:** When a scheduled recording failed, the Scheduled Recordings > History tab showed the error message but offered no action. To retry the recording, you had to navigate to Downloads > History to find the Retry button there. A Retry button now appears directly on the failed card in Scheduled Recordings > History, so you can re-queue the download without leaving the page.

**What changed:** A Retry button was added to FAILED cards in the Scheduled Recordings history tab. The button only appears on FAILED entries, not on CANCELLED or COMPLETED ones. No backend or scheduling logic was changed.

---

### Fixed: Program guide no longer goes blank when your provider sends incomplete data

**What you would notice:** Some IPTV providers occasionally include empty or incomplete entries in their guide data. Previously, a single bad entry caused Mustarrd to stop loading the program guide for every channel, not just the affected one. Your account would show a red "connection failed" badge even though the provider was reachable. Because the app never recorded a successful sync, it kept retrying and failing on every cycle, leaving the guide empty until you restarted. After this fix, bad entries are quietly skipped and the guide loads all valid channels normally.

**What changed:** The guide import loop now checks each entry before processing it. Null or non-standard entries are skipped without stopping the rest of the import. No user settings or configuration were changed.

---

### Fixed: Program guide now updates correctly for providers that use deflate compression

**What you would notice:** Some IPTV providers compress their guide data using deflate instead of the more common gzip format. If your provider used deflate, Mustarrd would log a parse error, your account badge would turn red, and your guide would slowly go empty as old entries expired. The last-synced time in Settings > Guide would still show the previous successful time, giving no hint that syncs were failing. After this fix, Mustarrd handles deflate-compressed guides transparently, the same way it already handles gzip and plain XML.

**What changed:** The guide decompression step was updated to try two deflate methods when gzip detection does not match. No user settings or configuration were changed.

---

### Improved: Browse EPG shows an error immediately when your provider cannot be reached

**What you would notice:** Opening Browse EPG while your IPTV provider was unreachable used to show an orange loading spinner that never stopped. There was no message and no way to know something was wrong without waiting several minutes. After this fix, the error appears within a second: a red alert saying "Could not reach your provider" with a direct link to Settings, Accounts so you can correct the issue without hunting through menus.

**What changed:** The channels panel in Browse EPG now stops retrying a failed request immediately instead of waiting for three retries to time out. The error message and Settings link were already coded, just hidden behind the loading state. The VOD, series, and search panels already behaved this way; this brings the channels panel in line with them. Frontend only, no logic changes.

---

### Improved: Cancelled downloads now show "Download Again" instead of "Retry"

**What you would notice:** In the Downloads history, a recording you intentionally cancelled used to show a button labelled **Retry**, the same as a recording that failed due to an error. This made it hard to tell at a glance whether something went wrong. Cancelled downloads now show **Download Again** to make it clear the recording stopped because you stopped it, not because something broke. Failed recordings still show **Retry** as before.

**What changed:** A small label change was made to the Downloads history card. No download logic was changed.

---

### Improved: Settings now tells you how to save recordings when ffmpeg is not installed

**What you would notice:** If you are running Mustarrd outside Docker and do not have ffmpeg installed, the warning in Settings > Post-Processing used to say to install ffmpeg manually but did not tell you what to do if you could not. It now adds: "To save recordings without converting, switch the format above to **Keep original (.ts)**." This gives you a working path forward without needing to install anything.

**What changed:** One sentence was added to the ffmpeg unavailable warning in Settings > Post-Processing. No settings values or recording logic were changed.

---

### Fixed: Program guide now shows correct times for Australia, Newfoundland, Moscow, Korea, Singapore, and more

**What you would notice:** If your IPTV provider labels time zones using abbreviations like ACST (Australia Central Standard), NST (Newfoundland), MSK (Moscow), KST (Korea), SGT (Singapore), AST (Atlantic Standard), ADT (Atlantic Daylight), ACDT (Australia Central Daylight), or NDT (Newfoundland Daylight), your program guide was silently treating all of them as UTC. Programs could appear at times that were several hours off, and scheduled recordings would grab the wrong content. After this fix, Mustarrd recognizes these abbreviations and applies the correct offset so guide times match what is actually airing.

**What changed:** Nine timezone abbreviations were added to the list Mustarrd uses when reading guide timestamps. No user-visible setting or configuration was changed.

---

### Fixed: Show titles and descriptions in the program guide now update when your provider corrects them

**What you would notice:** IPTV providers sometimes push placeholder titles like "TBA" or "Upcoming Show" while a program is being scheduled, then replace them with the real name before air time. Previously, once Mustarrd stored a title from the guide, it kept that title forever and ignored any correction on the next guide refresh. This meant you could end up with a recording named after a placeholder title even after the provider fixed it. After this fix, Mustarrd updates the stored title and description whenever the provider sends a newer version.

**What changed:** The part of Mustarrd that imports guide data was updated so that a second import of the same program updates the title and description instead of silently keeping the first version. No user-visible setting or configuration was changed.

---

### Improved: ComSkip configuration fields now appear in Settings whenever ComSkip is installed

**What you would notice:** On a fresh Docker install with ComSkip available, the binary path and INI path fields in Settings > Post-Processing were invisible unless you had already switched to a ComSkip recording format. This was a catch-22: you could not see or adjust the ComSkip paths without first picking a format that required them. After this fix, those fields appear whenever Mustarrd detects the ComSkip binary, regardless of which format you have selected.

A second issue was also fixed: the Recording Format dropdown could appear fully grayed out for a moment when you first opened Settings while the app was still checking whether ffmpeg was installed. All format options are now accessible immediately on page load, with individual options disabled only when the relevant tool is confirmed absent.

**What changed:** Two small display fixes were made to Settings > Post-Processing. No settings values, recordings, or configuration files were changed.

---

### Improved: Failed and cancelled recordings now consistently show "Aired:" in history

**What you would notice:** Failed and cancelled recordings in the Scheduled Recordings history showed "Airs: Yesterday at 7:30 PM" (present tense), as if the recording was still upcoming, while completed recordings already showed "Aired:" correctly. After this fix all terminal recording cards, whether completed, failed, or cancelled, show "Aired:" consistently.

**What changed:** A small display fix was made to the Scheduled Recordings history cards. No recording or scheduling logic was changed.

---

### Fixed: Canceling a download during or just after conversion no longer leaves orphaned files or wrong status

**What you would notice:** Two separate cancellation bugs were fixed in this release.

First: if you canceled a download right after it finished converting, the converted file would land in your completed folder but the Downloads page would show the recording as CANCELLED or make it disappear entirely. There was no way to find the file through the app, and it sat on your drive without Mustarrd knowing about it.

Second: if you canceled a download that had finished downloading but had not started converting yet, the original .ts file was silently left in your download folder. The app showed CANCELLED as expected, but the file stayed on your drive consuming disk space. On Unraid with limited storage this could quietly fill your drive over time.

After this fix, both cases are handled correctly. A recording canceled after conversion is properly marked completed. A recording canceled before conversion cleans up the source file automatically.

**What changed:** Two gaps in the cancellation logic in the download manager were closed. No user-visible settings or configuration were changed.

---

### Improved: Downloads history now shows how long ago a recording aired

**What you would notice:** Download cards in the History, Active, and Upcoming tabs used to show air dates as fixed dates like "Jun 8, 2026 7:30 PM." They now show relative dates the same way the Scheduled Recordings page already did: "Yesterday at 7:30 PM" for recent items, and a short weekday form like "Tue, Nov 14, 2023 at 10:13 PM" for older ones. This matches what you already see on the Scheduled Recordings page and makes it easier to see at a glance how recent a recording is.

**What changed:** A one-line display adjustment was made to Download cards. No download, scheduling, or storage logic was changed.

---

### Fixed: Scheduled recordings now download the correct content for providers in India, Newfoundland, Iran, and Myanmar

**What you would notice:** If your IPTV provider uses a half-hour time zone offset, such as India (+5:30), Newfoundland (-3:30), Iran (+3:30), or Myanmar (+6:30), your scheduled recordings may have been downloading content from the wrong time slot, roughly three to six hours earlier than the program you actually scheduled. The guide showed the correct program time and the recording started on schedule, but when you played the file back, the content was from a different program entirely. This fix was a companion to the guide-time fix released earlier today: the guide was corrected first, and this update applies the same correction to the download itself.

**What changed:** The part of Mustarrd that builds the download URL for scheduled recordings now applies the same time zone normalization that was added to the guide parser earlier. Short time zone offsets like `+5:30` and `+530` are zero-padded before being used to calculate the correct playback position on the provider's server. Previously, those offsets were silently ignored and UTC was used instead. No user-visible setting or configuration was changed.

---

### Fixed: Show titles containing & no longer leave your program guide empty

**What you would notice:** If your IPTV provider lists programs with titles like "R&B Music," "News & Events," or "AT&T Special," Mustarrd would crash while importing the guide and leave your Browse page empty. The error happened because a bare `&` is not valid in the XML format providers use for guide data. After this fix, Mustarrd escapes those characters before parsing so the guide loads correctly. Standard XML codes like `&amp;` and `&#160;` are left unchanged.

**What changed:** Guide data is now scanned for bare `&` characters before the XML parser sees it. Any that are not already part of a valid entity are replaced with the safe `&amp;` equivalent. No user-visible setting or configuration was changed.

---

### Fixed: Program guide times are now correct for providers in India, Newfoundland, Iran, and Myanmar

**What you would notice:** If your IPTV provider is based in a region that uses a half-hour or unusual time zone offset, such as India (+5:30), Newfoundland (-3:30), Iran (+3:30), or Myanmar (+6:30), your program guide may have been showing show times that were off by 30 minutes to over six hours. Scheduled recordings would fire at the wrong time or miss the program entirely. After this fix, Mustarrd correctly reads these time zone offsets from the guide data and stores program times accurately.

**What changed:** The part of Mustarrd that reads incoming guide timestamps now recognizes short time zone formats like `+5:30` and `+530` (with a single-digit hour) and normalizes them before parsing. Previously, these were silently treated as UTC. No user-visible setting or configuration was changed.

---

### Fixed: Mustarrd no longer crashes with a confusing error when the disk fills up while it is off

**What you would notice:** If your disk filled up while Mustarrd was shut down, restarting it would cause pending downloads to immediately fail with a cryptic operating system error about no space left on device. The error was hard to read and did not explain what to do. After this fix, Mustarrd checks available disk space before re-queuing downloads at startup. If there is not enough space, it marks the downloads as failed with a plain message that says the disk is full, so you know what to fix.

**What changed:** The startup recovery process that re-queues interrupted downloads now runs the same free-space check that already exists for new downloads. If space is below the configured minimum, the download is marked failed with a readable message instead of being queued to crash. No user-visible setting or configuration was changed.

---

### Improved: Upcoming recording cards no longer show the duration twice

**What you would notice:** Cards on the Downloads > Upcoming tab and the Scheduled Recordings page showed the program duration twice in a row: once on the "Airs" line and again after "Download starts." The second appearance could be read as "the download itself will take 1 hour," which was confusing. After this change, the duration only appears on the "Airs" line. If you have pre- or post-padding configured, the padded total appears after "Download starts" with a clear "with padding" label so you know what the number means.

**What changed:** A small display adjustment was made to the Upcoming recording cards. No download or scheduling logic was changed.

---

### Fixed: IPTV providers with HTML entities in guide data no longer wipe the program guide

**What you would notice:** Some IPTV providers build their program guide data from web pages and include HTML shortcuts like `&nbsp;` (a special space) or `&eacute;` (the letter e with an accent) in show descriptions. These are valid in HTML but not in the XML format Mustarrd uses to read guide data. When Mustarrd tried to import a guide containing these characters, the XML parser would crash mid-import. If you had "Force Refresh" selected, the old guide was deleted first, and then the crash left you with a completely empty Browse page and no error message. Every automatic guide refresh after that repeated the wipe. After this fix, those HTML characters are converted to a safe form before parsing. The guide loads correctly, descriptions are preserved, and no data is lost.

**What changed:** The part of Mustarrd that reads incoming guide data now automatically escapes HTML-style character codes to safe XML equivalents before the XML parser sees them. Standard XML characters (`&amp;`, `&lt;`, `&gt;`, `&apos;`, `&quot;`) and numeric codes are left unchanged.

---

### Improved: Downloads > Upcoming tab now tells you how to cancel a scheduled recording

**What you would notice:** The Downloads page has an Upcoming tab that shows your scheduled recordings before they run. The cards there were read-only: no buttons, no links, and no explanation of what to do if you wanted to cancel or change one. Users were often confused about where to go. A small line of text now appears at the top of the Upcoming list that reads "To cancel or edit a recording, go to Scheduled Recordings." and includes a clickable link that takes you straight there.

**What changed:** A single hint line with a navigation link was added to the top of the Upcoming recordings list. No download or scheduling logic was changed.

---

### Improved: Scheduled Recordings history now has a status filter

**What you would notice:** The Scheduled Recordings history tab showed all recordings mixed together with no way to narrow the list. The Downloads history tab already had a status filter, but Scheduled did not. A filter dropdown now appears at the top of the Scheduled history tab. You can select All, Completed, Failed, or Cancelled to see only the recordings you care about. If no recordings match the selected filter, a plain message explains why the list is empty.

**What changed:** A status filter dropdown was added to the Scheduled Recordings history tab. No scheduling or backend logic was changed.

---

### Fixed: Scheduled recordings on Movies or Sports channels now get the correct filename

**What you would notice:** When you scheduled a recording on a channel categorized as Movies or Sports and left the filename field blank, the downloaded file got the wrong name. A movie called "The Dark Knight" on a Movies channel would be named like `The Dark Knight - 2024-01-15.mkv` instead of `The Dark Knight (2008).mkv`, because Mustarrd lost track of the channel's category between when you scheduled the recording and when it actually ran. This fix ensures the channel category is saved when you create the schedule and used when the recording fires.

**What changed:** The scheduled recording now saves the channel category (such as "Movies" or "Sports") when the schedule is created. When the scheduler runs the recording, it passes that saved category to the filename generator so the file is named correctly. No user-visible setting or config was changed. A regression test was added.

---

### Fixed: Program guide no longer goes empty for providers that use date-style timestamps

**What you would notice:** If your IPTV provider sends program guide data using date-formatted timestamps like `2024-01-15 20:00:00 +0100` instead of the compact numeric format like `20240115200000 +0100`, your entire Browse page would be silently empty after every guide refresh. No error appeared in the logs or the UI. After this fix, both timestamp formats are handled correctly and the program guide populates as expected.

**What changed:** The part of Mustarrd that reads incoming guide data now recognizes ISO-style date timestamps (with dashes, a space separator, or a `T` separator) and converts them to the format Mustarrd uses internally. The existing compact-format path is unchanged.

---

### Fixed: Scheduling a recording on a channel without catchup support now marks it as Failed right away

**What you would notice:** If you scheduled a recording on a channel that does not support catchup (some channels in your provider's list have it disabled), Mustarrd used to queue the download anyway. The download would start, hit a provider error, and fail with no explanation. After this fix, Mustarrd detects at schedule time that the channel does not support catchup and marks the recording as Failed immediately with a plain-English message, so you know right away rather than waiting for a silent download failure.

A second related fix: if Mustarrd could not reach your provider during a scheduled check, it would previously dispatch downloads with a default 30-day window regardless, causing failures for every pending recording. Schedules now stay in Scheduled state and retry on the next poll when the provider is temporarily unreachable.

**What changed:** The scheduler now raises a specific error when a channel has no catchup support instead of falling back to a 30-day window. Schedules for no-catchup channels are marked Failed with a clear message. Provider connection errors leave the schedule in Scheduled state for automatic retry. Backend only, no frontend changes.

---

## 2026-06-08

### Improved: Settings now shows when your program guide last synced, with a Refresh Now button

**What you would notice:** There was no way to tell from Settings whether your program guide data was fresh or stale. You had to go to Settings > Accounts and use the Force EPG Refresh button there. A new Guide section is now available in Settings, between File Naming and Appearance. It shows the date and time your guide last synced (for example, "Last synced: Today at 10:30 PM") and a Refresh Now button you can click without leaving Settings. If the guide has never synced, it says "Guide not yet synced." The button shows a spinner while running and cannot be double-clicked.

**What changed:** A new Guide section was added to the Settings sidebar. It uses the same data already available to the Accounts page, so no backend logic was changed. The change is purely in the settings interface.

---

### Fixed: ComSkip settings panel now appears correctly after the setup wizard runs

**What you would notice:** If you run Mustarrd on Docker and ComSkip is installed, the setup wizard recommends and applies the "MKV container + skip commercials" recording profile. After finishing the wizard and opening Settings > Post-Processing, the dropdown showed "MKV container (fast, no re-encode)" with no ComSkip settings below it. There was no way to see or change the ComSkip binary path or configuration file path. Worse, saving any setting in that state silently switched your recording profile from the fast stream-copy method to a slower full re-encode, which uses more CPU and takes longer. After this fix, the correct profile label appears in the dropdown, the ComSkip binary and configuration file fields are visible, and saving settings preserves the fast stream-copy profile exactly as the wizard set it.

**What changed:** Two bugs were fixed. In the frontend, the recording format detection logic checked the wrong condition first, so a valid "fast stream-copy + commercial removal" state was silently mapped to the plain MKV option. In the backend, settings save incorrectly forced the fast stream-copy flag off whenever ComSkip was enabled, overwriting the profile on every save. Both are now corrected. A regression test was added to verify that the fast ComSkip profile survives a settings save without being changed.

---

### Improved: Edit Account button now appears directly on unreachable provider cards

**What you would notice:** When an IPTV account shows as unreachable on the Accounts settings page, the next step is almost always to fix the server URL or credentials. Previously the only way to do that was to open the three-dot menu on the card and find the Edit option, which is easy to miss. An "Edit Account" button now appears directly below the error message on any unreachable provider card, so you can correct the details right away without hunting for a menu.

**What changed:** A direct Edit Account button was added to unreachable provider cards on the Accounts settings page. No account or connection logic was changed.

---

### Improved: Provider error on failed recordings now links directly to Account Settings

**What you would notice:** When a recording failed because Mustarrd could not reach your IPTV provider, the error message said "Cannot reach the provider. Check the server URL in your account settings." The phrase "your account settings" was plain text with no way to click it. You had to know where to go on your own. That phrase is now a clickable link that takes you directly to Settings > Accounts, where you can correct the server URL or credentials. The same error on the Browse page already had this link; now the Downloads and Scheduled history pages match it.

**What changed:** Two frontend files were updated so the provider-unreachable error message includes a navigation link. No download logic or backend code was changed.

---

### Fixed: Expired scheduled recordings now correctly show as Failed instead of staying Scheduled forever

**What you would notice:** If you had a scheduled recording whose catchup window had already passed when the scheduler checked it (for example, you scheduled something on a provider that only keeps 7 days of catchup, and 8 days had gone by), the recording would remain stuck showing "Scheduled" in the UI forever. Every 30 seconds the app would silently try and fail to process it, and the status never changed. After this fix, expired recordings correctly update to "Failed" so you can see at a glance that the program is no longer available for download.

**What changed:** When the scheduler processed expired recordings, it correctly marked them as Failed in memory, but then returned early without saving that status to the database. One line was added to save the status before the early return. No scheduling logic was changed.

---

### Fixed: Deleting a user now immediately cancels their scheduled recordings and active downloads

**What you would notice:** When an admin deleted a user from the Users page, that user's scheduled recordings would continue to fire (creating new downloads attributed to the deleted account) and any downloads already in progress would keep running, consuming disk space and IPTV stream slots. After this fix, deleting a user immediately cancels all their pending schedules and stops any active downloads. Their browser session is also disconnected.

**What changed:** The admin user-delete action now cancels scheduled recordings, stops active downloads, and closes open browser connections for the deleted user before removing the account. Previously this cleanup only ran when a user deleted their own account. The behavior now matches the existing self-delete path.

---

### Fixed: Disabling a Plex user now actually blocks them from logging in

**What you would notice:** If you disabled a Plex-linked user on the Users page, that user could still complete a Plex sign-in and get full access to Mustarrd again. The disabled status was silently overwritten by the login and the account was re-enabled. After this fix, a disabled user who attempts to sign in with Plex gets a "Your account has been disabled" error and cannot access Mustarrd until an admin re-enables them.

**What changed:** The Plex sign-in endpoint now checks whether the user account is disabled before granting a session. Previously it set every returning Plex user's status to "active" unconditionally, which undid any admin disable action. The credentials (username and password) login path already had this check; the Plex login path now matches it.

---

### Improved: Retry button now appears directly on failed and cancelled download cards

**What you would notice:** When a download failed or was cancelled, the only way to retry it was to open the three-dot action menu on the card and find the Retry option. That menu is easy to miss, especially on a phone. The Retry button now appears directly on the card itself, right alongside the other action buttons, so you can act immediately without hunting through a menu.

**What changed:** A Retry button was added inline to failed and cancelled download cards in the History tab. No download logic was changed. The button uses the same retry action that was already available in the menu.

---

### Fixed: Series episode downloads with very long titles no longer fail on Linux

**What you would notice:** If you downloaded a series episode where the combined length of the show name and episode title was very long (more than about 200 characters, most common with Korean, Japanese, or Chinese titles), the download would fail silently with no clear explanation. The fix in a previous release capped each part individually, but the combined result could still be too long when both parts were at their individual limits. After this fix, the combined filename is always trimmed to fit, and the download completes normally.

**What changed:** After assembling the show name and episode title into a single filename, the result is now trimmed to 200 UTF-8 bytes before the file extension is added. Each part was already capped individually, but the assembled combination was never checked. This means a show and episode both with 200-byte names no longer produce a 400-byte filename that Linux refuses to write.

---

### Fixed: EPG guide no longer goes stale with large IPTV providers

**What you would notice:** If your IPTV provider serves a large program guide file (common with providers that carry many channels), Mustarrd would silently fail to refresh the guide and show stale or missing program listings with no explanation. After this fix, Mustarrd waits up to 5 minutes for the guide download to complete, matching the extra time large files need on a slow connection or a busy provider.

**What changed:** The timeout for downloading the XMLTV program guide file was raised from 30 seconds to 300 seconds. Large providers routinely serve guide files of 20 to 200 MB, which can take longer than 30 seconds on a slow or loaded connection. The new limit matches the timeout already used for other large downloads in Mustarrd. Most users will not notice any difference; users whose guide was silently failing will now see it refresh correctly.

---

### Fixed: File Naming settings no longer say ".ts extension" for MP4 and MKV users

**What you would notice:** Under Settings > File Naming, the description said "Files get the .ts extension automatically." That was only correct for users recording in the default TS format. If you had set your Recording Format to MP4 or MKV, the description was simply wrong: your files were getting .mp4 or .mkv extensions, not .ts. The description now correctly explains that the extension is controlled by your Recording Format setting, and that you should not add an extension to your file naming template.

**What changed:** One sentence in the File Naming settings description was reworded to accurately describe how file extensions work. No recording logic or file handling was changed.

---

### Fixed: ComSkip now correctly cuts all commercials when EDL segments overlap

**What you would notice:** If you use commercial removal and your provider's ComSkip EDL file contained one commercial block fully inside another (overlapping segments), a slice of commercial video would slip through into the final recording. The recording would look complete but still contain ads. After this fix, overlapping commercial segments are handled correctly and all commercial content is removed.

**What changed:** The logic that converts commercial markers into keep-segments was advancing its position backward when it encountered a contained segment, causing the outer commercial's tail to be included in the output. The fix ensures the position always moves forward, so no commercial content is ever included between contained segments. Most users will not notice any difference; users who had this specific issue will get cleaner recordings.

---

### Improved: File Naming settings now show real example filenames

**What you would notice:** Under Settings > File Naming, each template section used to show the raw template syntax as an example, such as `{show} - S{season:02d}E{episode:02d} - {title}`. That text was identical to what was already in the input field above it and gave no useful information, especially to users who do not know what `{season:02d}` means. The example line now shows what a real downloaded file would actually be named, such as `Breaking Bad - S01E05 - Gray Matter` for TV shows or `Inception (2010)` for movies. You can see exactly what your filename settings will produce before saving anything.

**What changed:** The backend now generates a sample filename using placeholder data and includes it in the settings response. The frontend displays this rendered example instead of the raw template text. No template logic was changed, and no existing filenames are affected.

---

### Improved: Download start lines are easier to read on mobile

**What you would notice:** On a phone, Scheduled and Downloads Upcoming cards showed text like "Download starts: Wednesday at 9:30 PM (1h 30m recording)". On narrow screens, the word "recording" would wrap onto its own line, making the card look misaligned. Since the label "Download starts:" already tells you a recording is happening, the word "recording" was redundant. Cards now show "Download starts: Wednesday at 9:30 PM (1h 30m)" and fit cleanly on one line on all phone sizes.

**What changed:** The word "recording" was removed from the download duration text in the Scheduled Upcoming and Downloads Upcoming card components. No other text or behavior was changed.

---

### Fixed: Cancelling a download at the last moment no longer deletes the completed file

**What you would notice:** In a very narrow timing window, pressing Cancel on a download that had just finished could permanently delete the completed recording from your folder and show it as Cancelled. The recording would be gone with no way to recover it. After this fix, if a download has already finished and moved to your completed recordings folder, pressing Cancel at that moment has no effect: the recording stays in place and shows as Completed.

**What changed:** The cancel handler in the download manager now checks whether a download is already marked as completed before taking any action. If it is, the handler stops without touching the file. Previously this check was missing, so the handler would delete whatever file it found at the download path, which by that point was the completed recording in your recordings folder.

---

### Fixed: Long show and episode titles no longer cause series downloads to fail

**What you would notice:** If you downloaded a series episode where both the show name and the episode title were very long (most common with Korean, Japanese, or Chinese content), the download would fail with no clear explanation. The app would mark it as Failed and give no actionable message. After this fix, the combined filename is automatically trimmed to fit within the filesystem's filename length limit, and the download completes normally.

**What changed:** Each part of a series filename (show name and episode title) was already capped individually, but the combined result was never checked. Two long parts together could exceed the 255-character Linux filename limit. A trim step was added after combining the two parts so the resulting filename always fits. Your show and episode names are preserved as fully as possible; only characters beyond the limit are removed.

---

### Fixed: ComSkip failure message now correctly says your raw recording is still on disk

**What you would notice:** When ComSkip failed during post-processing, Mustarrd showed an error message that said "Recording not saved." That wording made it sound like your recording had been deleted. In fact, the original raw recording file was never removed and was still sitting in your downloads folder the whole time. The message now correctly says "The raw recording is still in the download folder and was not deleted," so you know your file is safe and can rescue it if needed.

**What changed:** The error message shown when ComSkip fails was reworded to accurately describe what happens: the raw recording stays in the download folder and is not deleted. Previously, the message said "not saved," which was misleading and caused some operators to queue a re-download, overwriting the intact file they already had.

---

### Fixed: Cancelling a download immediately after queuing it no longer leaves it stuck as Pending

**What you would notice:** If you cancelled a download within a very short window after queueing it, the download could get stuck showing as Pending forever. It had no active task driving it forward and no way to recover without retrying or restarting the app. After this fix, downloads cancelled at any point, including that brief window right after queuing, correctly move to Cancelled and any partial file is cleaned up.

**What changed:** A rare timing issue in the download manager was fixed. If a cancellation arrived at exactly the moment the download task was starting up and had not yet read the download record from the database, the cancellation handler would fail silently and leave the download in Pending state. The fix ensures the handler can always fetch the record it needs, regardless of when the cancellation arrives.

---

### Fixed: Commercial removal now fails clearly when ffprobe is not installed

**What you would notice:** If you had commercial removal enabled and ffprobe was not installed alongside ffmpeg, Mustarrd would silently mark the download as completed even though none of the commercials had actually been cut. The file looked done but still contained every ad break. After this fix, if ffprobe is missing or cannot read the file duration, the download is marked as failed with a message telling you to install ffprobe alongside ffmpeg.

**What changed:** The commercial removal step now checks the file duration before it does any work. If ffprobe is unavailable or returns an invalid duration, the process stops immediately and marks the download as failed with a clear error message. Previously, a missing ffprobe caused the duration to come back as zero, which silently produced the original file unchanged.

---

### Improved: Accounts heading on mobile now appears above the buttons

**What you would notice:** On a phone, the Settings > Accounts page was showing two action buttons at the top with the "Accounts" heading below them. With no label above the buttons, the page looked broken and it was not obvious what screen you were on. The heading now appears at the top where it belongs, with the buttons underneath.

**What changed:** The layout of the Accounts page header was updated so that on narrow screens the heading always renders first and the action buttons appear below it. On desktop, the layout is unchanged.

---

### Improved: Browse EPG right panel now shows a message when your provider is unreachable

**What you would notice:** On desktop, the Browse EPG page has a two-column layout: channels on the left, the program guide on the right. When your IPTV provider cannot be reached, the right panel was completely blank. This could look like a display glitch or a broken page with no hint of what was wrong. The right panel now shows a TV icon and the message "Channel guide will appear here once your provider is connected." so you can tell immediately that the empty space is intentional and not a bug.

**What changed:** A placeholder message and icon were added to the right panel of the Browse EPG desktop layout. When the provider is unreachable, the panel now shows the message instead of an empty box. No backend changes were made.

---

### Fixed: Providers using compact XMLTV timestamps now have their guide loaded correctly

**What you would notice:** Some IPTV providers write timestamps in their program guide files in a compact 12-character format with the timezone offset written directly after the time, like `202311152000+0200`, with no space before the `+`. If your provider used this format, every program on every channel would be silently dropped during each guide refresh. Browse and Catchup would show nothing, with no error message to explain why.

**What changed:** One character was added to the timestamp parser in the program guide importer. The parser now correctly identifies when a timezone offset starts within the 14-character slice it was examining and routes those timestamps to the right handling branch. Programs that were previously dropped now import correctly with the right UTC time.

---

### Fixed: Program guide data is no longer lost when a provider sends a broken or cut-off guide file

**What you would notice:** When your IPTV provider's program guide file was corrupted, partially downloaded, or cut off mid-file, Mustarrd would silently discard all the programs it had already successfully read before hitting the bad part. Your guide would show gaps for affected channels even though Mustarrd had already parsed that data correctly. After this fix, any programs read before the broken section are saved. The server log records a warning so you can see that the file was incomplete.

**What changed:** The program guide importer now catches the error that occurs when an XMLTV file is truncated or contains invalid characters mid-way through. When that happens, parsing stops cleanly and everything collected up to that point is written to the database. Previously, the error caused all buffered data to be discarded without any warning.

---

### Improved: Upcoming recording cards now show the day of the week instead of a full date

**What you would notice:** Recording cards in Scheduled > Upcoming and Downloads > Upcoming that previously showed a full date like "Wed, Jun 10, 2026 at 8:00 PM" for shows airing within the next week now show "Wednesday at 8:00 PM" instead. This is easier to read at a glance and avoids a layout issue on mobile where the old format could cause the time to wrap onto its own line.

**What changed:** A small addition was made to the date formatting helper in the frontend. Dates within 2 to 6 days from now show the weekday name. Today, Tomorrow, and dates more than 6 days out are unchanged.

---

### Improved: Scheduled Upcoming now lists recordings in the order they will air

**What you would notice:** The Scheduled Recordings > Upcoming tab was showing your recordings in reverse order. The recording furthest away appeared at the top, and the one airing soonest appeared at the bottom. If you had three recordings scheduled, you had to scroll to the bottom to see what was recording next. The list now sorts soonest first, matching the order already used on the Downloads > Upcoming tab.

**What changed:** A one-line sort was added to the Scheduled Upcoming list in the frontend. No backend changes were made.

---

### Fixed: Downloading multiple untitled bonus episodes no longer overwrites earlier files

**What you would notice:** If your provider offered several bonus episodes or extras in a season, all labeled episode 0 with no episode title, Mustarrd would save them all to the same filename. Each new download silently replaced the previous one, so you could end up with only the last episode downloaded and no indication that anything was lost. After this fix, each episode gets a unique filename based on an internal ID, so all of them are saved correctly.

**What changed:** The filename generator now uses a unique episode ID as a tiebreaker for any untitled episode numbered zero, regardless of which season it is in. Previously, this tiebreaker only applied when both the season and episode number were zero.

---

### Improved: Downloads Upcoming tab now has a Go to Browse button when empty

**What you would notice:** When you have no upcoming recordings scheduled, the Downloads Upcoming tab used to show a message saying to go to Browse EPG to find something to record, but there was no button to take you there. You had to navigate there yourself. A Go to Browse button now appears on that empty state, matching the button already shown on the Active and Scheduled pages when they are empty.

**What changed:** A Go to Browse button was added to the Downloads Upcoming empty state. No backend changes were made.

---

### Fixed: Program guide now loads correctly for providers that use a namespace in their XMLTV feed

**What you would notice:** Some IPTV providers include a specific XML marker called a namespace at the top of their program guide file. If your provider used one, Mustarrd would silently import zero programs. The Browse and Catchup pages would appear completely empty with no error message shown anywhere. After this fix, the guide loads normally for all providers regardless of whether their XMLTV file includes a namespace.

**What changed:** Mustarrd's program guide parser now strips namespace prefixes from tag names before processing them, so it correctly identifies channels and programs even when the provider's XMLTV file includes an XML namespace declaration. No changes were made to how downloads or recordings work.

---

### Fixed: Scheduled recordings now fire at the correct time for providers that label timezones by name

**What you would notice:** Some IPTV providers write timezone abbreviations such as EST, PST, CET, or BST directly in their program guide timestamps instead of using a numeric offset. Mustarrd was treating all of those as UTC, storing the time wrong by the full offset of the timezone. EST programs were off by 5 hours; UK summer (BST) programs were off by 1 hour. A recording scheduled for 8:00 PM EST would actually fire at 1:00 AM the next day. After this fix, named timezone abbreviations are converted to the correct UTC time before storing, so scheduled recordings target the right content.

**What changed:** The program guide time parser now recognises common named timezone abbreviations (EST, EDT, CST, CDT, MST, MDT, PST, PDT, CET, CEST, BST, GMT, and others) and applies the correct UTC offset for each. Previously, any named timezone abbreviation that was not a numeric offset was silently stored as UTC. Recordings already scheduled before this update are not affected.

---

### Improved: Browse EPG right panel is now fully blank when the provider is unreachable

**What you would notice:** When your IPTV provider cannot be reached, Browse EPG shows a connection error in the left panel. After a fix earlier today, the "Select a channel to view its EPG" placeholder text was already hidden in that state. A camera icon and "No channel selected" header in the right panel were still visible, which could suggest that selecting a channel was possible when it was not. Both the icon and header are now hidden in error state. The right panel is completely blank, keeping your attention on the connection error and the link to Settings.

**What changed:** A one-line guard was added to the Browse EPG right panel so the header row does not render when the provider is known to be unreachable. No backend changes were made.

---

### Fixed: Disabling a user now immediately ends their live downloads feed

**What you would notice:** Before this fix, an admin could disable a user account on the Users page, but if that user had the Downloads page open, their real-time progress feed stayed active for the rest of their session. They would continue seeing live download updates even though their account had been disabled. After this fix, disabling a user closes their connection immediately. Their live feed stops as soon as the admin saves the change.

**What changed:** When an admin disables a user, Mustarrd now closes all of that user's active WebSocket connections straight away. Previously, the connection was only checked the next time the user tried to do something. No changes were made to how downloads run or how recordings are stored.

---

### Improved: Browse EPG no longer shows a confusing prompt when the provider is unreachable

**What you would notice:** On the Browse EPG page, when a provider cannot be reached, the left panel shows a connection error. Previously, the right panel simultaneously displayed "Select a channel to view its EPG", even though no channels are available in that state. The two messages contradicted each other and could confuse a non-technical user into wondering why they could not select anything. The right panel is now empty in this state. Only the actionable error in the left panel is shown.

**What changed:** A small guard was added to the Browse EPG layout. When the provider is known to be unreachable and no channel is selected, the empty-state prompt is suppressed. No backend changes were made.

---

### Fixed: Scheduled History now shows why a recording failed

**What you would notice:** In Scheduled > History, a recording that failed used to show only a red "Failed" badge with no explanation. To find out what went wrong, you had to navigate to Downloads > History and manually match the entry by channel and time. The failure reason now appears directly on the Scheduled History card, for example: "Provider returned an error page. The catchup window may have expired or be unavailable."

**What changed:** The Scheduled History card now shows the download failure message alongside the "Failed" badge. No changes were made to how recordings are stored or processed.

---

### Improved: Settings > Accounts button order corrected on mobile

**What you would notice:** On a phone, the Settings > Accounts page used to show "Force EPG Refresh" above "+ Add Another Account". The more important action was below the less important one. The order is now corrected on narrow screens: "+ Add Another Account" appears first and "Force EPG Refresh" appears below it. The layout on desktop is unchanged.

**What changed:** When the two buttons stack on narrow screens, the column order is reversed so the primary action appears at the top. No backend changes were made.

---

### Fixed: Retrying a failed download now checks disk space first

**What you would notice:** If a download failed because the disk was full and you clicked Retry, Mustarrd used to start the download again immediately with no disk check. It would hit the same full-disk condition and fail again, with no clear explanation. Clicking Retry now returns the same "Not enough disk space" message you would see if you tried to start a fresh download. The problem is obvious and you know to free space before retrying.

**What changed:** The Retry action on a failed download now checks available disk space before re-queuing, the same check that already ran when you first requested the download. If the disk is below the configured free space minimum, Mustarrd returns a "Not enough disk space" message instead of silently restarting a download that will fail again.

---

### Improved: Settings no longer shows a loading badge alongside a connection error

**What you would notice:** On the Settings > Accounts page, when a provider could not be reached, the account card showed both a red "Unreachable" status and a blue "Catchup: loading..." badge at the same time. The two indicators appeared to contradict each other. The loading badge now only appears when the provider is reachable. If the provider status is already known to be an error, the card shows only the error state.

**What changed:** A small guard was added so the loading badge only appears when the provider has not been flagged as unreachable. No backend changes were made.

---

### Improved: Downloads Upcoming cards now show when a show ends and when the download begins

**What you would notice:** On the Downloads > Upcoming tab, each recording card previously showed only the show's start time. The card now shows the full air window, for example "Airs: Today at 7:30 PM - 8:00 PM (30m)", and a separate line showing when Mustarrd will begin downloading, for example "Download starts: Today at 8:00 PM (30m recording)". This makes it easy to see at a glance whether a recording fits your schedule and how long the download will run. The Scheduled Recordings page already showed this information. The Downloads Upcoming tab now matches it.

**What changed:** The Upcoming recording cards were updated to display the air end time and the download start time alongside the air start time. No recording logic was changed.

---

### Fixed: Sending a program request with a numeric timestamp no longer causes a server error in all cases

**What you would notice:** No visible change during normal use. A previous update fixed server errors when a third-party app or script sent a number instead of a text date for a program's start or end time. This update extends that fix to cover an additional path that the earlier change missed, so the same type of invalid input now returns a clear error message in all cases.

**What changed:** The file naming step that runs just before a download starts was not covered by the earlier fix. It now handles integer timestamps correctly instead of crashing with an internal server error.

---

### Improved: Yesterday's recordings now display as "Yesterday" instead of a full date

**What you would notice:** On the Scheduled page, cancelled or completed recordings from yesterday used to show a full weekday-and-date label like "Sat, Jun 7, 2026 at 7:30 PM". They now show "Yesterday at 7:30 PM", matching the same natural style already used for upcoming recordings that air "Today" or "Tomorrow". Recordings from more than two days ago continue to show the full date.

**What changed:** The date label used on history cards was extended to recognise the previous day and display "Yesterday" instead of the full date. No backend changes were made.

---

### Fixed: Long-running Mustarrd instances no longer slowly accumulate memory over time

**What you would notice:** No visible change during normal use. On a Mustarrd instance that has been running for weeks or months and processed many recordings, a small amount of memory was silently retained for each completed, failed, or cancelled download and never released. Over a very long time this could cause memory usage to creep upward on memory-limited servers such as Unraid systems with 8 GB or less.

**What changed:** An internal table that tracks which user started each download was not cleared when a download finished. That entry is now removed as soon as the download reaches a final state (completed, failed, or cancelled). No user-visible behaviour changes.

---

### Fixed: Sending a program request with a number instead of a text date no longer causes a server error

**What you would notice:** No visible change during normal use. Previously, if a third-party app or script sent a request to schedule or download a program and used a number (for example `1700000000`) instead of a text date (for example `"2023-11-14T22:13:20"`) for the start or end time, Mustarrd would respond with a generic server error. It now returns a clear "invalid input" message telling the caller the data was in the wrong format.

**What changed:** The date-parsing code now checks that start and end times are text strings before trying to read them as dates. A number or other non-text value now produces a proper "400 Bad Request" response instead of an unhandled crash.

---

### Fixed: Series episodes with no season or episode information no longer overwrite each other

**What you would notice:** Some IPTV providers send series episodes without any season number, episode number, or episode title. Mustarrd would save every such episode from the same show to the same filename (for example `Season 00/S00E00 - My Show.mkv`). Downloading several such episodes would leave only one file on disk because each new download overwrote the previous one, with no error or warning.

**What changed:** When a series episode has no season, episode, or title information, Mustarrd now adds the provider's internal episode ID to the filename to make each file unique. All episodes are saved separately and no longer overwrite each other.

---

### Fixed: Downloading the same movie or series episode twice no longer corrupts the recording

**What you would notice:** If you clicked Download twice on the same movie or series episode, or if two requests arrived at the same moment, Mustarrd would start both downloads and write to the same file at the same time. The recording would appear as completed but would often be corrupted and unplayable.

**What changed:** If a download for a particular file is already active, any further request to download the same item is now rejected with a clear message. Only one download at a time can write to any given file.

---

### Fixed: Settings no longer accepts values that could cause Mustarrd to open thousands of connections at once

**What you would notice:** If "Max concurrent downloads" was set to a very large number (for example 10,000 in the Settings page), Mustarrd would attempt to run that many downloads simultaneously. This could exhaust the available network connections on your server and cause the download process to crash until Mustarrd was restarted.

**What changed:** "Max concurrent downloads" is now capped at 50, and "Max concurrent post-processing jobs" is limited to between 1 and 20. Both values are well above what any home server requires. The defaults remain 2 and 1.

---

### Improved: Upcoming recordings on the Downloads page now show a status badge

**What you would notice:** Cards on the Downloads Upcoming tab had no status indicator. You had to visit the Scheduled Recordings page to see whether a recording was confirmed, queued, or paused due to low disk space.

**What changed:** Each card on the Downloads Upcoming tab now shows the same status badge (Scheduled, Queued, Paused (Low Space), and so on) that already appeared on the Scheduled Recordings page. No backend changes were made.

---

### Fixed: Retrying a recording no longer allows a duplicate schedule to sneak through while the retry is in progress

**What you would notice:** If you retried a failed or cancelled recording and then tried to schedule the same program again before the retry finished, Mustarrd would accept the second schedule request as if the first were not running. Both downloads would write to the same output file at the same time, corrupting it. Mustarrd now correctly sees the retry as an active schedule and rejects the duplicate.

**What changed:** When a download is retried, Mustarrd now immediately marks the linked scheduled recording back to "queued" status. The guard that blocks duplicate schedules checks that status, so any further attempt to schedule the same program while the retry is in progress is correctly refused.

---

### Improved: The low disk space banner now shows the minimum threshold so you know exactly why recordings are paused

**What you would notice:** Before this change, the banner said something like "870.5 GB free" with no explanation of why recordings were stopped. If your drive is large, that number alone is confusing. The banner now reads "870.5 GB free (25 GB minimum)" so you can see at a glance what the threshold is and why Mustarrd paused new recordings.

**What changed:** The low disk space banner now includes the configured minimum free space in parentheses alongside the current free space. The minimum is set in Settings under Recording. No backend changes were made.

---

### Fixed: Mustarrd no longer corrupts recordings when your provider schedules the same show twice with slightly different start times

**What you would notice:** Some IPTV providers serve the same show twice in the program guide with start times that differ by a second or two. Mustarrd's duplicate check compares timestamps exactly, so it would see those as two different programs and schedule two separate downloads for the same show. Both downloads would write to the same output file at the same time, producing a corrupted or empty recording. Mustarrd now catches this before the second download starts, marks it as a duplicate, and lets the original recording finish normally.

**What changed:** Before queuing a new download, Mustarrd now checks whether any active download is already writing to the same output file. If a conflict is found, the new request is rejected with a clear message instead of racing against the existing download.

---

### Improved: Cancelled and failed recordings in Scheduled History now say "Aired" and no longer show a "Download starts" line

**What you would notice:** On the Scheduled page, the History tab lists cancelled and failed recording attempts. Previously those cards said "Airs: Jun 7, 2026 7:59 PM" and showed a "Download starts:" line, even though the recording was already in the past and no download ever ran. The air time label now reads "Aired:" to match the historical context, and the "Download starts" line is hidden for cancelled and failed entries where no download happened.

**What changed:** The air time label on history cards was updated to past tense for cancelled and failed entries. The "Download starts" line is now hidden for those same entries. Upcoming and active scheduled recordings are unchanged and still show "Airs:" and "Download starts:". No backend changes were made.

---

### Fixed: Pressing the EPG Refresh button twice quickly no longer starts two separate guide refreshes

**What you would notice:** If you clicked "Refresh EPG" twice in quick succession, or if a network retry happened to fire at the same moment as your click, Mustarrd would run a full program guide refresh twice back to back. This doubled the time the refresh took and put unnecessary extra load on your IPTV provider. The second request is now blocked and only one refresh runs.

**What changed:** A busy flag is now set the moment a refresh is queued. Any further request that arrives while that flag is set is turned away immediately. The flag clears once the refresh task actually starts running.

---

### Fixed: Program guide is no longer wiped when your provider returns an empty response during a force-refresh

**What you would notice:** If your IPTV provider briefly returned nothing when Mustarrd triggered a force-refresh of the program guide, the entire guide would be deleted and stay empty until the next automatic scheduled refresh, which could be up to 8 hours away. The existing guide is now left untouched when the provider returns an empty response.

**What changed:** The force-refresh now checks that the response actually contains program data before deleting the existing guide. If the response is empty, a warning is logged and your current guide is preserved.

---

### Improved: Status badges on scheduled recordings no longer push show titles off the screen on phones

**What you would notice:** On the Scheduled page, each recording card shows a status badge (such as "Scheduled" or "Paused (Low Space)"). Previously that badge shared the top-right corner of the card with the menu button, and on phones a long show title like "The Great British Bake Off" had no room and was cut off with "..." making it impossible to read. The badge now sits below the channel name on the left side of the card, and the menu button is alone at the top-right. Show titles now have full width and are no longer cut short.

**What changed:** The badge was moved from beside the menu button to below the channel name. No backend changes were made.

---

### Improved: The Downloads page tabs no longer wrap to a second row on phones

**What you would notice:** On a phone, the Downloads page shows three tabs: Active, Upcoming, and History. Previously the History tab could fall onto a second row, making the page look broken and hiding the fact that all three sections exist. All three tabs now stay on a single row and share the available width evenly.

**What changed:** The tab bar on the Downloads page was updated to stretch tabs evenly across the full width and prevent wrapping. No backend changes were made.

---

### Improved: The "Paused (Low Space)" badge on upcoming recordings no longer pushes the air time to a second line

**What you would notice:** On the Downloads page, scheduled recordings that are paused because disk space is low show a yellow "Paused (Low Space)" badge. Previously that badge sat next to the air time text, and on smaller screens or phones the two would compete for space, pushing the air time onto a second line mid-sentence. The badge now sits in the title row next to the show name, and the air time always has its own line below with room to display fully.

**What changed:** The layout of upcoming recording cards in Downloads was restructured so the badge shares a row with the show title, and the air time sits in a separate row underneath. No backend changes were made.

---

### Improved: Completed recordings in Downloads History now show the download size on a separate labeled line

**What you would notice:** Previously, a completed recording showed its file size next to the filename with a dot separator, which was easy to miss and could be confusing for transcoded recordings where the download size and the final file on disk differ in size. The download size is now shown on its own line below the filename, with a clear "Download size: 1.4 GB" label so you know exactly what you are looking at. Entries with no reported size (chunked streams, older records, or failed downloads) show only the filename, with no spurious "0 B" line.

**What changed:** The completed recording card in Downloads > History was updated to display the filename and download size as two separate lines. The label "Download size:" makes clear that this is the size reported by your provider during the download, not necessarily the size of the final processed file. No backend changes were made.

---

### Fixed: Commercial removal no longer fails for shows with apostrophes in the title

**What you would notice:** Shows whose names include an apostrophe (Father's Day, New Year's Eve, Britain's Got Talent, It's a Wonderful Life) were failing during the commercial-removal step. The download would finish, but the recording would be marked as failed or warning with no output file produced, and the log would show a file-not-found error from ffmpeg. This is now fixed.

**What changed:** The file path formatter used when combining recording segments before running ComSkip was using the wrong quoting style for apostrophes. ffmpeg's concat list format uses its own quoting rules, not standard shell quoting. The formatter was corrected to use ffmpeg's expected style.

---

### Fixed: Original .ts file is now deleted when "delete after transcode" is on and a remux falls back to re-encode

**What you would notice:** If you have "delete original after transcode" enabled and a recording could not be remuxed (converting the container format without re-encoding the video), Mustarrd would fall back to a full re-encode. The re-encoded file would land in your completed folder correctly, but the original .ts file would remain in your downloads folder instead of being deleted. This is now fixed.

**What changed:** The fallback re-encode path was missing the step that removes the original file when the "delete original" setting is on. The deletion now runs correctly after a successful re-encode following a remux failure.

---

### Fixed: Only admins can now trigger a manual EPG guide refresh

**What you would notice:** On a shared Mustarrd instance where Plex login is enabled or multiple users have accounts, any logged-in user could previously trigger a full EPG refresh. This could hammer your IPTV provider repeatedly if a user or an app did it in a loop, risking rate limits that block normal downloads. This is now limited to admin accounts.

**What changed:** The "Force Refresh EPG" button now requires an admin session. Regular users and Plex-provisioned accounts will get a permission error if they try to trigger it. Browsing the channel guide, searching, and all other EPG features are unaffected.

---

### Improved: An orange banner now appears on every page when disk space is low or recordings are paused

**What you would notice:** Before this change, if scheduled recordings were paused because disk space ran low, a warning badge only appeared on the Scheduled recordings page. Browsing the guide or adjusting settings gave no indication that recordings had stopped. An orange banner now appears at the top of every page in those situations, showing the current free disk space and telling you what to do.

**What changed:** A banner was added to the top of every page in the app. It appears when any scheduled recording is in a paused (low space) state, or when free disk space falls below your configured minimum. No backend changes were made.

---

### Fixed: Completed recordings are no longer re-downloaded after a container restart

**What you would notice:** After restarting Mustarrd (for example after an Unraid array operation), some recordings that had finished downloading would be queued to download again from scratch. If the original catchup window had expired by the time the re-download started, the recording would fail. This affected recordings from providers that do not send a file size in the download response (chunked downloads).

**What changed:** Mustarrd now checks whether a recording is complete by comparing the file size on disk to the amount already downloaded, even when the provider did not report a total file size. Recordings that match are moved to completed on restart instead of being re-queued.

---

### Improved: Downloads History now has a filter to show only completed, failed, or cancelled recordings

**What you would notice:** The History tab on the Downloads page used to show all past recordings in one mixed list. Finding the recordings that failed, or checking what you had cancelled, meant scrolling through everything. A filter dropdown now sits at the top of the History panel. Choose from All, Completed, Failed, or Cancelled to narrow the list instantly. This is a display change only and does not affect your recordings.

**What changed:** A status filter dropdown was added to the Downloads > History tab. Selecting a status narrows the list immediately. No backend changes were made.

---

### Fixed: The EPG Offset (minutes) setting now actually shifts program times in the guide

**What you would notice:** Settings has a global "EPG Offset (minutes)" field that is supposed to shift all program times forward or backward to correct for a provider that sends guide data in the wrong timezone. Before this fix, you could save a value there and nothing would change. The setting was stored in the database but never read. Program times in Browse and EPG search now shift by the number of minutes you enter in that field.

**What changed:** The EPG service now reads the global offset and adds it on top of any per-account guide offset when displaying programs in Browse EPG and EPG search. If you previously set this field and noticed it had no effect, it will now work after updating.

---

### Improved: Scheduled and Upcoming recordings now show "Today" and "Tomorrow" instead of full dates

**What you would notice:** Cards on the Scheduled Recordings page and the Downloads Upcoming tab now say "Today at 7:30 PM" or "Tomorrow at 8:00 PM" for shows airing in the next two days. Shows airing further out still display the full date. This makes it much easier to scan your recording list and spot what records tonight or tomorrow at a glance.

**What changed:** The date display on Scheduled and Upcoming cards now checks whether a recording airs today or tomorrow and uses plain-English labels accordingly. No backend changes were made.

---

### Fixed: Program guide no longer goes blank for providers that omit seconds from their timestamps

**What you would notice:** A small number of IPTV providers send program guide data with 12-digit timestamps that leave out the seconds (for example, "202306011200" instead of "20230601120000"). If your provider used this format, your entire guide would appear empty after refreshing with no error message to explain why. Mustarrd now handles both formats, so the guide loads correctly regardless of which your provider uses.

**What changed:** The part of Mustarrd that reads and parses program guide data now accepts 12-digit timestamps (hours and minutes only) in addition to the standard 14-digit format that includes seconds. This also fixes a related case where a 12-digit timestamp followed by a timezone offset (for example "202306011200 +0000") was silently dropped.

---

### Fixed: Passing an unreasonably large timestamp to an internal API endpoint no longer causes a server error

**What you would notice:** No visible change during normal use. Previously, if an API client sent an impossibly large timestamp value to the failed recordings count endpoint, Mustarrd would respond with a generic server error. It now returns a proper "invalid input" error that tells the caller their value was out of range.

**What changed:** The failed recordings count endpoint now validates that the provided timestamp falls within a reasonable date range. Values outside that range return a 422 (Unprocessable Entity) error instead of crashing with a 500.

---

## 2026-06-07

### Fixed: Hardware-accelerated transcoding no longer destroys MP4 and MKV recordings

**What you would notice:** If you use hardware acceleration (VAAPI, NVIDIA, or AMD) and your Recording Format is set to MP4 or MKV, every completed VOD download was being permanently destroyed during post-processing. The download appeared to finish normally in the UI, but the file on disk was empty and could not be played. This did not affect users with Hardware Acceleration set to CPU, or users keeping recordings as TS files.

**What changed:** When the recording format already matches the downloaded file type, Mustarrd now skips the conversion step entirely. Running a conversion tool with the same file as both the input and the output empties the file before it can read a single byte. The same protection added for TS files in an earlier update is now applied to MP4 and MKV as well.

---

### Fixed: A completed recording is no longer deleted after a container restart

**What you would notice:** If Mustarrd restarted (for example, during an Unraid array update or a container restart) at the exact moment a download finished writing to disk but before it could save the completion status, the recording would be permanently deleted the next time Mustarrd started and reported as Failed with "Provider returned an error (HTTP 416)." This only affected providers that do not include a file size in the download response. Those recordings are now kept and marked as completed instead of being deleted.

**What changed:** When a provider replies with HTTP 416 (meaning the requested start position is past the end of the file, indicating the file is already complete) and a partial file already exists on disk, Mustarrd now treats the response as a success and moves the recording to the completed folder. An HTTP 416 with no file on disk is still treated as an error.

---

### Improved: Deleting a scheduled recording now asks for confirmation

**What you would notice:** Clicking Delete on a scheduled recording used to remove it immediately with no warning. A single misclick meant the schedule was gone and you had to go back to Browse to re-schedule the show. Deleting a schedule now shows a confirmation row at the bottom of the card with a red "Yes, delete" button and a plain "Cancel" button. Works on both desktop and mobile.

**What changed:** A confirmation step was added to the scheduled recording delete action. No recording logic was changed.

---

### Fixed: ComSkip recording formats are now selectable before the comskip binary is installed

**What you would notice:** If you had not yet installed or configured a comskip binary, opening the Recording Format dropdown showed "MKV + skip commercials" and "MP4 + skip commercials" grayed out and unclickable. The field for entering a custom binary path only appears after selecting one of those formats, so there was no way to get started. Both formats are now selectable whenever ffmpeg is available. Selecting one reveals the binary path and INI path fields, along with a note explaining that comskip was not found and where to enter the path.

**What changed:** The ComSkip format options now require only ffmpeg to be available, not a comskip binary already in the system PATH. The alert message was also updated to explain what to do when the binary is not found.

---

### Fixed: A disabled user can no longer reactivate their account using an old setup link

**What you would notice:** If an admin disabled a user account, that user could still visit the original setup link, which was valid for 24 hours, and reactivate their account without the admin knowing. Mustarrd now blocks that path and shows an error immediately.

**What changed:** When someone visits a setup link, Mustarrd now checks whether the account is disabled before doing anything else. Disabled accounts receive a clear error instead of being silently re-enabled. Generating a new setup link for a disabled account is also blocked.

---

### Fixed: Deleting a user no longer permanently locks out their linked Plex account

**What you would notice:** After an admin deleted a Plex-linked user and then invited that same person again, the person's Plex login returned a server error on every attempt. The only way to fix it was to contact the admin and have them intervene manually. Mustarrd now cleans up completely when a user is deleted, so the same person can be re-invited without any problems.

**What changed:** When a user is deleted, Mustarrd now also removes the linked Plex identity record and any outstanding setup tokens belonging to that user. Nothing is left behind that could block a future re-invite.

---

### Improved: Account cards no longer show a green "Enabled" badge alongside error messages

**What you would notice:** Every account card in Settings > Accounts showed a green "ENABLED" badge regardless of whether anything was wrong. When an account was unreachable or had a connection error, the green badge appeared next to the red error indicator, sending contradictory signals. Active accounts no longer show the badge. Only disabled accounts show a gray "Disabled" badge.

**What changed:** The green "Enabled" badge was removed from healthy account cards. No account logic was changed.

---

### Improved: Air time and duration stay on the same line in Downloads > Upcoming

**What you would notice:** On narrow screens or in a narrow browser window, the air time and recording duration on Downloads > Upcoming cards could wrap onto separate lines. The duration then appeared as an unlabeled, disconnected field. They now always stay together on one line.

**What changed:** The air time and duration in each upcoming recording card are treated as a single unit of text so they always wrap together. No recording logic was changed.

---

### Fixed: Downloads no longer save an unplayable file when your provider returns an error page instead of video

**What you would notice:** Some providers return a web page instead of the actual video content when a program is unavailable, for example when the catchup window has expired or you have hit a session limit. Before this fix, Mustarrd treated that web page as if it were a real recording, wrote it to disk, and marked the download as Completed. The "completed" file was garbage and could not be played. Mustarrd now detects this situation and marks the download as Failed with a clear message explaining what happened.

**What changed:** After connecting to your provider, Mustarrd now checks whether the response is a video stream before writing any data. If the provider sends an HTML or plain-text response instead of a video, the download is stopped immediately and marked Failed with the message "Provider returned an error page (Content-Type: text/html). The catchup window may have expired or be unavailable." Providers that correctly omit the Content-Type header are not affected.

---

### Improved: Downloads > Upcoming now labels the air time as "Airs:"

**What you would notice:** On the Downloads > Upcoming tab, each recording card showed a date and time below the channel name with no explanation of what that time meant. You could not tell at a glance whether it was when the show airs, when the download would start, or something else. Each card now shows "Airs:" before the date and time, making it immediately clear. The Scheduled Recordings page already used this label; the Downloads page now matches it.

**What changed:** The "Airs:" prefix was added to the time display on the Downloads > Upcoming tab. No recording logic was changed.

---

### Fixed: EPG guide data now goes to the correct channel when two channels have the same name

**What you would notice:** If your provider had two channels with very similar names, for example "BBC One" and "bbc one" or "CNN" and "cnn," only one of them showed program guide data in Browse EPG. The other appeared completely empty until the slower automatic refresh ran. Mustarrd now consistently gives guide data to the first matching channel in your provider's list.

**What changed:** When building the channel name map during EPG import, Mustarrd now uses a first-write-wins rule for duplicate normalized names. The first channel in your provider's list keeps the name-based guide mapping, and later duplicates are skipped. The slower API-based backfill that previously papered over the problem continues to run as before.

---

### Fixed: Interrupted downloads no longer produce a corrupted file when the provider does not confirm the resume position

**What you would notice:** If a download was interrupted (for example by a container restart) and Mustarrd sent a request to your provider asking to continue from where it left off, some providers acknowledged the request with the right status code but did not include the information Mustarrd needed to verify they were actually sending from the right position. Mustarrd was trusting the provider and appending bytes regardless, which produced a corrupted recording roughly twice the expected size. Mustarrd now falls back to a clean re-download whenever the provider does not confirm the resume position, and logs a message explaining the fallback.

**What changed:** When resuming an interrupted download, Mustarrd now checks that the provider explicitly confirms the byte position before appending. If the confirmation is missing, the download starts over from byte zero. The log will say "Provider returned 206 without Content-Range; re-requesting from start." (If the provider confirms a resume but starts from the wrong position, the log will say "Provider returned Content-Range start N instead of requested M; re-requesting from start.") No change to downloads that are not interrupted.

---

### Improved: Whole-hour durations now show as "1h" instead of "1h 0m"

**What you would notice:** Any recording that is exactly one hour, two hours, and so on used to display its duration as "1h 0m" on the Scheduled and Downloads pages. The trailing "0m" added no useful information. Those durations now show as "1h", "2h", and so on. Recordings with a partial hour, like "1h 30m" or "45m", are not affected.

**What changed:** The duration display on the Scheduled page and the Downloads > Upcoming tab now drops the "0m" when there are no extra minutes. No recording logic was changed.

---

### Improved: Settings now groups post-processing options under clearer headings

**What you would notice:** In Settings, the Post-Processing section previously showed "Max Concurrent Post-Processing" under a heading labelled "RECORDING FORMAT," which made it look like a format setting when it is not. The concurrent jobs control now lives under its own "CONCURRENCY" heading, and "RECORDING FORMAT" covers only the format-related options (output format, hardware acceleration, delete original, and ComSkip settings). The options themselves are unchanged; only the grouping is clearer.

**What changed:** The Settings > Post-Processing page now has two distinct section headings: "CONCURRENCY" above the concurrent jobs control, and "RECORDING FORMAT" above the format controls. No behavior was changed.

---

### Improved: Long show titles in Downloads > Upcoming no longer get cut off on phones

**What you would notice:** On phone screens, long show titles on the Downloads > Upcoming tab were still being cut off with "..." after an earlier fix covered the Scheduled page and the Downloads list view. The Upcoming tab now wraps titles to a second line so the full show name is always readable.

**What changed:** Program titles on the Downloads > Upcoming tab now wrap instead of truncating on narrow screens. No visual change on desktop screens.

---

### Improved: Show titles no longer get cut off on phones

**What you would notice:** On phone screens, if a show title was long and shared a row with a wide badge (like "PAUSED (LOW SPACE)"), the title was cut off with "..." making it impossible to read the full name. The title now wraps to a second line so the full show name is always visible.

**What changed:** The program title on the Scheduled and Downloads pages now wraps instead of truncating when there is not enough horizontal space. No visual change on desktop screens.

---

### Fixed: Hardware-accelerated transcoding no longer overwrites recordings with empty files

**What you would notice:** If you had Transcoding turned on with the output format set to TS and hardware acceleration turned on (VAAPI, NVIDIA, AMD, or Apple Silicon), every completed recording was silently overwritten with a 0-byte empty file. The download looked done in the UI but the file on disk was empty. This did not affect users with hardware acceleration set to CPU, or users saving to MKV or MP4.

**What changed:** When the output format is already TS (the same format as the source file), Mustarrd no longer runs FFmpeg at all. The file is left as-is. This was always the correct behavior: copying a TS file to itself with stream copy produces an empty file. The CPU code path happened to avoid this bug, but any hardware-accelerated path triggered it.

---

### Fixed: Resuming an interrupted download no longer starts over from the beginning

**What you would notice:** If the backend restarted while a recording was in progress and your provider did not report how long the download would be, the download would always restart from byte zero. For a recording close to the end of your provider's catchup window, re-downloading from scratch could cause the window to close before the file was complete. Mustarrd now sends a Range request to your provider when resuming. If the provider supports it (most do), the download picks up from where it left off. If the provider does not support resuming, Mustarrd falls back to starting over and logs a message explaining why.

**What changed:** When recovering an in-progress download, Mustarrd now sends a `Range: bytes=<offset>-` header to request only the remaining portion. If the provider responds with HTTP 206, the existing partial file is kept and bytes are appended from the saved position. If the provider responds with HTTP 200 (ignoring the Range header), Mustarrd falls back to a full re-download, same as before.

---

### Improved: Low disk space badge on Downloads now matches the Scheduled page

**What you would notice:** When a recording is paused because your drive is running low on space, the Downloads page showed an orange badge reading "LOW DISK SPACE" with no icon. The Scheduled page showed the same paused recording as a yellow "PAUSED (LOW SPACE)" badge with an alert icon. The two pages were describing the same condition in two different ways. Both pages now show "PAUSED (LOW SPACE)" in yellow with an alert icon, so you always see the same label no matter which page you check.

**What changed:** The badge label and color on the Downloads page Upcoming tab now match the Scheduled page for recordings paused due to low disk space. No recording behavior was changed.

---

### Improved: Account cards now say "Enabled" and "Disabled" instead of "Active" and "Inactive"

**What you would notice:** The status badge on each account card used to read "ACTIVE" in green when an account was turned on. This sat next to the red "Unreachable" dot that appears when Mustarrd cannot reach your provider. Seeing a green "ACTIVE" badge next to a red failure dot was confusing: the green badge looked like everything was working, but the red dot said it was not. The badge now reads "ENABLED" when the account is turned on, and "DISABLED" when it is turned off. "Enabled" describes whether the account is switched on in Mustarrd, not whether the connection to your provider is currently working. The red "Unreachable" dot separately tells you about the connection.

**What changed:** The label on the account status badge was updated to use "Enabled" and "Disabled" instead of "Active" and "Inactive." Colors and behavior are unchanged.

---

### Fixed: Settings no longer accepts negative recording padding values

**What you would notice:** Before this fix, you could save a negative number for "Default pre-padding" or "Default post-padding" in Settings and Mustarrd would silently accept it. A negative padding value shifted the recording start time in the wrong direction and shortened the captured duration, causing recordings to miss content or fail without any clear explanation. Mustarrd now rejects negative values immediately and returns an error, so your padding settings always behave as expected.

**What changed:** The Settings API now validates that the default pre-padding and post-padding minutes must be zero or greater. This matches the same rule that already applied to per-schedule padding. Existing valid settings are not affected.

---

### Improved: Downloads page no longer shows blank progress bars for stages that are not running

**What you would notice:** While a recording was downloading, the Downloads page showed three progress bars: Download, Commercial Detect, and Re-encode. For most users who have ComSkip and transcoding turned off, the Commercial Detect and Re-encode bars appeared immediately at zero with blank labels and stayed that way for the entire download. It looked like something was broken or stuck. Now only the stages that are actually running show a bar.

**What changed:** The Commercial Detect and Re-encode progress bars are now hidden when those stages are not active for a given download. If you have ComSkip or transcoding enabled, the bars appear as soon as those stages start working.

---

### Improved: Browse now shows a clear message when your provider cannot be reached

**What you would notice:** When your IPTV provider was unreachable, the Browse page channels panel showed "No channels available yet." The same message appeared on a freshly configured account waiting for its first sync. There was no way to tell whether something was wrong or the channels simply had not loaded yet. Browse now shows a red alert with "Could not reach your provider." Admin users also see a link to Settings to check the account connection status and error details.

**What changed:** The Browse channels panel now shows a distinct error message when your provider cannot be reached, instead of the same empty-state message shown on first setup. Admin users see a link to Settings. Non-admin users see a note suggesting they contact their administrator.

---

### Fixed: Recordings with post-padding no longer fail immediately if the program just expired

**What you would notice:** If you added extra post-recording padding (minutes added after a show ends), a recording whose program had just passed the catchup window could still be dispatched and immediately fail with "Recording not found on provider." This left a red error row in your Downloads list. It only happened when the padding time extended past the catchup window boundary, and only with non-zero post-padding. Those recordings are now correctly marked as expired before any download attempt.

**What changed:** When deciding whether to dispatch a scheduled recording, Mustarrd now compares the actual program end time against the catchup window, ignoring any padding. A recording whose program has already expired is marked Failed with a clear message immediately, rather than being dispatched and failing.

---

### Improved: Settings now has a single Recording Format dropdown instead of separate toggles

**What you would notice:** The Settings page previously showed four separate fields for post-processing: "Enable Transcoding," "Remux Only," "Enable ComSkip," and a Transcode Format selector. These are now combined into a single "Recording Format" dropdown with plain-English labels. Your existing configuration is read and shown correctly in the new dropdown. Nothing about how recordings are saved has changed.

**What changed:** The four post-processing fields on the Settings page were replaced with one dropdown. The available options are: Keep original (.ts), MKV container (fast, no re-encode), MP4 container (fast, no re-encode), MKV (re-encode with FFmpeg), MP4 (re-encode with FFmpeg), MKV + skip commercials, and MP4 + skip commercials. This is a Settings page visual change only. The backend settings that control recording behavior are unchanged.

---

### Fixed: Guide data for a deleted account is now removed immediately

**What you would notice:** If you deleted an account and then added a new one, the new account's program guide could briefly show programs from the deleted account before the guide refreshed. This happened because SQLite can reuse account ID numbers, so the new account would inadvertently pick up the old guide data. This is now fixed. When an account is deleted, all of its guide data is removed at the same time.

**What changed:** Account deletion now deletes all program guide rows for that account as part of the same operation, and clears the in-memory guide cache so no stale data can be returned to the browser before the cache resets.

---

### Fixed: Deleting an account now cancels its scheduled recordings and stops active downloads

**What you would notice:** Before this fix, deleting an account from Mustarrd left its scheduled recordings in a broken state. On the next scheduler check, those recordings would fail with a confusing "Account not found" error rather than a clear reason. Any download that was actively running when you deleted the account would keep running in the background with nowhere to save to. Now, deleting an account immediately cancels all its pending and active scheduled recordings with a clear "Account deleted" note, and any download that was in progress is stopped cleanly.

**What changed:** The account deletion step now marks all non-finished scheduled recordings as cancelled before removing the account row, and passes any actively running downloads to the cancellation handler so they stop immediately.

---

### Improved: Account cards no longer show default badges that have no meaning for most setups

**What you would notice:** Account cards on the Accounts page used to show "Catchup: none reported" and "Guide offset: 0h" on every card by default, even when those were simply the default values with no meaning for most setups. These two badges are now hidden unless they carry a value worth paying attention to. The Catchup badge appears only when your provider reports how many days of archive it supports. The Guide Offset badge appears only when it is set to a non-zero value. Account cards now show less clutter and only surface information that is relevant to your setup.

**What changed:** Two badges on the Accounts page are hidden when they hold default or empty values. This is a display-only change with no effect on recording behavior.

---

### Improved: Unreachable account badge now shows a plain-English reason for the failure

**What you would notice:** When Mustarrd cannot connect to one of your IPTV accounts, the Accounts page showed a red "Unreachable" dot with no further explanation. You had to check your provider, credentials, and network manually to figure out what went wrong. The Accounts page now shows a short plain-English reason under the red dot, for example: "Invalid credentials. Check your username and password." or "Connection timed out. Check your network and try again."

**What changed:** Mustarrd now translates the underlying connection error into a short readable message and stores it alongside the account status. The Accounts page displays this message under the Unreachable badge. Messages are capped at 200 characters so a long technical error cannot fill the card.

---

### Fixed: Queuing a download that is already active now gives a clear error

**What you would notice:** If a recording of a program was already in the queue (pending, downloading, or being processed) and you clicked Download for the same program again, Mustarrd would silently start a second recording writing to the same file. The two recordings would overwrite each other, leaving a corrupt or incomplete file. Now Mustarrd immediately rejects the second request with the message "A download for this program is already active."

**What changed:** Before starting a new download, Mustarrd checks whether an active entry for the same program already exists. If one is found in the pending, downloading, or processing state, the request is rejected and no duplicate entry is created. Re-downloading a program that previously failed or was cancelled still works normally, because only active (in-progress) entries trigger the check.

---

### Fixed: Failed transcode no longer leaves a partial file on disk

**What you would notice:** If a download required conversion (transcoding) and FFmpeg failed partway through, a partial file ending in .mkv or .mp4 was left in your download folder and never cleaned up. These incomplete files accumulated over time and could not be played. Now Mustarrd deletes them automatically when transcoding fails, so only successfully converted recordings are kept.

**What changed:** The cleanup step that runs after a failed download now includes .mkv and .mp4 partial outputs alongside the existing types it already removed. Successful recordings are not affected: the output file is moved to your completed folder before cleanup runs, so it is never deleted.

---

### Fixed: Movies and Series page no longer blank on some providers

**What you would notice:** On certain IPTV providers, opening the Movies or Series page showed nothing at all. There was no error message. The page was simply empty, even though your provider had content available. This is now fixed.

**What changed:** Some IPTV providers send their movie and series listings in a slightly different format than expected. Mustarrd already handled this for the Live TV channel list, but the same fix had not been applied to Movies and Series. All four affected lists (movie categories, movie titles, series categories, and series titles) now handle both formats correctly.

---

### Fixed: Movies and series downloads now check available disk space before starting

**What you would notice:** On a system running low on disk space, trying to download a movie or series episode would start the download and potentially fill your drive. The same disk-space guard that already blocked catchup downloads did not apply to movies or series. Now it does. If you do not have enough free space (based on the threshold you set in Settings), the download will be blocked immediately with a clear message: how much space is free and how much is required.

**What changed:** The movie and series download endpoints now run the same preflight disk-space check that catchup downloads already used. If free space is below your configured minimum, the download is rejected before any data is transferred.

---

### Fixed: Long programs that started just before the catchup window edge now appear correctly

**What you would notice:** Some programs, particularly long ones like movies or extended sports broadcasts, would be missing from the Catchup page even though most of the program was well within your provider's catchup window. There was no error and no indication the program existed. You would see a gap in the guide where the program should be. These programs now appear alongside everything else and can be downloaded normally.

**What changed:** Mustarrd was filtering catchup programs by checking whether the program started within the archive window. This incorrectly excluded any program that began just before the window boundary but finished well inside it. The check now uses the program end time instead, so a program is included as long as it finished within the window, regardless of when it started.

---

### Improved: Settings now rejects invalid values and explains what is accepted

**What you would notice:** Before this change, if you typed an unsupported format like "avi" into the Transcode Format field or left Minimum Free Space set to 0, Mustarrd would save the value silently and every transcoded download would fail with a confusing FFmpeg error, or the disk-space guard would be disabled entirely with no warning. Settings now shows a clear error immediately when a value is not accepted. If your saved configuration already has 0 in the Minimum Free Space field (possible on older installs), Mustarrd now silently corrects it to 25 GB so you can open and save Settings without hitting an error.

**What changed:** The Transcode Format and Hardware Acceleration fields now only accept values Mustarrd supports. The Minimum Free Space field requires at least 1 GB. A legacy stored value of 0 is corrected to 25 GB automatically when you open Settings.

---

### Improved: Downloads page now has an Upcoming tab showing your scheduled recordings

**What you would notice:** The Downloads page now has three tabs: Active, Upcoming, and History. The new Upcoming tab lists every recording that is scheduled to download automatically, sorted by when the program airs. Each row shows the show name, channel, air time, and duration. If a recording is paused because your drive is running low on space, it shows an orange badge explaining why. A count badge on the tab heading (for example "Upcoming (3)") shows how many recordings are queued without requiring you to click in.

**What changed:** A new Upcoming tab was added to the Downloads page between the Active and History tabs. A new backend endpoint returns the list of upcoming scheduled recordings.

---

### Fixed: Downloads no longer fail when a stream or video ID contains special characters

**What you would notice:** Some IPTV providers assign stream IDs, on-demand video IDs, or series episode IDs that include characters like `/`, `#`, or `?`. Before this fix, those characters would corrupt the download URL, and every attempt to record from those channels would fail immediately with no clear reason. The content appeared available in your guide, but the download never started or returned a confusing error. This is now fixed for all four Xtream download URL types (live streams, timeshift, on-demand video, and series episodes).

**What changed:** Stream IDs, video IDs, and episode IDs are now encoded before being placed in the download URL, the same way your username and password were already encoded by an earlier fix.

---

### Fixed: Recording padding is now capped at 120 minutes for manual downloads

**What you would notice:** When starting a manual download with a very large pre-padding or post-padding value, Mustarrd would build a download window spanning days or longer and send it to your provider. The provider would usually return a confusing error or deliver garbage content with no clear explanation. Manual downloads now enforce the same 120-minute maximum padding that scheduled recordings already had.

**What changed:** The manual download request handler now rejects padding values above 120 minutes, matching the limit already in place for scheduled recordings.

---

### Improved: Red badge on the Downloads menu item shows how many recordings have failed

**What you would notice:** Before this change, there was no way to know a recording had failed without manually opening the Downloads page. You might not notice a failed recording for days. A small red badge now appears on the Downloads item in the sidebar as soon as a recording permanently fails. The badge shows the number of failures since you last visited Downloads. Opening the Downloads page clears the badge. The existing yellow badge that shows how many downloads are currently in progress continues to work alongside it.

**What changed:** A new backend endpoint counts permanent failures since a given time. The frontend reads a stored timestamp of your last Downloads visit and uses it to request the count. The badge updates automatically each time the page loads.

---

### Fixed: Catchup programs now show correctly on providers that omit archive flags from individual guide entries

**What you would notice:** On some IPTV providers, channels are configured to allow catchup, but individual program entries in the guide do not carry their own "has archive" flag. Before this fix, Mustarrd treated those programs as unavailable for download: the yellow catchup border did not appear, and clicking the program gave no download option. The Catchup page could appear completely empty even when catchup was fully working. Programs on these providers now correctly show the catchup border and can be downloaded.

**What changed:** When a program entry from the live guide does not include its own archive flag, Mustarrd now falls back to the archive setting of the channel itself. This matches how the program guide background refresh already handled the same situation. Providers that do include per-program archive flags are not affected.

---

### Fixed: Failed downloads now show a plain-English reason instead of a technical error

**What you would notice:** When a download failed, the error shown in the Downloads history was often a raw Python exception or an empty string that gave no useful information. You might see something like `<class 'asyncio.TimeoutError'>` or nothing at all. Failed downloads now show a short, readable message: "Connection timed out," "Not enough disk space," "Provider refused the request (403)," "Server not reachable," and so on. The message appears in the Downloads list next to the failed item.

**What changed:** A new internal function maps the most common failure types to short plain-language descriptions at both places in the download process where failures are caught. Unknown errors fall back to showing the raw message as before.

---

### Fixed: Downloads no longer fail when your IPTV password contains special characters like # or /

**What you would notice:** If your IPTV provider gave you a password containing characters like `#`, `?`, `/`, or a space, every catchup and timeshift download would silently fail. The URL Mustarrd sent to your provider was cut off at the special character, so the provider received a truncated or garbled credential and rejected it with a 401 or 404 error. Your credentials looked correct in Settings, but downloads kept failing with no useful message. This is now fixed for any combination of characters in your username or password.

**What changed:** The four URL builders that handle stream, timeshift, on-demand video, and series downloads now encode your username and password before placing them in the request URL. Characters that have special meaning in a URL, such as `#`, `?`, `/`, and spaces, are replaced with their safe equivalents before being sent to your provider. A similar fix was made earlier for the API and guide-update URLs; this completes the same fix for the download URLs.

---

### Fixed: Browse and program guide now work with providers that label their responses incorrectly

**What you would notice:** On some IPTV providers, especially older or budget PHP-based panels, opening Browse would show no channels, the program guide would fail to refresh and stay stale, and adding your account details would fail with a generic error. Everything looked correct in your settings and the provider itself was working. The underlying cause was that the provider's server was sending valid data but labeling it as plain text in the response header instead of JSON. Mustarrd was rejecting the data because of that incorrect label rather than reading what was actually inside it.

**What changed:** Mustarrd now reads the content of each response to determine what it contains, rather than relying on the server's Content-Type header. Any provider that returns valid data with a "text/plain" or "text/html" header now works correctly.

---

### Fixed: Scheduled recordings no longer fail immediately on providers that send "0" as text instead of a number

**What you would notice:** If your provider sent the program start time as the text "0" instead of the number 0, Mustarrd would calculate a start time in the year 1970 and immediately mark the scheduled recording as Failed with a "catchup window has passed" message. This could affect some providers even for shows scheduled in the future.

**What changed:** Start and stop timestamps from the provider are now converted to numbers before being used. The text "0", an empty value, or any non-numeric value all correctly fall back to using the show's scheduled start and end times from the program guide.

---

### Fixed: Subtitle files next to recordings are no longer deleted by ComSkip post-processing

**What you would notice:** If you placed subtitle files (`.srt`, `.vtt`, `.ass`) or metadata files (`.xml`) in the same folder as your recordings and then ran a download with ComSkip enabled, those files would silently vanish after post-processing finished. There was no warning and no way to recover them. Subtitle files in your recording folders are now left alone.

**What changed:** ComSkip's cleanup step was incorrectly including subtitle and metadata file extensions alongside the working files it is supposed to remove. Only actual ComSkip and FFmpeg working files (`.edl`, `.txt`, `.log`, `.logo`, `.csv`, `.vdr`, segment files) are now deleted. The same correction was applied to the cleanup that runs after every transcode or remux, so subtitle files are preserved in both paths.

---

### Fixed: ComSkip failures now mark the recording as Failed instead of saving it full of commercials

**What you would notice:** If ComSkip ran into an unexpected error while processing a recording (for example, if the binary crashed, a file was unreadable, or an internal error occurred), Mustarrd was silently ignoring the error and saving the file anyway with all commercials still included. The download would show as Completed with no indication that commercial removal had failed. Now, if ComSkip fails unexpectedly, the recording is marked as Failed and shows a clear error message, so you know to check what went wrong.

**What changed:** An error-handling block in post-processing that was swallowing ComSkip exceptions now lets them through so the download is correctly marked as Failed instead of silently completing with commercials intact.

---

### Fixed: Scheduled recordings on non-UTC servers now store the correct times

**What you would notice:** If your Unraid server or host machine is set to a timezone other than UTC (for example, US/Eastern, Europe/London, or Australia/Sydney), scheduled recordings could fail before the program had even aired. Mustarrd would calculate a start time that was off by your UTC offset and decide the catchup window had already passed. The scheduler would mark those recordings as Failed with a "catchup window has passed" message even for shows scheduled in the future. This is now fixed.

**What changed:** Two corrections were made to how recording times are stored. A fallback path was reading the server's local clock without converting to UTC, causing times to be wrong on non-UTC servers by the server's timezone offset. Timestamps sent as an empty or non-numeric value by some providers are also now handled correctly.

---

### Fixed: ComSkip temporary files left behind on channels with brackets in the name

**What you would notice:** If your provider uses channel names containing square brackets, such as [BBC One HD] or [Sky Sports], temporary files created by ComSkip during commercial removal were not being cleaned up after the recording finished. These small files (.edl, .log, and similar) would accumulate quietly in your downloads folder over time. The cleanup now works correctly for all channel names.

**What changed:** The file cleanup step was treating square brackets in channel names as a pattern wildcard instead of literal characters. It now escapes them so the correct files are matched and deleted.

---

### Fixed: Program guide no longer fails on a specific kind of corrupt data from some providers

**What you would notice:** A previous update made Mustarrd resilient to corrupt guide data from your provider, but one narrow case could still cause the guide refresh to fail silently and leave your program listings stale. This happened when a provider's server restarted mid-response or a network proxy injected an error page into a compressed data stream. The guide now handles this case the same way it handles other corrupt responses and continues refreshing on the next scheduled cycle.

**What changed:** An additional error type that Python raises for gzip data with a valid start but corrupt body is now caught alongside the cases handled by the earlier fix.

---

### Fixed: Downloading from Browse now checks available disk space before starting

**What you would notice:** Settings lets you configure a minimum free disk space threshold (default 25 GB). That limit protected scheduled recordings but was ignored for manual downloads you started by clicking Download in Browse. If your disk was nearly full, the download would start, run until your storage ran out, leave a partial file behind, and show a cryptic error like "No space left on device." Now Browse checks free space before queuing the download. If there is not enough room, you see a clear message straight away: "Not enough disk space to start this download. 5.0 GB free, 25 GB required." No partial file is written.

**What changed:** The manual download endpoint now performs the same free-space check that scheduled recordings already used, and returns a clear error instead of letting the download fail mid-transfer.

---

### Fixed: Enabling ComSkip can no longer be silently undone by a second settings change

**What you would notice:** ComSkip requires transcoding to be on and remux-only mode to be off in order to cut commercials. Previously, if you made two separate settings changes, for example first enabling ComSkip and then separately disabling transcoding, Mustarrd would save the invalid combination without warning. ComSkip would run and mark the commercial segments, but the file would not actually be cut, leaving all commercials in the final recording with no error message. Both settings are now kept consistent: if ComSkip is on, transcoding stays on and remux-only stays off, no matter the order of your changes.

**What changed:** The settings enforcement now checks the full final state after all changes are applied, not just the fields in the current request.

---

### Improved: Scheduled recording cards now have clearer time labels

**What you would notice:** On the Scheduled Recordings page, each recording card used to show "Slot" and "Expected start" for the time fields, which were confusing. "Slot" is internal jargon, and there was a duplicate time line on the card. The times are now labeled clearly: "Airs: [start time] - [end time] ([show duration])" shows when the program airs, and "Download starts: [time] ([recording duration])" shows when Mustarrd will begin downloading, including any padding you set.

**What changed:** Two time labels on schedule cards were rewritten and one duplicate line was removed.

---

### Fixed: Searching the program guide no longer shows programs you cannot download

**What you would notice:** When you searched across all channels in Browse, programs that your provider had marked as not available for catchup would still appear in the results. Clicking them to start a download did nothing, with no error or explanation. These programs are now hidden from search results, so you only see what you can actually download.

**What changed:** The search results now exclude past programs your provider has flagged as unavailable for catchup. Future programs (for scheduling) are still included regardless of this flag.

---

### Fixed: Browse and the program guide no longer crash on certain IPTV providers

**What you would notice:** On some IPTV providers, opening Browse or waiting for the program guide to refresh would produce a server error and channels would not load at all. These providers send their channel and category lists in an unusual format that Mustarrd was not prepared to handle. Browse and the program guide now work correctly on these providers.

**What changed:** Mustarrd now handles both the standard and the unusual format that some providers use when sending channel and category lists.

---

### Improved: Browse now shows clearer messages when no channels are found

**What you would notice:** When you open Browse and no channels have loaded yet, it now says "No channels available yet." When you search and nothing matches what you typed, it now says "No channels match your search." Previously both states showed the same generic message, making it hard to tell whether something was wrong or you simply had an empty result.

**What changed:** Two distinct messages were written: one for the empty state and one for empty search results.

---

### Fixed: Corrupt or incomplete guide data from your provider no longer silently stales your program guide

**What you would notice:** If your provider sent back a corrupt or incomplete compressed guide file (which can happen during a provider restart or a network hiccup mid-transfer), Mustarrd would mark the account as unhealthy and stop refreshing the guide with no explanation. The guide would sit stale indefinitely. Mustarrd now handles the bad data gracefully and treats it as an empty response, so the guide continues refreshing on the next cycle.

**What changed:** The compressed guide data handler now catches decompression errors instead of letting them crash the guide refresh job.

---

### Fixed: Scheduled recordings no longer get stuck showing "Failed" after a successful retry

**What you would notice:** If a download briefly failed and then automatically retried and finished successfully, the Schedules page could still show the recording as permanently Failed, with no way to clear it. Reloading the page would not help. The recording was actually fine on disk, but the status display was wrong and would stay wrong forever.

**What changed:** Loading the Schedules page was secretly writing the download status back to the database on every page load. If you happened to load the page during the brief window between a failure and a successful retry, the failure was permanently stamped onto the schedule record. The Schedules page now only reads data. The schedule status is updated correctly by the download process itself when a recording finishes, fails, or is cancelled.

---

### Improved: Downloads history has a new "Clear finished" button

**What you would notice:** There is now a "Clear finished" button on the Downloads page. Clicking it removes all completed and failed entries from the history list in one step, after a confirmation prompt to prevent accidents. Cancelled entries stay in the list in case you want to retry them.

**What changed:** A new button lets you bulk-remove completed and failed history entries. Your actual downloaded files on disk are not affected.

---

### Improved: Completed schedule cards no longer show a confusing "Download ID" number

**What you would notice:** When a scheduled recording finished, the card used to show "Recording finished. Download ID: 42" below the status. That number is an internal identifier that means nothing to most users, and the tooltip explaining it was invisible on phones. The Download and Play buttons right below already tell you the recording is ready. The ID line has been removed.

**What changed:** The completed-recording card no longer displays the internal download ID.

---

### Fixed: Downloads with program titles in Chinese, Japanese, Korean, or Arabic no longer fail immediately

**What you would notice:** If you downloaded a program with a title made mostly or entirely of Chinese, Japanese, Korean, or Arabic characters, the download would fail right away with a file error. This happened because those characters each take up more space in a filename than Latin letters do, and Mustarrd was not accounting for that when checking the length limit. Downloads with non-Latin titles now complete normally.

**What changed:** When building a filename, Mustarrd now measures length the same way your filesystem does (in bytes), so it trims long titles correctly for all languages.

---

### Fixed: The "Enable transcoding" toggle in Settings now works

**What you would notice:** If you had "Enable transcoding" turned off in Settings, Mustarrd was ignoring that setting and running every catchup download through ffmpeg anyway. Your files were being transcoded to MKV even though you had transcoding disabled. Downloads now respect the setting: with transcoding off, Mustarrd saves the raw .ts file directly to your completed folder.

**What changed:** The download process was not checking the transcoding toggle before deciding whether to run ffmpeg. It now checks the setting first and skips the transcode step entirely when transcoding is disabled.

---

### Fixed: The "Keep original file after transcode" toggle in Settings now works

**What you would notice:** If you had enabled transcoding and set "Delete original file after transcode" to off in Settings, Mustarrd was ignoring that setting and always deleting the original .ts file after transcoding finished. Users who wanted to keep the raw stream alongside the transcoded MKV were not getting it. The original .ts file is now kept or deleted based on your setting.

**What changed:** After transcoding, the cleanup step was unconditionally deleting the original .ts file without checking your setting. It now checks the setting before deleting, so the original file is only removed if you have chosen to delete it.

---

### Improved: Downloads page now shows how much free space is left on your recording drive

**What you would notice:** At the top of the Downloads page there is now a badge showing how much disk space is available on your recording drive. The badge turns red when space drops below the minimum you configured in Settings. If your recording drive is missing or not mounted (which can happen on Unraid after an array restart or if a share path changes), the badge shows "Recording drive not found" in orange instead of reporting a wrong number from the wrong disk.

**What changed:** The Downloads page now polls a read-only endpoint every 30 seconds to get current free space from the configured recording drive. No directories are created or modified. The check is read-only.

---

### Fixed: Downloads with unreadable program titles no longer create a hidden file that overwrites itself

**What you would notice:** Some IPTV providers send program titles made entirely of spaces, dots, or other characters that are stripped when building a filename. Previously, Mustarrd would create a hidden file named `.ts` for these programs, and each one would silently overwrite the previous. The file appeared to download successfully but could never be found. Downloads with unreadable titles now save as `unknown-program.ts` instead.

**What changed:** When the cleaned program title comes out empty after stripping illegal characters, Mustarrd now uses `unknown-program` as the filename instead of producing a hidden `.ts` file.

---

### Fixed: Browse page no longer shows a blank error when loading a channel guide on a fresh install

**What you would notice:** On a fresh install, or immediately after clearing your program database, opening the Browse page for a channel would show a blank error page instead of the guide. Once the guide had been populated at least once, the error stopped. The guide now loads correctly on the very first request.

**What changed:** Some IPTV providers send timestamps in milliseconds instead of seconds. The channel guide page was not prepared to handle millisecond timestamps and crashed with an internal server error. It now converts them automatically, matching how the program guide importer already handled the same format.

---

### Fixed: Scheduled recordings older than your provider's catchup window no longer fail silently

**What you would notice:** If Mustarrd restarted (or recovered from a disk-full pause) while a scheduled recording was waiting, any recording whose program had already passed the catchup window would be dispatched anyway. The download would fail with a raw provider error and no explanation. It now fails immediately with a plain message explaining that the catchup window has passed.

**What changed:** The scheduler now checks whether a pending recording is still within the provider's catchup window before attempting to download it. If the window has passed, the recording is marked as failed with an explanation instead of being sent to a download that cannot succeed.

---

### Fixed: Pre- and post-recording padding can no longer be set to an unreasonably large value

**What you would notice:** In Schedule settings, you can add extra minutes before and after a recording to avoid clipping. Previously there was no upper limit, so entering a very large number (such as 10000 minutes) was accepted silently and would lock a download slot for days. Padding is now capped at 120 minutes.

**What changed:** The scheduler now rejects pre- and post-padding values above 120 minutes with a validation error instead of accepting them.

---

### Fixed: Program guide no longer goes blank when your provider does not include a catchup window length

**What you would notice:** On some IPTV providers, the program guide would go completely blank after a refresh. The Browse page would show no programs even though your channels were still listed. Mustarrd now keeps your existing program guide data in this case.

**What changed:** When a provider sends channel information without a catchup window length, Mustarrd was treating the missing value as zero and deleting all stored program guide entries for that channel. It now treats a missing catchup window as "unknown" and keeps whatever program data is already stored.

---

### Fixed: TV episode filenames no longer end with a trailing dash when a custom filename template is set

**What you would notice:** If you had customized the TV filename template in Settings, episodes with no subtitle would produce filenames with a trailing separator, for example "Breaking Bad - S01E01 - .ts" instead of "Breaking Bad - S01E01.ts". This only happened when a custom template was saved in Settings; the default template was not affected.

**What changed:** The filename builder was not checking whether an episode title existed before applying a custom template. It now checks first, and falls back to the no-subtitle format when no episode title is available.

---

### Fixed: Hidden characters from RTL-language providers no longer appear in filenames or break Plex and Jellyfin matching

**What you would notice:** If your IPTV provider is Arabic, Hebrew, Persian, or another right-to-left language, channel names and program titles may contain invisible formatting characters that are not visible to you but are present in the text. These characters ended up in your filenames, causing files to appear invisible or fail to match in Plex and Jellyfin. Filenames are now cleaned of these characters before the file is saved.

**What changed:** Seven invisible Unicode characters (including zero-width space, directional marks, and soft hyphen) are now stripped from channel names and program titles before they reach the filename. They are removed rather than replaced with spaces so that words remain joined correctly.

---

## 2026-06-06

### Improved: Each account in Settings now shows whether it can reach your provider

**What you would notice:** In Settings > Accounts, each account card now shows a colored status indicator next to the account name. A green dot labeled "Connected" means Mustarrd successfully connected during the last program guide refresh. A red dot labeled "Unreachable" means the last attempt failed. New accounts show "Not checked yet" until the first program guide refresh runs. A note shows how long ago the last check completed.

**What changed:** Mustarrd now records whether each program guide refresh attempt succeeded or failed. Two new fields on each account track the result and the time it was last checked. The Accounts page reads those fields and shows the status indicator without making any extra network calls.

---

### Improved: Setting up your first account is now simpler

**What you would notice:** On a fresh install with no accounts configured, the Accounts page now shows only an "Add Your First Account" button in the center of the screen. The "Force EPG Refresh" button and an "Add Account" button in the header are hidden until you have at least one account saved. Once you have an account, the header shows "Add Another Account" and "Force EPG Refresh" as before.

**What changed:** The header buttons on the Accounts page now appear or disappear based on whether any accounts are configured. This removes confusing options on first-time setup when there is nothing to refresh yet.

---

### Fixed: Logging in now works when your admin account uses a username other than "admin"

**What you would notice:** If you set up Mustarrd with a custom admin username (anything other than the default "admin"), you would be unable to log in. The login form would reject your credentials even when the password was correct. Login now works for any admin username.

**What changed:** The login endpoint was hardcoded to look up an account named "admin" instead of looking up whichever admin account is actually configured. It now looks up the real admin account, so any valid admin username is accepted.

---

### Fixed: Custom filename templates in Settings now actually apply to your downloads

**What you would notice:** If you ever opened Settings and customized the filename template for TV shows, movies, or sports, those changes were saved but had no effect. Every downloaded file still used the default naming format no matter what you entered. Your templates now apply correctly.

**What changed:** The part of Mustarrd that names files during a download was not reading your saved template preferences at all. It now reads them and uses them when building the filename. If a template contains a variable that is not available for a particular program, Mustarrd falls back to the default format instead of crashing.

---

### Fixed: Show names containing ".ts" in the middle no longer get corrupted in the filename

**What you would notice:** If you downloaded a program where the channel name or show title happened to include ".ts" somewhere in the middle, for example "KTSA.ts Evening News", the downloaded file would be incorrectly named with that text removed: "KTSA Evening News.ts". The file still downloaded correctly, but the name was wrong.

**What changed:** A text substitution that was only meant to remove the file extension from the end of a name was also removing ".ts" from anywhere inside the name. It now only removes the extension at the very end.

---

### Fixed: TV episode filenames with no subtitle no longer repeat the show title

**What you would notice:** If you downloaded a TV episode that had no episode title (just a show name and episode number, which is common for news and sports programs), the downloaded filename contained the show name twice: for example, "Breaking Bad - S01E01 - Breaking Bad S01E01.ts" instead of "Breaking Bad - S01E01.ts".

**What changed:** The filename builder was using the full show identifier as a fallback for the missing subtitle field, duplicating the title. It now leaves the subtitle portion out entirely when no episode title is available.

---

### Fixed: Filename preview in Settings now matches the actual filename saved to disk

**What you would notice:** When you customized a filename template in Settings, there was a preview that showed an example of what the filename would look like. That preview was not reading your saved settings, so it showed a different result than what your downloads actually produced. The preview now matches what Mustarrd will actually name your files.

**What changed:** The filename preview feature was not passing your settings to the preview calculation. It now uses the same settings as the download process.

---

### Improved: Accounts and Plex Integration now have distinct icons in the Settings sidebar

**What you would notice:** In the Settings navigation, Accounts and Plex Integration previously shared the same server rack icon. If you have both sections configured, it was hard to tell them apart at a glance. Plex Integration now shows a TV icon.

**What changed:** The icon next to "Plex Integration" in the Settings sidebar was changed from a server rack icon to a TV icon.

---

### Improved: Long folder paths in Settings no longer overflow the text box

**What you would notice:** If you configured a long folder path in Settings (common on Unraid when pointing Mustarrd at a deep NAS share path), the path would overflow and get cut off sharply at the edge of the field. It was hard to confirm which folder was actually configured. Long paths now show "..." at the point where they are truncated. Hovering over the field shows the full path.

**What changed:** A text overflow style was applied to folder path fields in Settings. No folder paths or behavior were changed.

---

### Fixed: Entering wrong credentials now shows an error immediately instead of saving a broken account

**What you would notice:** If you typed your IPTV username or password incorrectly when adding an account, Mustarrd accepted them, saved the account as connected, and showed nothing in Browse. The account appeared to be working but was not. You had to delete it and try again without a clear error message. Wrong credentials now show an error immediately so you know to correct them before saving.

**What changed:** Mustarrd was treating a zero value in the authentication field as valid credentials. It now rejects that and returns an error.

---

### Fixed: An empty program guide response from your provider no longer crashes the server

**What you would notice:** On some providers, if a channel's program guide returned no data, clicking anything related to that account would produce a server error. Browse could fail to load for that account entirely. This only affected providers that send a completely empty guide response instead of an empty list.

**What changed:** Mustarrd now handles a completely absent program guide response the same way it handles an empty list, instead of crashing.

---

### Fixed: Play and Download no longer fail after you change your download folder in Settings

**What you would notice:** If you ever changed your download or completed folder in Settings (common on Unraid when pointing Mustarrd at a NAS share), clicking Play or Download on any recording would fail with a "403 Forbidden" error. The recording existed on disk but Mustarrd refused to serve it. This happened whether the recording was made before or after the folder change.

**What changed:** Mustarrd now accepts files from both the original default folder and any folder you configured in Settings. Previously it only checked the built-in default location, which meant any custom folder was blocked. Recordings in your old folder keep working after you switch to a new one.

---

### Improved: First-time setup now shows the password minimum length upfront

**What you would notice:** On a fresh install, the Password field on the setup screen now shows "Minimum 8 characters" below it. Before this change, there was no hint. If you typed a short password and clicked Save, you got an error message after the fact. The requirement is now visible before you click anything.

**What changed:** A short note was added below the Password field on the setup and invite-link screens. No other screens are affected and no behavior changed.

---

### Fixed: Searching the program guide with special characters no longer returns wrong results

**What you would notice:** If you typed a `%` sign or multiple underscores into the program guide search box, you got completely wrong results. Searching just `%` returned every program in the database regardless of what you were looking for. Searching a string of underscores like `_____` could cause a slow, unresponsive search. Any literal search that happened to contain these characters was broken.

**What changed:** Mustarrd now treats `%` and `_` as plain text when you type them in the search box, rather than as special database wildcard characters. Search results now match what you actually typed.

---

### Fixed: ComSkip commercial removal no longer fails when it produces unusual timing values

**What you would notice:** After a recording finished and ComSkip ran to remove commercials, the download would sometimes end up marked as Failed with a cryptic error message. This happened when ComSkip produced an EDL file (its list of commercial segments) with a timing value it could not compute, such as `N/A`. The rest of the recording was fine but the whole post-processing step was aborted.

**What changed:** Mustarrd now skips over any timing line in ComSkip's output that it cannot read, logs a warning so you can see it happened, and continues processing the rest of the file. A single bad line from ComSkip no longer causes the entire commercial-skip step to fail.

---

### Improved: History tabs now explain what they are for when empty

**What you would notice:** The Downloads page and the Scheduled Recordings page each have a History tab that shows past activity. Before this change, landing on an empty History tab showed a single line saying "No download history yet." or "No schedule history yet." with no further explanation. There was no indication of what would appear there or how to populate it.

**What changed:** Each empty History tab now shows a short description below the heading explaining what the tab is for and what causes entries to appear there. The description is dimmed so it does not distract once the tab has content.

---

### Fixed: Downloads grabbed the wrong show on many providers

**What you would notice:** When you clicked a program in the guide and started a download, Mustarrd sometimes downloaded a different show, typically one that aired two or more hours away from what you selected. This was most noticeable if your provider uses European or other non-UTC time zones.

**What changed:** Mustarrd now correctly reads the time format that many providers send in their program guide data. Some providers send start times as a 14-digit number (for example, `20260420190000`) instead of the standard format with dashes. The previous code did not recognize this and fell back to a different time, which caused the wrong content to be downloaded. The fix also ensures guide data imported from XMLTV files carries the correct start time through to the download URL.

---

### Fixed: Hardware-accelerated transcoding failed on AMD GPUs

**What you would notice:** If you have an AMD graphics card (including Ryzen CPUs with built-in graphics) and enabled hardware acceleration in Settings, transcoding failed with an error message about an invalid parameter or a failed driver. Intel-based systems were not affected.

**What changed:** Mustarrd was looking in the wrong place in the system to find out which graphics driver to use. It now checks the correct location, which correctly identifies AMD cards and tells the transcoder to use the right driver (`radeonsi`) instead of accidentally trying to load the Intel driver.

---

### Improved: Empty Downloads and Scheduled pages now have a button to Browse

**What you would notice:** If you open Downloads or Scheduled and nothing is there yet, you now see a "Go to Browse" button. Previously the page told you to go to Browse but gave you nothing to click.

**What changed:** Both empty-state pages now include a button that takes you directly to the Browse page so you can find something to download.

---

### Fixed: Special characters in your IPTV password broke channel browsing and guide updates

**What you would notice:** If your IPTV provider gave you a password that included a `+`, `&`, `=`, or `%` character (common with auto-generated passwords), Mustarrd showed "Failed to load channels from provider" and the program guide would not update, even though the password was correct. Downloads were not affected by this change.

**What changed:** Mustarrd now correctly encodes special characters when it sends your credentials to your provider for channel lookups and guide data requests. A `+` is sent as `+`, not misread as a space.

---

### Fixed: Cancelling a download left a partial file on disk

**What you would notice:** If you cancelled a download partway through, the partially-written file stayed in your downloads folder and was never cleaned up. On a busy system with slow providers that need retries, these leftover files could quietly fill your disk over time.

**What changed:** Cancelling a download now deletes the partial file immediately, the same way a failed download already did.

---

### Fixed: Running out of disk space left a partial file that kept the disk full

**What you would notice:** If a download ran out of disk space mid-transfer, Mustarrd correctly marked it as Failed, but left the partial file on disk. Because the disk was still full, every download after that also failed. A non-technical user had no obvious way to recover without manually finding and deleting the file.

**What changed:** When a download fails because the disk is full, Mustarrd now automatically deletes the partial file. Disk space is freed immediately and the next download can proceed.

---

### Fixed: Hidden characters in program titles caused downloads to fail silently

**What you would notice:** Some IPTV providers embed invisible control characters inside program titles in their guide data. When Mustarrd tried to create a folder for that download, it failed with a cryptic internal error and marked the download as Failed with no useful message shown to the user.

**What changed:** Mustarrd now strips control characters (including null bytes) from program titles before creating folders or filenames. The download proceeds normally regardless of what characters the provider's guide data contains.

---

### Fixed: Downloading a program outside the catchup window silently created an empty file

**What you would notice:** If you tried to download a recording that was older than your provider's catchup window (usually 3 to 7 days back), the download would show as Completed and a 0-byte file would appear in your completed folder. Playing it in Plex or Jellyfin would fail silently. Scheduled recordings that were delayed past the catchup window by a full disk could quietly accumulate empty files.

**What changed:** Mustarrd now checks whether the provider actually sent any content. If it receives an empty response, it marks the download as Failed with the message "Provider returned an empty response. The catchup window for this program may have expired." The empty file is also deleted automatically.

---

### Improved: REC indicator now only shows when a download is active

**What you would notice:** Previously, a red pulsing REC dot appeared in the corner on every screen, even on the login page when nothing was recording. Many users read the constant red pulse as an alarm or error. The indicator now only appears when at least one download is actually in progress.

**What changed:** The REC dot is now hidden when there are no active downloads. It appears and pulses normally as soon as a download starts.

---

### Improved: Save Settings button now only appears when you have changes pending

**What you would notice:** The Settings page used to always show a grayed-out "Save Settings" button, even when you had not changed anything. This was confusing because the button looked disabled or broken. The button now stays hidden until you actually edit a setting, at which point it turns orange and becomes clickable.

**What changed:** The "Save Settings" button now follows the same pattern as "Discard Changes": both buttons only appear when there is something to act on.

---

### Improved: Browse setup buttons are now clearer for first-time users

**What you would notice:** When no IPTV account is connected, the Browse page used to show two buttons ("Open Setup" and "Add Account") with no explanation of which to press first. The buttons now have clearer labels and helper text below each one. "Open Setup" is now "Start Setup Wizard" with the note "Guided walkthrough for first-time setup." The "Add Account" button keeps its label but now has the note "Already set up? Add another IPTV account" below it so returning users know it is for them.

**What changed:** Button labels and descriptions on the Browse page were updated. No functionality changed.

---

### Improved: Setup wizard no longer shows a grayed-out Continue button before your account is saved

**What you would notice:** When you first open Mustarrd and reach the account setup step, you previously saw two buttons: an orange "Save & Continue" and a grayed-out "Continue" with no explanation of why it was disabled. This looked broken. The Continue button is now hidden until you have successfully saved your first account, at which point it appears if you want to skip adding a second account.

**What changed:** The Continue button on the account setup step is now hidden until an account has been saved, instead of always being shown in a disabled state.

---

### Improved: Scheduled Recordings page now explains what it is for and how to schedule a show

**What you would notice:** When you open Scheduled Recordings and the list is empty, the page now shows a brief description explaining that scheduled recordings let you set programs to download automatically when they air. It also tells you to go to Browse, find an upcoming show, and click Schedule to set one up. The "Go to Browse" button is now also orange, matching other primary action buttons in the app.

**What changed:** A short description and instructions were added to the Scheduled Recordings empty state. The "Go to Browse" button was changed from a faded style to the standard orange button.

---

### Fixed: Browse showed no catchup-available channels on some providers

**What you would notice:** On certain IPTV providers, the Browse page loaded your channel list but none of the channels showed the mustard-yellow border that indicates catchup is available. The page appeared to offer nothing to download even when your plan included full catchup access.

**What changed:** Some providers send the catchup flag for a channel as the text "1" instead of the number 1. Mustarrd now accepts both formats. Channels with catchup available now highlight correctly regardless of how the provider sends the flag.

---

### Improved: Downloads page empty state is clearer and easier to act on

**What you would notice:** When you open Downloads and nothing has been downloaded yet, the "Go to Browse" button is now orange and easy to spot. Before this change it appeared in a faded olive color that blended into the background and looked like it might be disabled. A short note now explains that this is where your completed downloads will appear.

**What changed:** The "Go to Browse" button on the empty Downloads page now uses the standard orange button style, and a one-line explanation was added below the page heading.

---

### Fixed: Programs with missing timestamps no longer crash with a server error

**What you would notice:** Some IPTV providers leave start and end times out of certain entries in their program guide. If you clicked on one of those entries in Browse, Mustarrd returned a generic server error with no explanation. It now shows a clear message saying the program has no valid time information.

**What changed:** Mustarrd now returns a readable error message when a program in the guide has no valid start or end time, instead of crashing with an internal server error.

---

### Fixed: Retrying a cancelled download no longer leaves it stuck as Pending forever

**What you would notice:** If you cancelled a download and then clicked Retry, the download would show as Pending and never start. The only way to get it moving was to restart Mustarrd.

**What changed:** When a download is cancelled, Mustarrd marks it internally so the download worker knows to stop processing it. The Retry action was not clearing that mark, so the worker silently ignored the download after it was re-queued. Retrying now clears the mark correctly so the download starts.

---

## 2026-04-29

### Fixed: Show times now display in the channel's local time

**What you would notice:** Programs in the Browse grid and in the download and schedule dialogs were showing times based on your browser's timezone instead of the channel's own local time. If you are in a different timezone from your provider, the displayed times were off. This has been fixed.

**What changed:** The app now anchors all displayed times to the channel airtime so every user sees the same time regardless of where their browser is located.

---

## 2026-04-02

### Improved: Security hardening and simpler Settings page

**What you would notice:** The Settings page no longer has a separate guide settings section. Those options are now part of per-account configuration. You will not notice any visible difference from the security changes during normal use, but the app now has stronger protection against cross-site request forgery.

**What changed:** CSRF protection was hardened across all API endpoints. Legacy account credentials are migrated to a more secure storage format on startup.

---

## 2026-03-31

### Improved: Per-account catchup window and EPG time offset

**What you would notice:** Each IPTV account in Settings can now have its own catchup days setting. If one provider offers 7 days back and another offers 3, each is handled correctly. You can also set a time offset per account if your provider's EPG guide times are off by a few hours.

**What changed:** Catchup availability is now calculated per account instead of using one global setting. A new guide offset field appears on each account in Settings.

---

## 2026-03-16

### Improved: Hardware acceleration status visible in Settings

**What you would notice:** The Settings page now shows a diagnostic report for your GPU. This makes it easier to confirm that hardware-accelerated transcoding is being detected and used correctly.

**What changed:** The backend reports VAAPI driver status and GPU device info at startup. This report is visible in Settings under the transcoding section.

---

## 2026-03-04

### Improved: Podman container support

**What you would notice:** If you run Mustarrd inside a Podman container instead of Docker, it now correctly detects the container runtime. This fixes a detection issue that could cause problems on Podman-based systems.

**What changed:** The startup configuration now checks for Podman-specific signals alongside the existing Docker check.

---

## 2026-02-25

### Improved: Plex integration login flow and navigation

**What you would notice:** Connecting Mustarrd to Plex is smoother. The Plex sign-in step no longer asks for an extra confirmation click. The Settings page has cleaner labels ("Plex Integration" and a Users section) so it is easier to find what you need.

**What changed:** Admin download screens also now show who requested each recording when the Plex integration is active.

---

### Improved: Plex integration setup and unified login

**What you would notice:** The guided setup flow for linking Mustarrd to Plex runs end-to-end without extra steps. Plex users signing in to request recordings get a unified login screen.

**What changed:** Credentials are unified so users do not need to sign in twice. Logging around Plex connection errors is clearer.

---
