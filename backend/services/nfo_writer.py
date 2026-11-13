"""Kodi-format ``.nfo`` sidecars for finished recordings.

Plex (with the XBMC/NFO agent) and Jellyfin both read Kodi's XML, so one
sidecar next to the video file is what makes a recording match the right show
even when the provider's title is not the canonical one.

This module is deliberately free of the database and of the download pipeline:
it takes a video path plus the details of one recording and renders XML. That
keeps it unit-testable without a session, a settings row or a real download.
"""

import json
import logging
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from guide_metadata import GuideMetadata

logger = logging.getLogger(__name__)

NFO_SUFFIX = ".nfo"

# Which provider id becomes Kodi's default match, best first. Gracenote is
# written when we have it but is never the default: scrapers key off the
# TMDB/TVDB/IMDb triple.
_DEFAULT_ID_PRIORITY = ("tmdb", "tvdb", "imdb")
_ID_ATTRIBUTES = {
    "tmdb": "tmdb_id",
    "tvdb": "tvdb_id",
    "imdb": "imdb_id",
    "gracenote": "gracenote_id",
}
_ID_ORDER = _DEFAULT_ID_PRIORITY + ("gracenote",)


@dataclass(frozen=True)
class RecordingDetails:
    """Everything the sidecar can say about one recording.

    ``show_title`` is the program title as the guide published it — the series
    name for an episodic recording, the film's name otherwise. The episode's
    own title comes from ``metadata.subtitle``.
    """

    show_title: str
    plot: Optional[str] = None
    aired: Optional[date] = None
    runtime_minutes: Optional[int] = None
    metadata: GuideMetadata = field(default_factory=GuideMetadata)


def details_from_download(download) -> RecordingDetails:
    """Read the details for one finished recording off its download row.

    Duck-typed on purpose: anything carrying ``program_title``,
    ``guide_metadata_json``, ``program_start`` and a duration works, so this
    stays testable without the ORM.

    Runtime prefers the probed duration of the finished file over the guide's
    program duration, because the file is what a player will actually get:
    padding, a short recording and commercial removal all move it.
    """
    payload = {}
    raw = getattr(download, "guide_metadata_json", None)
    if raw:
        try:
            loaded = json.loads(raw)
        except (TypeError, ValueError):
            loaded = None
        if isinstance(loaded, dict):
            payload = loaded

    recorded_seconds = getattr(download, "recorded_duration_seconds", None)
    if recorded_seconds and recorded_seconds > 0:
        runtime_minutes = max(1, round(recorded_seconds / 60))
    else:
        runtime_minutes = getattr(download, "duration_minutes", None)

    return RecordingDetails(
        show_title=_text(getattr(download, "program_title", None)) or "",
        plot=payload.get("description"),
        aired=getattr(download, "program_start", None),
        runtime_minutes=runtime_minutes,
        metadata=GuideMetadata.from_guide_entry(payload),
    )


def _text(value) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _date_text(value) -> Optional[str]:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return _text(value)


def _append(parent: ET.Element, tag: str, value) -> None:
    """Add ``<tag>value</tag>``, or nothing at all when there is no value.

    An empty element is worse than a missing one: scrapers treat
    ``<title/>`` as an assertion that the recording has no title.
    """
    text = _text(value)
    if text is None:
        return
    ET.SubElement(parent, tag).text = text


def _append_unique_ids(parent: ET.Element, metadata: GuideMetadata) -> None:
    default_written = False
    for id_type in _ID_ORDER:
        value = _text(getattr(metadata, _ID_ATTRIBUTES[id_type], None))
        if value is None:
            continue
        element = ET.SubElement(parent, "uniqueid", {"type": id_type})
        if not default_written and id_type in _DEFAULT_ID_PRIORITY:
            element.set("default", "true")
            default_written = True
        element.text = value


def build_element(details: RecordingDetails) -> ET.Element:
    """The Kodi XML tree for one recording."""
    metadata = details.metadata or GuideMetadata()
    season_episode = metadata.season_episode
    runtime = details.runtime_minutes
    runtime_text = str(runtime) if runtime and int(runtime) > 0 else None
    aired = _date_text(details.aired)

    if season_episode is not None:
        season, episode = season_episode
        root = ET.Element("episodedetails")
        # Kodi's <title> on an episode is the episode's own name. Falling back
        # to the show title keeps the element populated for providers that
        # publish no sub-title at all.
        _append(root, "title", _text(metadata.subtitle) or details.show_title)
        _append(root, "showtitle", details.show_title)
        _append(root, "season", season)
        _append(root, "episode", episode)
        _append(root, "plot", details.plot)
        _append(root, "aired", aired)
        _append(root, "runtime", runtime_text)
    else:
        root = ET.Element("movie")
        _append(root, "title", details.show_title)
        _append(root, "plot", details.plot)
        _append(root, "premiered", aired)
        _append(root, "runtime", runtime_text)
        for category in metadata.categories:
            _append(root, "genre", category)

    _append_unique_ids(root, metadata)
    return root


def render_nfo(details: RecordingDetails) -> str:
    """The sidecar's file contents. Escaping is the XML writer's job, not ours."""
    root = build_element(details)
    ET.indent(root, space="    ")
    body = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n{body}\n'


def sidecar_path_for(video_path: str) -> str:
    """The ``.nfo`` path beside ``video_path``, same directory and basename."""
    return os.path.splitext(video_path)[0] + NFO_SUFFIX


def write_sidecar(video_path: str, details: RecordingDetails) -> Optional[str]:
    """Write the sidecar beside ``video_path``, replacing any existing one.

    Returns the path written, or ``None`` when it could not be written. A
    sidecar is a nicety: an unwritable completed folder must never turn a
    finished recording into a failed one, so every failure is logged and
    swallowed here rather than raised at the caller.
    """
    if not video_path:
        return None
    try:
        target = sidecar_path_for(video_path)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(render_nfo(details))
        return target
    except Exception as exc:
        logger.warning("Could not write NFO sidecar for %s: %s", video_path, exc)
        return None
