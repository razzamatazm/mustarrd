"""
Regression test: POST /epg/refresh must be admin-only.

Before fix: trigger_epg_refresh used require_admin_or_download_user.
Any authenticated user, including Plex-provisioned download_only accounts,
could trigger a full EPG refresh, hammering the IPTV provider and the host
with repeated XMLTV ingests.

After fix: trigger_epg_refresh uses require_admin.
Download-only sessions receive HTTP 403.
"""
import inspect
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import HTTPException

from auth import require_admin, AuthContext


class EPGRefreshWiringTest(unittest.TestCase):

    def test_trigger_epg_refresh_uses_require_admin(self):
        """trigger_epg_refresh must declare require_admin as its auth dependency."""
        from api.epg import trigger_epg_refresh
        sig = inspect.signature(trigger_epg_refresh)
        admin_param = sig.parameters.get("_admin")
        self.assertIsNotNone(admin_param, "_admin parameter missing from handler")
        dep = admin_param.default
        self.assertEqual(
            dep.dependency,
            require_admin,
            "trigger_epg_refresh must use require_admin, not require_admin_or_download_user",
        )


class EPGRefreshAuthGuardTests(unittest.IsolatedAsyncioTestCase):

    async def test_download_only_user_gets_403(self):
        """require_admin must reject a download_only session with HTTP 403."""
        ctx = AuthContext(authenticated=True, is_admin=False, role="download_only")
        with self.assertRaises(HTTPException) as cm:
            await require_admin(context=ctx)
        self.assertEqual(cm.exception.status_code, 403)

    async def test_unauthenticated_gets_403(self):
        """require_admin must reject an unauthenticated context."""
        ctx = AuthContext(authenticated=False)
        with self.assertRaises(HTTPException) as cm:
            await require_admin(context=ctx)
        self.assertEqual(cm.exception.status_code, 403)

    async def test_admin_passes_guard(self):
        """require_admin must allow an admin session through."""
        ctx = AuthContext(authenticated=True, is_admin=True, role="admin")
        result = await require_admin(context=ctx)
        self.assertIs(result, ctx)


if __name__ == "__main__":
    unittest.main()
