# Changelog

All notable changes to Mustarrd are listed here. Most recent changes are at the top.

---

## 2026-05-18

### Fixed: Show times now display in the channel's local time

**What you would notice:** Programs in the Browse grid and in the download and schedule dialogs were showing times based on your browser's timezone instead of the channel's own local time. If you are in a different timezone from your provider, the displayed times were off. This has been fixed.

**What changed:** The app now anchors all displayed times to the channel airtime so every user sees the same time regardless of where their browser is located.

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
