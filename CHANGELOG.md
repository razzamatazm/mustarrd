# Changelog

All notable changes to Mustarrd are listed here. Most recent changes are at the top.

---

## 2026-06-07

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
