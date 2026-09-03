"""
Automatic path/query catchup URL fallback (issue #428).

Some Xtream providers only serve catchup at /streaming/timeshift.php. With the
account's catchup URL style left on "auto", the first refusal of the path form
retries the same recording in the query form and remembers what worked, so
later recordings go straight there.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.download_manager import DownloadManager, ProviderStatusError

PATH_URL = "http://provider.test:8080/timeshift/user/pass/60/2024-01-15:20-30/123.ts"
QUERY_URL = (
    "http://provider.test:8080/streaming/timeshift.php"
    "?username=user&password=pass&stream=123&start=2024-01-15:20-30&duration=60"
)


def _account(style="auto", resolved=None):
    account = MagicMock()
    account.id = 7
    account.catchup_url_style = style
    account.catchup_url_style_resolved = resolved
    return account


def _download(url=PATH_URL, output_path="/tmp/show.ts"):
    download = MagicMock()
    download.id = 1
    download.account_id = 7
    download.source_url = url
    download.output_path = output_path
    return download


class TimeshiftStyleFallbackTests(unittest.TestCase):
    def setUp(self):
        self.manager = DownloadManager()
        self.manager._broadcast_log = AsyncMock()
        self.requested = []
        self.session = AsyncMock()
        self.session.commit = AsyncMock()

    def _install_downloader(self, behaviour):
        async def _download_file(url, output_path, download_id, session, offset=0):
            self.requested.append(url)
            return behaviour(url)

        self.manager._download_file = _download_file

    def _install_account(self, account):
        async def _load(session, account_id):
            return account

        self.manager._load_download_account = _load

    def _run(self, download, account, offset=0):
        self._install_account(account)
        return asyncio.run(
            self.manager._download_catchup_stream(download, download.id, self.session, offset=offset)
        )

    # --- auto: probe, fall back, remember ---

    def test_refused_path_form_falls_back_to_query_and_is_remembered(self):
        def behaviour(url):
            if url == PATH_URL:
                raise ProviderStatusError(404, "Not Found")
            return 2048

        self._install_downloader(behaviour)
        account = _account()
        download = _download()

        self.assertEqual(self._run(download, account), 2048)
        self.assertEqual(self.requested, [PATH_URL, QUERY_URL])
        self.assertEqual(account.catchup_url_style_resolved, "query")
        self.assertEqual(download.source_url, QUERY_URL)

    def test_fallback_fires_at_most_once(self):
        def behaviour(url):
            raise ProviderStatusError(404, "Not Found")

        self._install_downloader(behaviour)
        with self.assertRaises(ProviderStatusError):
            self._run(_download(), _account())
        self.assertEqual(self.requested, [PATH_URL, QUERY_URL])

    def test_both_forms_failing_surfaces_the_original_provider_error(self):
        def behaviour(url):
            if url == PATH_URL:
                raise ProviderStatusError(403, "Forbidden")
            raise ProviderStatusError(500, "Internal Server Error")

        self._install_downloader(behaviour)
        with self.assertRaises(ProviderStatusError) as caught:
            self._run(_download(), _account())
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(str(caught.exception), "HTTP 403: Forbidden")

    def test_successful_path_form_resolves_the_account_to_path(self):
        self._install_downloader(lambda url: 4096)
        account = _account()
        self.assertEqual(self._run(_download(), account), 4096)
        self.assertEqual(self.requested, [PATH_URL])
        self.assertEqual(account.catchup_url_style_resolved, "path")

    def test_resolved_query_style_is_used_without_probing_the_path_form(self):
        self._install_downloader(lambda url: 1024)
        account = _account(resolved="query")
        download = _download(url=QUERY_URL)
        self.assertEqual(self._run(download, account), 1024)
        self.assertEqual(self.requested, [QUERY_URL])
        self.assertEqual(account.catchup_url_style_resolved, "query")

    # --- forced settings never probe ---

    def test_forced_query_never_requests_the_path_form(self):
        def behaviour(url):
            if url == PATH_URL:
                raise AssertionError("path form must never be requested")
            raise ProviderStatusError(404, "Not Found")

        self._install_downloader(behaviour)
        with self.assertRaises(ProviderStatusError):
            self._run(_download(url=QUERY_URL), _account(style="query"))
        self.assertEqual(self.requested, [QUERY_URL])

    def test_forced_path_does_not_fall_back_or_resolve(self):
        def behaviour(url):
            raise ProviderStatusError(404, "Not Found")

        self._install_downloader(behaviour)
        account = _account(style="path")
        with self.assertRaises(ProviderStatusError):
            self._run(_download(), account)
        self.assertEqual(self.requested, [PATH_URL])
        self.assertIsNone(account.catchup_url_style_resolved)

    # --- non-catchup downloads are untouched ---

    def test_vod_url_is_not_restyled_and_needs_no_account(self):
        vod_url = "http://provider.test:8080/movie/user/pass/42.mp4"
        self._install_downloader(lambda url: 99)

        async def _load(session, account_id):
            raise AssertionError("VOD downloads must not load the account")

        self.manager._load_download_account = _load
        download = _download(url=vod_url)
        result = asyncio.run(
            self.manager._download_catchup_stream(download, 1, self.session, offset=0)
        )
        self.assertEqual(result, 99)
        self.assertEqual(self.requested, [vod_url])

    def test_non_status_errors_are_not_retried(self):
        def behaviour(url):
            raise OSError("disk is on fire")

        self._install_downloader(behaviour)
        with self.assertRaises(OSError):
            self._run(_download(), _account())
        self.assertEqual(self.requested, [PATH_URL])

    def test_missing_account_does_not_fall_back(self):
        def behaviour(url):
            raise ProviderStatusError(404, "Not Found")

        self._install_downloader(behaviour)
        with self.assertRaises(ProviderStatusError):
            self._run(_download(), None)
        self.assertEqual(self.requested, [PATH_URL])

    def test_a_resumed_transfer_does_not_probe(self):
        """A non-zero offset means this style already streamed bytes, so a
        refusal is the provider's problem and restyling would bin the partial."""

        def behaviour(url):
            raise ProviderStatusError(404, "Not Found")

        self._install_downloader(behaviour)
        account = _account()
        with self.assertRaises(ProviderStatusError):
            self._run(_download(), account, offset=4096)
        self.assertEqual(self.requested, [PATH_URL])
        self.assertIsNone(account.catchup_url_style_resolved)

    def test_fallback_restarts_from_byte_zero(self):
        offsets = []

        async def _download_file(url, output_path, download_id, session, offset=0):
            offsets.append(offset)
            if url == PATH_URL:
                raise ProviderStatusError(404, "Not Found")
            return 10

        self.manager._download_file = _download_file
        self._install_account(_account())
        asyncio.run(
            self.manager._download_catchup_stream(_download(), 1, self.session, offset=0)
        )
        self.assertEqual(offsets, [0, 0])

    def test_a_disk_error_on_the_retry_is_not_reported_as_a_provider_error(self):
        def behaviour(url):
            if url == PATH_URL:
                raise ProviderStatusError(404, "Not Found")
            raise OSError("No space left on device")

        self._install_downloader(behaviour)
        with self.assertRaises(OSError):
            self._run(_download(), _account())


class ProviderStatusErrorTests(unittest.TestCase):
    def test_message_is_unchanged_from_the_plain_exception(self):
        self.assertEqual(str(ProviderStatusError(404, "Not Found")), "HTTP 404: Not Found")


if __name__ == "__main__":
    unittest.main()
