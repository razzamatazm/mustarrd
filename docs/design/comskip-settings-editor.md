# Design: Comskip Settings Editor

**Status:** Proposed  
**Requested by:** Tyler (2026-06-06)  
**Branch:** design/comskip-settings-editor

---

## What we are building

A new "Comskip" section in the Settings page that lets users tune how Comskip detects commercials, with a tooltip on each label explaining the setting and its recommended value, and a "Reset to Defaults" button.

---

## Where it lives

New entry in `ADMIN_SECTIONS` in `Settings.jsx`, inserted after "Post-Processing":

```
Accounts | Users | Plex Integration | Recording | Post-Processing | Comskip | File Naming | Appearance | Security | Logs
```

---

## When Comskip is disabled

Display the section but grey out all controls and show a banner:

```
┌────────────────────────────────────────────────────────────────┐
│  ⓘ  Comskip is not enabled. Turn it on in Post-Processing      │
│     to configure these settings.                               │
└────────────────────────────────────────────────────────────────┘
```

This lets users pre-configure settings before enabling Comskip.

---

## Settings to expose

Nine settings, grouped into three subsections. All values stored in `app_settings` in the database. Comskip receives them via a generated `comskip.ini` written at runtime.

### Commercial Detection

| Field | Label | Default | Tooltip |
|-------|-------|---------|---------|
| `detect_method` | Detection methods | 107 | Which signals Comskip looks for when finding commercial boundaries. 107 uses black frames, logo presence, scene changes, fuzzy logic, and aspect ratio changes. Recommended: 107 (all practical methods). Higher values add rarely-useful signals and slow processing. |

`detect_method` is a bitmask. UI: a multi-select checklist with human labels:

- `1` Black frames
- `2` Logo detection
- `4` Scene change
- `8` Fuzzy logic
- `32` Aspect ratio change
- `64` Silence detection

(Values 16 = closed captions and 128 = cutscenes omitted: rarely available or effective on IPTV streams.)

### Commercial Timing

| Field | Label | Default | Tooltip |
|-------|-------|---------|---------|
| `max_commercialbreak` | Max commercial break (seconds) | 600 | Longest stretch of continuous commercials Comskip will mark as a single break. Increase if your provider runs long ad blocks. |
| `min_commercialbreak` | Min commercial break (seconds) | 25 | Shortest stretch Comskip will call a commercial break. Lower values may cause false positives on short scene transitions. |
| `max_commercial_size` | Max single commercial (seconds) | 125 | Longest a single commercial can be. Spots longer than this are treated as show content. |
| `min_commercial_size` | Min single commercial (seconds) | 4 | Shortest a single commercial can be. Raise this to avoid false cuts on brief logo bumpers. |

### Show Protection

| Field | Label | Default | Tooltip |
|-------|-------|---------|---------|
| `always_keep_first_seconds` | Always keep first N seconds | 0 | Never mark this many seconds at the start of the recording as commercial, regardless of what Comskip detects. Useful for providers that play a logo intro before the show. |
| `always_keep_last_seconds` | Always keep last N seconds | 60 | Never mark this many seconds at the end of the recording as commercial. Prevents accidental cutting of end credits or a post-credits scene. |
| `remove_before` | Remove N seconds before each break | 0 | Extra seconds of show content to cut immediately before each detected commercial block. Use with caution: removes show content. |
| `remove_after` | Remove N seconds after each break | 0 | Extra seconds of show content to cut immediately after each detected commercial block. |
| `thread_count` | Processing threads | 1 | Number of CPU threads Comskip uses. More threads = faster processing but more CPU load during recording. Maximum: 16. |

---

## Reset to Defaults button

Appears at the bottom of the section, alongside the existing Save Settings button:

```
[Reset to Defaults]   [Save Settings]
```

"Reset to Defaults" restores all Comskip fields to the values in the table above without saving, leaving the user a chance to review before clicking Save.

---

## Backend changes needed

1. **`models/settings.py`**: add 9 new columns to `AppSettings` with the defaults shown above.
2. **`backend/database.py`**: `ALTER TABLE` migration for all 9 columns on startup.
3. **`api/settings.py`**: include the 9 fields in the GET/PUT settings endpoints.
4. **`services/post_processor.py`**: when running Comskip, write a temporary `comskip.ini` from the stored settings instead of (or in addition to) the user-supplied `comskip_ini_path`. If the user has supplied a custom path, the custom file takes precedence.

---

## Frontend changes needed

1. **`Settings.jsx`**: add `{ id: 'comskip', label: 'Comskip', icon: IconAdjustments }` to `ADMIN_SECTIONS`.
2. New `ComskipSection` component in `frontend/src/components/settings/ComskipSection.jsx`:
   - Disabled banner when `formData.comskip_enabled` is false.
   - Three subsections with `NumberInput` fields (timing), a multi-select checklist (`detect_method`), and tooltips via Mantine `Tooltip` on each label.
   - "Reset to Defaults" button that calls `setFormData(prev => ({ ...prev, ...COMSKIP_DEFAULTS }))`.

---

## Questions for Pixel

1. Should the "Comskip disabled" state be a full-section alert, or just grey out the controls inline?
2. Is a `CheckboxGroup` or a `MultiSelect` dropdown preferred for the `detect_method` bitmask?
3. Icon: `IconAdjustments` or `IconScissors` for the section nav?
