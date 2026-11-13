"""Poster art (#443): a finished recording gets a poster, and nothing else changes.

Driven against a real in-memory SQLite database and a real temp folder
standing in for the library. The two things on the far side of the network
are faked at their seams: ``FakeJsonClient`` answers for TMDB and TVMaze, and
``FakeImageCache`` stands in for the disk-backed image fetcher.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from api import downloads as downloads_api  # noqa: E402
from api import settings as settings_api  # noqa: E402
from auth import AuthContext  # noqa: E402
from database import Base, _apply_lightweight_migrations  # noqa: E402
from models import AppSettings, Download, DownloadStatus, User, XtreamAccount  # noqa: E402
from services import logo_cache as logo_cache_module  # noqa: E402
from services import poster_art as svc  # noqa: E402
from services.poster_lookup import (  # noqa: E402
    LOOKUP_USER_AGENT,
    TMDB_API_BASE,
    TMDB_IMAGE_BASE,
    TVMAZE_API_BASE,
    FakeJsonClient,
)
from services.provider_http import DEFAULT_PROVIDER_USER_AGENT  # noqa: E402
from services.recording_events import (  # noqa: E402
    RECORDING_COMPLETED,
    RECORDING_FAILED,
    RECORDING_STARTED,
    RecordingEventBus,
)

JPEG = b"\xff\xd8\xff\xe0" + b"poster-bytes" * 4
SMALL_JPEG = b"\xff\xd8\xff\xe0" + b"thumb"
OTHER_JPEG = b"\xff\xd8\xff\xe0" + b"a-different-poster" * 4
PNG = b"\x89PNG\r\n\x1a\n" + b"png-bytes" * 4
HTML = b"<html><body>Not found</body></html>"

TVMAZE_SEARCH = f"{TVMAZE_API_BASE}/search/shows"
TVMAZE_ORIGINAL = "https://static.tvmaze.com/uploads/images/original_untouched/1/bluey.jpg"
TVMAZE_MEDIUM = "https://static.tvmaze.com/uploads/images/medium_portrait/1/bluey.jpg"
TMDB_KEY = "example-short-tmdb-key"


def _tvmaze_answer(name="Bluey"):
    return (
        200,
        [{"show": {"name": name, "image": {"original": TVMAZE_ORIGINAL, "medium": TVMAZE_MEDIUM}}}],
    )


def _admin(user_id=1):
    return AuthContext(authenticated=True, role="admin", user_id=user_id, is_admin=True)


def _viewer(user_id):
    return AuthContext(authenticated=True, role="download_only", user_id=user_id, is_admin=False)


class FakeImageCache:
    """Stands in for ``PosterCache``: a table of image URL -> bytes."""

    def __init__(self, images=None):
        self.images = dict(images or {})
        self.fetched = []
        self.read_from_disk = []
        self.forgotten = []

    async def get_image(self, url):
        self.fetched.append(url)
        content = self.images.get(url)
        return None if content is None else (content, "image/jpeg")

    def cached_image(self, url):
        self.read_from_disk.append(url)
        content = self.images.get(url)
        return None if content is None else (content, "image/jpeg")

    def forget(self, url):
        self.forgotten.append(url)


class PosterArtTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all(
                [
                    User(id=1, username="admin", role="admin", status="active"),
                    User(id=2, username="alice", role="download_only", status="active"),
                    User(id=3, username="bob", role="download_only", status="active"),
                    XtreamAccount(
                        id=1, name="Trex", server_url="http://p.test", username="u", password=""
                    ),
                    AppSettings(id=1),
                ]
            )
            await session.commit()

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.library = Path(self._tmp.name)

        self.http = FakeJsonClient({TVMAZE_SEARCH: _tvmaze_answer()})
        self.cache = FakeImageCache({TVMAZE_ORIGINAL: JPEG, TVMAZE_MEDIUM: SMALL_JPEG})
        self.service = svc.PosterArtService(
            cache=self.cache, http=self.http, session_maker=self.Session
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    def _video(self, relative="Bluey - S03E12 - Sleepytime.mkv"):
        path = self.library / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
        return str(path)

    async def _recording(self, **fields):
        defaults = dict(
            account_id=1,
            channel_id="10",
            channel_name="ABC Kids",
            program_title="Bluey",
            program_start=datetime(2026, 11, 12, 18, 0),
            program_end=datetime(2026, 11, 12, 18, 10),
            duration_minutes=10,
            source_url="http://p.test/timeshift/secret",
            output_path="",
            status=DownloadStatus.COMPLETED.value,
            guide_metadata_json=json.dumps({"season": 3, "episode": 12}),
            requested_by_user_id=2,
        )
        defaults.update(fields)
        if "output_path" not in fields:
            defaults["output_path"] = self._video()
        async with self.Session() as session:
            download = Download(**defaults)
            session.add(download)
            await session.commit()
            await session.refresh(download)
            return download

    async def _reload(self, download_id):
        async with self.Session() as session:
            return (
                await session.execute(select(Download).where(Download.id == download_id))
            ).scalar_one()

    async def _settings(self, **changes):
        async with self.Session() as session:
            settings = (await session.execute(select(AppSettings))).scalar_one()
            for key, value in changes.items():
                setattr(settings, key, value)
            await session.commit()

    def _library_files(self):
        return sorted(
            str(p.relative_to(self.library)) for p in self.library.rglob("*") if p.is_file()
        )


class FinishedRecordingGetsAPosterTests(PosterArtTestCase):
    async def test_the_poster_is_remembered_and_History_can_load_it(self):
        download = await self._recording()

        outcome = await self.service.attach(download.id)

        self.assertTrue(outcome.found)
        stored = await self._reload(download.id)
        self.assertEqual(stored.poster_url, TVMAZE_ORIGINAL)
        self.assertEqual(stored.poster_thumb_url, TVMAZE_MEDIUM)
        self.assertEqual(stored.poster_source, "tvmaze")
        poster = stored.to_dict()["poster"]
        self.assertTrue(poster.startswith(f"/api/downloads/{download.id}/poster?v="))
        self.assertNotIn("tvmaze", poster, "The browser is never sent to the poster's host.")

    async def test_a_recording_with_no_poster_reports_none(self):
        download = await self._recording(program_title="News at Ten", guide_metadata_json=None)

        outcome = await self.service.attach(download.id)

        self.assertFalse(outcome.found)
        stored = await self._reload(download.id)
        self.assertIsNone(stored.poster_url)
        self.assertIsNone(stored.to_dict()["poster"])
        self.assertEqual(stored.status, DownloadStatus.COMPLETED.value)

    async def test_a_saved_tmdb_key_puts_tmdb_first(self):
        self.http.routes[f"{TMDB_API_BASE}/search/tv"] = (
            200,
            {"results": [{"name": "Bluey", "poster_path": "/tmdb-bluey.jpg"}]},
        )
        self.cache.images[f"{TMDB_IMAGE_BASE}/tmdb-bluey.jpg"] = JPEG
        await self._settings(tmdb_api_key_encrypted=svc.encrypt_tmdb_api_key(TMDB_KEY))
        download = await self._recording()

        await self.service.attach(download.id)

        stored = await self._reload(download.id)
        self.assertEqual(stored.poster_source, "tmdb")
        self.assertEqual(stored.poster_url, f"{TMDB_IMAGE_BASE}/tmdb-bluey.jpg")

    async def test_the_smaller_image_is_used_when_the_big_one_will_not_load(self):
        del self.cache.images[TVMAZE_ORIGINAL]
        download = await self._recording()

        await self.service.attach(download.id)

        stored = await self._reload(download.id)
        self.assertEqual(stored.poster_url, TVMAZE_MEDIUM)
        self.assertIsNone(stored.poster_thumb_url, "It is already the small one.")

    async def test_something_that_is_not_an_image_is_not_a_poster(self):
        self.cache.images = {TVMAZE_ORIGINAL: HTML, TVMAZE_MEDIUM: HTML}
        download = await self._recording()

        outcome = await self.service.attach(download.id)

        self.assertFalse(outcome.found)
        self.assertIsNone((await self._reload(download.id)).poster_url)
        self.assertEqual(self._library_files(), ["Bluey - S03E12 - Sleepytime.mkv"])

    async def test_the_thumbnail_is_fetched_up_front_so_History_costs_no_network(self):
        download = await self._recording()

        await self.service.attach(download.id)

        self.assertIn(TVMAZE_MEDIUM, self.cache.fetched)

    async def test_a_batch_of_episodes_costs_one_lookup(self):
        ids = []
        for episode in range(1, 6):
            video = self._video(f"Bluey - S03E{episode:02d}.mkv")
            ids.append((await self._recording(output_path=video)).id)

        for download_id in ids:
            await self.service.attach(download_id)

        self.assertEqual(len(self.http.calls), 1)
        for download_id in ids:
            self.assertEqual((await self._reload(download_id)).poster_url, TVMAZE_ORIGINAL)

    async def test_saving_a_key_is_not_answered_from_an_earlier_nothing_found(self):
        self.http.routes.clear()
        first = await self._recording()
        await self.service.attach(first.id)
        self.assertIsNone((await self._reload(first.id)).poster_url)

        self.http.routes[f"{TMDB_API_BASE}/search/tv"] = (
            200,
            {"results": [{"name": "Bluey", "poster_path": "/b.jpg"}]},
        )
        self.cache.images[f"{TMDB_IMAGE_BASE}/b.jpg"] = JPEG
        await self._settings(tmdb_api_key_encrypted=svc.encrypt_tmdb_api_key(TMDB_KEY))
        second = await self._recording(output_path=self._video("Bluey - S03E13.mkv"))

        await self.service.attach(second.id)

        self.assertEqual((await self._reload(second.id)).poster_source, "tmdb")


class NeverTheRecordingsProblemTests(PosterArtTestCase):
    async def test_a_lookup_that_blows_up_does_not_raise(self):
        self.service._http = MagicMock()
        self.service._http.get = AsyncMock(side_effect=RuntimeError("network down"))
        download = await self._recording()

        outcome = await self.service.attach(download.id)

        self.assertFalse(outcome.found)
        self.assertEqual((await self._reload(download.id)).status, DownloadStatus.COMPLETED.value)

    async def test_an_image_fetch_that_blows_up_does_not_raise(self):
        self.cache.get_image = AsyncMock(side_effect=OSError("disk gone"))
        download = await self._recording()

        self.assertFalse((await self.service.attach(download.id)).found)

    async def test_an_unwritable_library_still_gets_the_thumbnail(self):
        download = await self._recording()

        with patch("services.poster_art.open", side_effect=PermissionError("read-only")):
            outcome = await self.service.attach(download.id)

        self.assertTrue(outcome.found)
        self.assertIsNone(outcome.library_path)
        self.assertEqual((await self._reload(download.id)).poster_url, TVMAZE_ORIGINAL)

    async def test_on_demand_downloads_are_left_alone(self):
        download = await self._recording(is_vod=True)

        outcome = await self.service.attach(download.id)

        self.assertEqual(outcome.status, svc.OUTCOME_SKIPPED)
        self.assertEqual(self.http.calls, [])

    async def test_a_recording_that_did_not_finish_is_left_alone(self):
        for status in (DownloadStatus.FAILED.value, DownloadStatus.DOWNLOADING.value):
            with self.subTest(status=status):
                download = await self._recording(status=status)
                self.assertEqual((await self.service.attach(download.id)).status, svc.OUTCOME_SKIPPED)
        self.assertEqual(self.http.calls, [])

    async def test_an_interrupted_recording_still_gets_one(self):
        download = await self._recording(status=DownloadStatus.INTERRUPTED.value)

        self.assertTrue((await self.service.attach(download.id)).found)

    async def test_a_recording_that_no_longer_exists_is_not_an_error(self):
        self.assertEqual((await self.service.attach(9999)).status, svc.OUTCOME_SKIPPED)


class SwitchesTests(PosterArtTestCase):
    async def test_turned_off_means_nobody_is_asked_anything(self):
        await self._settings(poster_art_enabled=False)
        download = await self._recording()

        outcome = await self.service.attach(download.id)

        self.assertEqual(outcome.status, svc.OUTCOME_DISABLED)
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.cache.fetched, [])
        self.assertIsNone((await self._reload(download.id)).poster_url)

    async def test_library_copy_off_keeps_the_thumbnail_and_writes_no_file(self):
        await self._settings(poster_art_save_to_library=False)
        download = await self._recording()

        outcome = await self.service.attach(download.id)

        self.assertTrue(outcome.found)
        self.assertIsNone(outcome.library_path)
        self.assertEqual(self._library_files(), ["Bluey - S03E12 - Sleepytime.mkv"])

    async def test_both_switches_default_to_on(self):
        async with self.Session() as session:
            payload = (await session.execute(select(AppSettings))).scalar_one().to_dict()

        self.assertIs(payload["poster_art_enabled"], True)
        self.assertIs(payload["poster_art_save_to_library"], True)


class LibraryFileTests(PosterArtTestCase):
    async def test_in_a_flat_folder_the_poster_takes_the_recordings_name(self):
        download = await self._recording()

        outcome = await self.service.attach(download.id)

        self.assertEqual(
            self._library_files(),
            ["Bluey - S03E12 - Sleepytime-poster.jpg", "Bluey - S03E12 - Sleepytime.mkv"],
        )
        self.assertEqual(Path(outcome.library_path).read_bytes(), JPEG)

    async def test_in_the_shows_own_folder_it_is_the_shows_poster(self):
        for relative in (
            "Bluey/Bluey - S03E12.mkv",
            "Bluey (2018)/Season 03/Bluey - S03E12.mkv",
            "Bluey {tvdb-353546}/Specials/Bluey - S00E01.mkv",
        ):
            with self.subTest(relative=relative):
                download = await self._recording(output_path=self._video(relative))

                outcome = await self.service.attach(download.id)

                show_folder = self.library / relative.split("/")[0]
                self.assertEqual(Path(outcome.library_path), show_folder / "poster.jpg")
                self.assertEqual((show_folder / "poster.jpg").read_bytes(), JPEG)

    async def test_a_folder_shared_with_other_shows_never_gets_a_poster_jpg(self):
        """Grouped by channel or category: one show's face would land on all of it."""
        for relative in ("ABC Kids/Bluey - S03E12.mkv", "Kids/Season 03/Bluey - S03E12.mkv"):
            with self.subTest(relative=relative):
                download = await self._recording(output_path=self._video(relative))

                outcome = await self.service.attach(download.id)

                self.assertTrue(outcome.library_path.endswith("Bluey - S03E12-poster.jpg"))
                self.assertFalse((self.library / relative.split("/")[0] / "poster.jpg").exists())

    async def test_a_film_keeps_its_own_poster_even_in_a_folder_named_after_it(self):
        video = self._video("Alien/Alien (1979).mkv")
        self.http.routes[TVMAZE_SEARCH] = _tvmaze_answer("Alien")
        download = await self._recording(
            program_title="Alien", guide_metadata_json=None, output_path=video
        )

        outcome = await self.service.attach(download.id)

        self.assertTrue(outcome.library_path.endswith("Alien (1979)-poster.jpg"))

    async def test_a_shows_existing_poster_is_not_overwritten(self):
        video = self._video("Bluey/Bluey - S03E12.mkv")
        mine = self.library / "Bluey" / "poster.png"
        mine.write_bytes(PNG)
        download = await self._recording(output_path=video)

        outcome = await self.service.attach(download.id)

        self.assertEqual(Path(outcome.library_path), mine)
        self.assertEqual(mine.read_bytes(), PNG)
        self.assertFalse((self.library / "Bluey" / "poster.jpg").exists())

    async def test_a_poster_that_changes_format_replaces_the_old_file(self):
        video = self._video("Bluey/Bluey - S03E12.mkv")
        self.cache.images[TVMAZE_ORIGINAL] = PNG
        download = await self._recording(output_path=video)
        await self.service.attach(download.id)
        self.assertTrue((self.library / "Bluey" / "poster.png").exists())

        self.cache.images[TVMAZE_ORIGINAL] = JPEG
        await self.service.attach(download.id, replace=True)

        self.assertEqual(
            sorted(p.name for p in (self.library / "Bluey").glob("poster.*")), ["poster.jpg"]
        )

    async def test_the_extension_follows_the_image_not_the_link(self):
        self.cache.images[TVMAZE_ORIGINAL] = PNG
        download = await self._recording()

        outcome = await self.service.attach(download.id)

        self.assertTrue(outcome.library_path.endswith("-poster.png"))

    async def test_a_missing_recording_file_writes_nothing(self):
        download = await self._recording(output_path=str(self.library / "gone.mkv"))

        outcome = await self.service.attach(download.id)

        self.assertTrue(outcome.found, "The thumbnail does not need the file.")
        self.assertIsNone(outcome.library_path)
        self.assertEqual(self._library_files(), [])

    async def test_no_half_written_file_is_left_behind(self):
        download = await self._recording()

        await self.service.attach(download.id)

        self.assertFalse([name for name in self._library_files() if name.endswith(".tmp")])


class RefreshTests(PosterArtTestCase):
    async def test_refresh_looks_again_and_replaces_the_poster(self):
        video = self._video("Bluey/Bluey - S03E12.mkv")
        download = await self._recording(output_path=video)
        await self.service.attach(download.id)
        calls_before = len(self.http.calls)

        new_url = "https://static.tvmaze.com/uploads/images/original_untouched/2/new.jpg"
        self.http.routes[TVMAZE_SEARCH] = (
            200,
            [{"show": {"name": "Bluey", "image": {"original": new_url}}}],
        )
        self.cache.images[new_url] = OTHER_JPEG

        outcome = await self.service.attach(download.id, replace=True)

        self.assertTrue(outcome.found)
        self.assertGreater(len(self.http.calls), calls_before, "A refresh asks again.")
        self.assertIn(new_url, self.cache.forgotten, "A refresh refetches the image.")
        stored = await self._reload(download.id)
        self.assertEqual(stored.poster_url, new_url)
        self.assertEqual((self.library / "Bluey" / "poster.jpg").read_bytes(), OTHER_JPEG)

    async def test_the_poster_address_changes_so_the_browser_reloads_it(self):
        download = await self._recording()
        await self.service.attach(download.id)
        before = (await self._reload(download.id)).to_dict()["poster"]

        new_url = "https://static.tvmaze.com/new.jpg"
        self.http.routes[TVMAZE_SEARCH] = (
            200,
            [{"show": {"name": "Bluey", "image": {"original": new_url}}}],
        )
        self.cache.images[new_url] = OTHER_JPEG
        await self.service.attach(download.id, replace=True)

        self.assertNotEqual((await self._reload(download.id)).to_dict()["poster"], before)

    async def test_a_refresh_that_finds_nothing_keeps_the_old_poster(self):
        download = await self._recording()
        await self.service.attach(download.id)
        self.http.routes.clear()

        outcome = await self.service.attach(download.id, replace=True)

        self.assertFalse(outcome.found)
        self.assertEqual((await self._reload(download.id)).poster_url, TVMAZE_ORIGINAL)

    async def test_finishing_again_reuses_the_poster_without_asking(self):
        """A retry that completes fires the event a second time."""
        download = await self._recording()
        await self.service.attach(download.id)
        os.remove(self.library / "Bluey - S03E12 - Sleepytime-poster.jpg")
        calls_before = len(self.http.calls)

        outcome = await self.service.attach(download.id)

        self.assertTrue(outcome.found)
        self.assertEqual(len(self.http.calls), calls_before)
        self.assertIn("Bluey - S03E12 - Sleepytime-poster.jpg", self._library_files())


class EventSeamTests(PosterArtTestCase):
    async def _publish(self, name, recording):
        bus = RecordingEventBus()
        subscriber = svc.PosterArtSubscriber(self.service)
        bus.subscribe(subscriber)
        bus.publish(name, recording)
        await bus.drain_once()
        await subscriber.wait_idle()

    async def test_a_slow_poster_does_not_keep_the_next_consumer_waiting(self):
        """The bus waits for each subscriber, so the lookup must not run inside it."""
        import asyncio

        from services.recording_events import FakeRecordingSubscriber

        release = asyncio.Event()
        started = asyncio.Event()

        async def slow(_download_id, **_kwargs):
            started.set()
            await release.wait()

        self.service.attach = slow
        download = await self._recording()
        bus = RecordingEventBus()
        posters = svc.PosterArtSubscriber(self.service)
        webhook = FakeRecordingSubscriber()
        bus.subscribe(posters)
        bus.subscribe(webhook)

        bus.publish(RECORDING_COMPLETED, download.to_dict())
        bus.publish(RECORDING_FAILED, download.to_dict())
        await asyncio.wait_for(bus.drain_once(), timeout=2)

        self.assertEqual(webhook.names, [RECORDING_COMPLETED, RECORDING_FAILED])
        await asyncio.wait_for(started.wait(), timeout=2)
        release.set()
        await posters.wait_idle()

    async def test_posters_are_fetched_one_at_a_time(self):
        import asyncio

        running = 0
        most_at_once = 0

        async def counted(_download_id, **_kwargs):
            nonlocal running, most_at_once
            running += 1
            most_at_once = max(most_at_once, running)
            await asyncio.sleep(0.01)
            running -= 1

        self.service.attach = counted
        download = await self._recording()
        bus = RecordingEventBus()
        posters = svc.PosterArtSubscriber(self.service)
        bus.subscribe(posters)
        for _ in range(4):
            bus.publish(RECORDING_COMPLETED, download.to_dict())
        await bus.drain_once()
        await posters.wait_idle()

        self.assertEqual(most_at_once, 1)

    async def test_a_completed_recording_gets_its_poster(self):
        download = await self._recording()

        await self._publish(RECORDING_COMPLETED, download.to_dict())

        self.assertEqual((await self._reload(download.id)).poster_url, TVMAZE_ORIGINAL)

    async def test_other_events_do_nothing(self):
        download = await self._recording()

        for name in (RECORDING_STARTED, RECORDING_FAILED):
            await self._publish(name, download.to_dict())

        self.assertEqual(self.http.calls, [])

    async def test_a_poster_that_takes_too_long_is_given_up_on(self):
        import asyncio

        async def never(_download_id, **_kwargs):
            await asyncio.Event().wait()

        self.service.attach = never
        download = await self._recording()

        with patch.object(svc, "ATTACH_TIMEOUT_SECONDS", 0.05):
            with self.assertLogs("services.poster_art", level="WARNING"):
                await self._publish(RECORDING_COMPLETED, download.to_dict())

    async def test_the_in_app_poster_path_stays_out_of_webhook_messages(self):
        from services.download_manager import DownloadManager

        download = await self._recording()
        await self.service.attach(download.id)
        stored = await self._reload(download.id)
        self.assertIsNotNone(stored.to_dict()["poster"])

        payload = DownloadManager()._event_payload(stored)

        self.assertNotIn("poster", payload)
        self.assertEqual(payload["id"], download.id)

    async def test_startup_registers_it_before_the_bus_starts_draining(self):
        source = (BACKEND_ROOT / "main.py").read_text()

        self.assertLess(
            source.index("register_poster_art_subscriber(recording_events)"),
            source.index("recording_events.run()"),
        )


class TmdbKeySettingTests(PosterArtTestCase):
    async def _save(self, **fields):
        async with self.Session() as session:
            return await settings_api.update_settings(
                settings_api.SettingsUpdate(**fields), _admin=None, session=session
            )

    async def _stored_key_column(self):
        async with self.Session() as session:
            return (await session.execute(select(AppSettings))).scalar_one().tmdb_api_key_encrypted

    async def test_the_key_is_stored_encrypted_and_never_sent_back(self):
        payload = await self._save(tmdb_api_key=f"  {TMDB_KEY}  ")

        self.assertIs(payload["tmdb_api_key_set"], True)
        self.assertNotIn(TMDB_KEY, json.dumps(payload))
        self.assertNotIn("tmdb_api_key", payload)
        stored = await self._stored_key_column()
        self.assertNotIn(TMDB_KEY, stored)
        async with self.Session() as session:
            settings = (await session.execute(select(AppSettings))).scalar_one()
            self.assertEqual(svc.tmdb_api_key_from(settings), TMDB_KEY)

    async def test_an_empty_key_removes_the_saved_one(self):
        await self._save(tmdb_api_key=TMDB_KEY)

        payload = await self._save(tmdb_api_key="")

        self.assertIs(payload["tmdb_api_key_set"], False)
        self.assertIsNone(await self._stored_key_column())

    async def test_saving_other_settings_leaves_the_key_alone(self):
        await self._save(tmdb_api_key=TMDB_KEY)

        payload = await self._save(poster_art_save_to_library=False)

        self.assertIs(payload["tmdb_api_key_set"], True)
        self.assertIs(payload["poster_art_save_to_library"], False)

    async def test_a_key_with_a_line_break_in_it_is_refused_in_a_sentence(self):
        with self.assertRaises(HTTPException) as raised:
            await self._save(tmdb_api_key="abc\ndef")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("TMDB key", raised.exception.detail)

    async def test_a_key_that_cannot_be_decrypted_reads_as_no_key(self):
        settings = AppSettings(tmdb_api_key_encrypted="v1:not-really-encrypted")

        self.assertIsNone(svc.tmdb_api_key_from(settings))

    async def _check(self, typed, answer):
        with patch.object(settings_api, "check_tmdb_api_key", AsyncMock(return_value=answer)) as check:
            async with self.Session() as session:
                result = await settings_api.check_poster_art_key(
                    settings_api.TmdbKeyCheck(api_key=typed), _admin=None, session=session
                )
        return result, check

    async def test_checking_a_typed_key_before_saving_it(self):
        result, check = await self._check(TMDB_KEY, True)

        self.assertIs(result["ok"], True)
        check.assert_awaited_once_with(TMDB_KEY)
        self.assertIsNone(await self._stored_key_column(), "Checking does not save.")

    async def test_checking_with_nothing_typed_checks_the_saved_key(self):
        await self._save(tmdb_api_key=TMDB_KEY)

        result, check = await self._check(None, False)

        self.assertIs(result["ok"], False)
        check.assert_awaited_once_with(TMDB_KEY)

    async def test_checking_with_no_key_anywhere_says_so(self):
        result, check = await self._check(None, True)

        self.assertIs(result["ok"], False)
        check.assert_not_awaited()

    async def test_tmdb_being_down_is_not_reported_as_a_bad_key(self):
        result, _check = await self._check(TMDB_KEY, None)

        self.assertIsNone(result["ok"])
        self.assertIn("not checked", result["message"])

    async def test_the_check_goes_to_tmdb_and_survives_it_blowing_up(self):
        http = FakeJsonClient({f"{TMDB_API_BASE}/configuration": (401, None)})
        self.assertIs(await svc.check_tmdb_api_key(TMDB_KEY, http), False)

        broken = MagicMock()
        broken.get = AsyncMock(side_effect=RuntimeError(f"GET ...?api_key={TMDB_KEY}"))
        with self.assertLogs("services.poster_art", level="WARNING") as logs:
            self.assertIsNone(await svc.check_tmdb_api_key(TMDB_KEY, broken))
        self.assertNotIn(TMDB_KEY, "\n".join(logs.output))


class PosterApiTests(PosterArtTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        patcher = patch.object(downloads_api, "poster_art", self.service)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _get(self, download_id, auth):
        async with self.Session() as session:
            return await downloads_api.get_download_poster(download_id, auth=auth, session=session)

    async def _refresh(self, download_id, auth):
        async with self.Session() as session:
            return await downloads_api.refresh_download_poster(
                download_id, auth=auth, session=session
            )

    async def test_the_poster_is_served_as_an_image_that_cannot_run_anything(self):
        download = await self._recording()
        await self.service.attach(download.id)

        response = await self._get(download.id, _admin())

        self.assertEqual(response.body, SMALL_JPEG, "History gets the thumbnail, not the full poster.")
        self.assertEqual(response.media_type, "image/jpeg")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", response.headers["Content-Security-Policy"])
        self.assertIn("private", response.headers["Cache-Control"])

    async def test_the_full_poster_stands_in_when_the_thumbnail_will_not_load(self):
        download = await self._recording()
        await self.service.attach(download.id)
        del self.cache.images[TVMAZE_MEDIUM]

        response = await self._get(download.id, _admin())

        self.assertEqual(response.body, JPEG)

    async def test_switched_off_shows_what_is_on_disk_and_fetches_nothing(self):
        download = await self._recording()
        await self.service.attach(download.id)
        await self._settings(poster_art_enabled=False)
        self.cache.fetched.clear()

        response = await self._get(download.id, _admin())

        self.assertEqual(response.body, SMALL_JPEG)
        self.assertEqual(self.cache.fetched, [], "Off means no requests to the poster's host.")

    async def test_a_recording_without_a_poster_is_a_404(self):
        download = await self._recording(program_title="News at Ten", guide_metadata_json=None)

        with self.assertRaises(HTTPException) as raised:
            await self._get(download.id, _admin())

        self.assertEqual(raised.exception.status_code, 404)

    async def test_the_owner_can_see_it_and_another_user_cannot(self):
        download = await self._recording(requested_by_user_id=2)
        await self.service.attach(download.id)

        self.assertEqual((await self._get(download.id, _viewer(2))).body, SMALL_JPEG)
        with self.assertRaises(HTTPException) as raised:
            await self._get(download.id, _viewer(3))
        self.assertEqual(raised.exception.status_code, 404)

    async def test_refresh_reports_the_new_poster(self):
        download = await self._recording()

        result = await self._refresh(download.id, _viewer(2))

        self.assertIs(result["found"], True)
        self.assertTrue(result["download"]["poster"].startswith(f"/api/downloads/{download.id}/poster"))

    async def test_only_an_admin_refresh_replaces_a_shows_shared_poster(self):
        video = self._video("Bluey/Bluey - S03E12.mkv")
        shared = self.library / "Bluey" / "poster.jpg"
        shared.write_bytes(OTHER_JPEG)
        download = await self._recording(output_path=video, requested_by_user_id=2)

        await self._refresh(download.id, _viewer(2))
        self.assertEqual(shared.read_bytes(), OTHER_JPEG, "Other people's episodes share it.")

        await self._refresh(download.id, _admin())
        self.assertEqual(shared.read_bytes(), JPEG)

    async def test_refresh_finding_nothing_is_an_answer_not_an_error(self):
        download = await self._recording(program_title="News at Ten", guide_metadata_json=None)

        result = await self._refresh(download.id, _admin())

        self.assertIs(result["found"], False)
        self.assertIsNone(result["download"]["poster"])

    async def test_refresh_is_for_finished_recordings_only(self):
        failed = await self._recording(status=DownloadStatus.FAILED.value)
        on_demand = await self._recording(is_vod=True)

        for download in (failed, on_demand):
            with self.subTest(download=download.id):
                with self.assertRaises(HTTPException) as raised:
                    await self._refresh(download.id, _admin())
                self.assertEqual(raised.exception.status_code, 400)

    async def test_refresh_on_someone_elses_recording_is_a_404(self):
        download = await self._recording(requested_by_user_id=2)

        with self.assertRaises(HTTPException) as raised:
            await self._refresh(download.id, _viewer(3))

        self.assertEqual(raised.exception.status_code, 404)

    async def test_refresh_while_turned_off_says_why(self):
        await self._settings(poster_art_enabled=False)
        download = await self._recording()

        with self.assertRaises(HTTPException) as raised:
            await self._refresh(download.id, _admin())

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("turned off", raised.exception.detail)


class PosterCacheTests(unittest.IsolatedAsyncioTestCase):
    """The real cache: LogoCache's guard and disk format, in its own folder."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(
            logo_cache_module, "ensure_config_files", return_value=Path(self._tmp.name)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(logo_cache_module, "_validate_logo_url", AsyncMock(return_value=True))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _session_factory(self, body):
        response = MagicMock()
        response.status = 200
        response.content_length = len(body)
        response.headers = {"Content-Type": "image/jpeg"}

        async def iter_chunked(_size):
            yield body

        response.content.iter_chunked = iter_chunked
        get_cm = MagicMock()
        get_cm.__aenter__ = AsyncMock(return_value=response)
        get_cm.__aexit__ = AsyncMock(return_value=False)
        http_session = MagicMock()
        http_session.get.return_value = get_cm
        client_cm = MagicMock()
        client_cm.__aenter__ = AsyncMock(return_value=http_session)
        client_cm.__aexit__ = AsyncMock(return_value=False)
        return MagicMock(return_value=client_cm)

    async def test_posters_are_kept_apart_from_logos_and_fetched_under_our_own_name(self):
        factory = self._session_factory(JPEG)

        with patch.object(svc.aiohttp, "ClientSession", factory):
            result = await svc.PosterCache().get_logo(TVMAZE_ORIGINAL)

        self.assertEqual(result[0], JPEG)
        _, kwargs = factory.call_args
        self.assertEqual(kwargs["headers"], {"User-Agent": LOOKUP_USER_AGENT})
        self.assertNotEqual(LOOKUP_USER_AGENT, DEFAULT_PROVIDER_USER_AGENT)
        self.assertTrue(list((Path(self._tmp.name) / "poster_cache").glob("*.img")))
        self.assertFalse((Path(self._tmp.name) / "logo_cache").exists())

    async def test_a_poster_over_the_size_cap_is_refused(self):
        factory = self._session_factory(b"\xff\xd8\xff" + b"x" * svc.POSTER_MAX_BYTES)

        with patch.object(svc.aiohttp, "ClientSession", factory):
            self.assertIsNone(await svc.PosterCache().get_logo(TVMAZE_ORIGINAL))

    async def test_forgetting_an_image_makes_the_next_request_fetch_it_again(self):
        cache = svc.PosterCache()
        with patch.object(svc.aiohttp, "ClientSession", self._session_factory(JPEG)):
            await cache.get_logo(TVMAZE_ORIGINAL)

        cache.forget(TVMAZE_ORIGINAL)
        newer = self._session_factory(OTHER_JPEG)
        with patch.object(svc.aiohttp, "ClientSession", newer):
            result = await cache.get_logo(TVMAZE_ORIGINAL)

        self.assertEqual(result[0], OTHER_JPEG)
        newer.assert_called_once()


class ImageSniffingTests(unittest.TestCase):
    def test_the_three_formats_libraries_read_are_recognised(self):
        self.assertEqual(svc.image_extension(JPEG), ".jpg")
        self.assertEqual(svc.image_extension(PNG), ".png")
        self.assertEqual(svc.image_extension(b"RIFF\x00\x00\x00\x00WEBPVP8 "), ".webp")

    def test_anything_else_is_not_an_image(self):
        for content in (HTML, b"", b"GIF89a", b"<svg xmlns='http://www.w3.org/2000/svg'/>"):
            with self.subTest(content=content[:8]):
                self.assertIsNone(svc.image_extension(content))


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_existing_install_gains_the_columns_with_both_switches_on(self):
        conn = AsyncMock()
        new_columns = {
            ("downloads", "poster_url"),
            ("downloads", "poster_thumb_url"),
            ("downloads", "poster_source"),
            ("app_settings", "poster_art_enabled"),
            ("app_settings", "poster_art_save_to_library"),
            ("app_settings", "tmdb_api_key_encrypted"),
        }

        async def column_exists(_conn, table_name, column_name):
            return (table_name, column_name) not in new_columns

        with patch("database._column_exists", side_effect=column_exists):
            await _apply_lightweight_migrations(conn)

        executed = [str(call.args[0]) for call in conn.execute.await_args_list]
        for statement in (
            "ALTER TABLE downloads ADD COLUMN poster_url TEXT",
            "ALTER TABLE downloads ADD COLUMN poster_thumb_url TEXT",
            "ALTER TABLE downloads ADD COLUMN poster_source VARCHAR(32)",
            "ALTER TABLE app_settings ADD COLUMN poster_art_enabled BOOLEAN DEFAULT 1",
            "ALTER TABLE app_settings ADD COLUMN poster_art_save_to_library BOOLEAN DEFAULT 1",
            "ALTER TABLE app_settings ADD COLUMN tmdb_api_key_encrypted TEXT",
        ):
            self.assertIn(statement, executed)


if __name__ == "__main__":
    unittest.main()
