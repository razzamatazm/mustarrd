"""
Regression test: plex_login_start must be rate-limited.

Before fix: POST /api/auth/plex/login/start had no call to _enforce_rate_limit.
An unauthenticated caller could loop the endpoint without bound, creating Plex.tv
PINs until Plex.tv rate-limits the shared client identifier, breaking legitimate
Plex logins for all users.

After fix:
- RATE_LIMITS["plex_start"] = 10 is defined.
- plex_login_start calls _enforce_rate_limit("plex_start", request) before any DB work.
- The 11th request from the same client within the window receives HTTP 429.
- Requests below the limit still pass through to Plex.tv.
"""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import api.auth as auth_module
from api.auth import RATE_LIMITS, plex_login_start
from database import Base
from models import PlexServer


def _mock_request(host="127.0.0.1"):
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = host
    req.session = {}
    return req


class PlexStartRateLimitTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        auth_module._attempt_log.clear()

    async def asyncTearDown(self):
        auth_module._attempt_log.clear()
        await self.engine.dispose()

    async def _seed_plex_server(self):
        async with self.session_factory() as session:
            server = PlexServer(
                base_url="http://plex.local",
                token_encrypted="enc-tok",
                access_token_encrypted="enc-tok",
                auto_allow_all_server_users=True,
            )
            session.add(server)
            await session.commit()

    async def test_rate_limit_key_defined(self):
        """plex_start must have an entry in RATE_LIMITS."""
        self.assertIn("plex_start", RATE_LIMITS)
        self.assertGreater(RATE_LIMITS["plex_start"], 0)

    async def test_eleventh_request_returns_429(self):
        """11th call from the same client within the window must get HTTP 429."""
        limit = RATE_LIMITS["plex_start"]
        key = ("plex_start", "10.0.0.1")
        now = time.monotonic()
        # Pre-fill the log to the limit (all within the rate window)
        auth_module._attempt_log[key] = [now - 1] * limit

        async with self.session_factory() as session:
            with self.assertRaises(HTTPException) as ctx:
                await plex_login_start(_mock_request("10.0.0.1"), session)

        self.assertEqual(ctx.exception.status_code, 429)

    async def test_under_limit_passes_through(self):
        """Requests below the limit must reach plex_service.create_pin."""
        await self._seed_plex_server()
        fake_pin = {"id": 99, "code": "ABCD", "expires_in": 300}
        with patch("api.auth.plex_service.create_pin", new=AsyncMock(return_value=fake_pin)):
            async with self.session_factory() as session:
                result = await plex_login_start(_mock_request("10.0.0.2"), session)

        self.assertEqual(result["id"], 99)

    async def test_different_clients_have_independent_buckets(self):
        """Exhausting one client's bucket must not block a different client."""
        limit = RATE_LIMITS["plex_start"]
        now = time.monotonic()
        # Fill up client A's bucket
        auth_module._attempt_log[("plex_start", "10.0.0.3")] = [now - 1] * limit

        # Client B (10.0.0.4) should still get through
        await self._seed_plex_server()
        fake_pin = {"id": 55, "code": "WXYZ", "expires_in": 300}
        with patch("api.auth.plex_service.create_pin", new=AsyncMock(return_value=fake_pin)):
            async with self.session_factory() as session:
                result = await plex_login_start(_mock_request("10.0.0.4"), session)

        self.assertEqual(result["id"], 55)

        # Client A still blocked
        async with self.session_factory() as session:
            with self.assertRaises(HTTPException) as ctx:
                await plex_login_start(_mock_request("10.0.0.3"), session)
        self.assertEqual(ctx.exception.status_code, 429)


if __name__ == "__main__":
    unittest.main()
