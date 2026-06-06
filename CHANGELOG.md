# Changelog

All notable changes to Mustarrd are listed here. Most recent changes are at the top.

---

## 2026-06-06

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
