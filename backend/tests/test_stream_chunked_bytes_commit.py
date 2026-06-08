"""
Regression: _stream_response_to_file must commit downloaded_bytes to the DB after
the streaming loop when total_size == 0 (chunked / unknown-length stream).

This commit is what makes recover_incomplete_downloads able to detect a complete
chunked file. If it is removed, downloaded_bytes stays 0 in the DB at crash time,
recovery cannot distinguish a complete chunked file from a partial one, and it
re-queues the finished recording as PENDING (which risks HTTP 416 data loss on the
next resume attempt).

The tests in test_recover_chunked_complete_range416.py fabricate downloaded_bytes=8192
to represent the post-commit DB state, so they pass even if this commit were deleted.
This test targets the commit directly: it calls _stream_response_to_file and asserts
that session.execute receives an update setting downloaded_bytes to the written count.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.download_manager import DownloadManager


class StreamChunkedByteCommitTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_stream_commits_final_byte_count(self):
        """
        For total_size == 0 the in-loop progress commit never fires (progress stays 0
        and downloaded == total_size is only true before any bytes land). The post-loop
        block must execute exactly once and set downloaded_bytes to the written count.

        Removing the post-loop commit block would drop execute calls to 0 and fail
        this test, proving the fix is load-bearing.
        """
        body = b"\x47" * 512

        async def fake_iter_chunked(chunk_size):
            yield body

        mock_content = MagicMock()
        mock_content.iter_chunked = fake_iter_chunked

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.content = mock_content

        executed_stmts = []

        async def capture_execute(stmt):
            executed_stmts.append(stmt)
            return MagicMock()

        mock_session = AsyncMock()
        mock_session.execute = capture_execute
        mock_session.commit = AsyncMock()

        manager = DownloadManager()

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            tmp_path = f.name
        try:
            with (
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
            ):
                result = await manager._stream_response_to_file(
                    response=mock_response,
                    output_path=tmp_path,
                    file_mode="wb",
                    downloaded_start=0,
                    total_size=0,
                    download_id=99,
                    session=mock_session,
                )
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

        self.assertEqual(result, 512, "Must return total bytes written.")

        self.assertEqual(
            len(executed_stmts),
            1,
            "Exactly one execute expected: the post-loop downloaded_bytes commit. "
            "Zero means the commit block was removed.",
        )
        self.assertEqual(
            mock_session.commit.call_count,
            1,
            "Exactly one commit expected after the loop.",
        )

        stmt = executed_stmts[0]
        compiled = stmt.compile(compile_kwargs={"literal_binds": True})
        sql = str(compiled)
        self.assertIn(
            "downloaded_bytes",
            sql,
            "Post-loop execute must update downloaded_bytes.",
        )
        self.assertIn(
            "512",
            sql,
            "Post-loop execute must set downloaded_bytes to 512 (actual written bytes).",
        )


if __name__ == "__main__":
    unittest.main()
