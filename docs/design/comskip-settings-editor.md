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

Icon: `IconScissors` (not `IconAdjustments`). The section is about cutting commercials out of recordings.

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
| `detect_method` | Detection methods | 107 | Which signals Comskip looks for when finding commercial boundaries. 107 enables black frames, logo presence, resolution change, aspect ratio changes, and silence detection. Recommended: 107. Higher values add rarely-useful signals and slow processing. |

`detect_method` is a bitmask. UI: a `CheckboxGroup` (visible checkboxes, not a dropdown) with human labels:

- `1` Black frames
- `2` Logo detection
- `4` Scene change
- `8` Resolution change
- `32` Aspect ratio change
- `64` Silence detection

Default `detect_method = 107` pre-checks: 1, 2, 8, 32, 64 (black frames, logo, resolution change, aspect ratio, silence). Scene change (4) is **not** included in 107 and must not be pre-checked.

(Values 16 = closed captions and 128 = cutscenes omitted: rarely available or effective on IPTV streams.)

### Commercial Timing

| Field | Label | Default | Tooltip | Validation |
|-------|-------|---------|---------|------------|
| `max_commercialbreak` | Max commercial break (seconds) | 600 | Longest stretch of continuous commercials Comskip will mark as a single break. Increase if your provider runs long ad blocks. | Must be >= `min_commercialbreak`. |
| `min_commercialbreak` | Min commercial break (seconds) | 25 | Shortest stretch Comskip will call a commercial break. Lower values may cause false positives on short scene transitions. | Must be <= `max_commercialbreak`. |
| `max_commercial_size` | Max single commercial (seconds) | 125 | Longest a single commercial can be. Spots longer than this are treated as show content. | Must be >= `min_commercial_size`. |
| `min_commercial_size` | Min single commercial (seconds) | 4 | Shortest a single commercial can be. Raise this to avoid false cuts on brief logo bumpers. | Must be <= `max_commercial_size`. |

Save Settings is disabled (with an inline error message) if `min_commercialbreak > max_commercialbreak` or `min_commercial_size > max_commercial_size`.

### Show Protection

| Field | Label | Default | Tooltip | Validation |
|-------|-------|---------|---------|------------|
| `always_keep_first_seconds` | Always keep first N seconds | 0 | Never mark this many seconds at the start of the recording as commercial, regardless of what Comskip detects. Useful for providers that play a logo intro before the show. | |
| `always_keep_last_seconds` | Always keep last N seconds | 60 | Never mark this many seconds at the end of the recording as commercial. Prevents accidental cutting of end credits or a post-credits scene. | |
| `remove_before` | Remove N seconds before each break | 0 | Extra seconds of show content to cut immediately before each detected commercial block. Use with caution: removes show content. | |
| `remove_after` | Remove N seconds after each break | 0 | Extra seconds of show content to cut immediately after each detected commercial block. | |
| `thread_count` | Processing threads | 1 | Number of CPU threads Comskip uses. More threads = faster processing but more CPU load during recording. Maximum: 16. | Clamped to 1..16 (enforced in backend validation and as `min=1, max=16` on the NumberInput). |

---

## Reset to Defaults button

Appears at the bottom of the section, alongside the existing Save Settings button:

```
[Reset to Defaults]   [Save Settings]
```

"Reset to Defaults" restores all Comskip fields to the values in the table above without saving, leaving the user a chance to review before clicking Save.

---

## comskip.ini path handling

`backend/api/settings.py` (line 185-186) auto-fills `comskip_ini_path` from the default location whenever that file exists. This means `comskip_ini_path` is non-null even when the user has never chosen a custom file. The generated settings must not silently lose to the auto-filled default path.

Design:

- Rename the existing hidden `comskip_ini_path` field to `comskip_auto_ini_path` (backend-managed, not shown in the UI). This stores the auto-detected default path.
- Add a new `comskip_custom_ini_path` field (user-supplied, exposed in the UI as an optional text input at the bottom of the section). Label: "Custom Comskip INI path (optional)". Tooltip: "If set, this file overrides the generated settings above. Leave blank to use Comskip's built-in defaults plus the settings on this page."
- Backend precedence at runtime: if `comskip_custom_ini_path` is non-empty, pass it to Comskip and skip generating an INI from stored settings. Otherwise, write a temporary INI from the stored settings (ignoring `comskip_auto_ini_path`).

If the rename of `comskip_ini_path` is a bigger migration than the implementer wants, an alternative: add a `comskip_use_generated_ini` boolean (default true). When true, always generate from settings and ignore `comskip_ini_path`. When false (and `comskip_ini_path` is set), use the named file. The implementer should pick whichever is simpler given the current migration pattern.

---

## Backend changes needed

1. **`models/settings.py`**: add 9 new columns to `AppSettings` (detect_method, max/min_commercialbreak, max/min_commercial_size, always_keep_first/last_seconds, remove_before/after, thread_count), plus `comskip_custom_ini_path` (nullable string).
2. **`backend/database.py`**: `ALTER TABLE` migration for all new columns on startup.
3. **`api/settings.py`**: include all new fields in GET/PUT. Validate `min <= max` pairs and clamp `thread_count` to 1..16 before saving. Remove the auto-fill logic for `comskip_ini_path` (or guard it so it only applies when `comskip_custom_ini_path` is null and `comskip_use_generated_ini` is false).
4. **`services/post_processor.py`**: when running Comskip, check `comskip_custom_ini_path` first. If set, pass it to Comskip. Otherwise, write a temporary `comskip.ini` from the stored settings.

---

## Frontend changes needed

1. **`Settings.jsx`**: add `{ id: 'comskip', label: 'Comskip', icon: IconScissors }` to `ADMIN_SECTIONS`.
2. New `ComskipSection` component in `frontend/src/components/settings/ComskipSection.jsx`:
   - Disabled banner when `formData.comskip_enabled` is false (controls greyed, not hidden).
   - Three subsections with `NumberInput` fields (timing + show protection) and a `CheckboxGroup` for `detect_method`.
   - Tooltips via Mantine `Tooltip` on each label.
   - Inline validation errors if `min_commercialbreak > max_commercialbreak` or `min_commercial_size > max_commercial_size`.
   - "Reset to Defaults" button that calls `setFormData(prev => ({ ...prev, ...COMSKIP_DEFAULTS }))`.
   - Optional text input for `comskip_custom_ini_path` at the bottom of the section.
