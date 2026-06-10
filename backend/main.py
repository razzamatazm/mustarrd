import asyncio
import hmac
import logging
import json
from base64 import b64decode, b64encode
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.middleware.sessions import SessionMiddleware
from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection
from itsdangerous import BadSignature
from fastapi.responses import FileResponse
from sqlalchemy import select
from starlette.responses import JSONResponse
import os

from config import settings
from database import async_session_maker, init_db
from api import (
    accounts,
    auth as auth_api,
    channels,
    downloads,
    settings as settings_api,
    schedules,
    vod,
    epg,
    logs,
    onboarding,
    admin_users,
    admin_plex,
)
from models import AppSettings
from security import CSRF_HEADER_NAME, CSRF_SESSION_KEY, origin_is_allowed
from services.server_log_bridge import start_server_log_bridge, stop_server_log_bridge


def _configure_logging() -> None:
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(
            level=logging.DEBUG if settings.debug else logging.INFO,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )

    # Keep framework and dependency chatter out of the main app logs.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("multipart").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


_configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()
    logging.getLogger("uvicorn.access").disabled = True
    start_server_log_bridge(asyncio.get_running_loop())

    # Initialize background tasks
    from services.download_manager import download_manager
    from services.scheduled_manager import scheduled_manager
    from services.epg_ingest_manager import epg_ingest_manager
    await download_manager.recover_incomplete_downloads()
    download_task = asyncio.create_task(download_manager.process_queue())
    post_process_task = asyncio.create_task(download_manager.process_post_queue())
    schedule_task = asyncio.create_task(scheduled_manager.process_queue())
    epg_task = asyncio.create_task(epg_ingest_manager.process_queue())

    yield

    # Shutdown
    from services.hls_streamer import hls_streamer
    await hls_streamer.shutdown()
    stop_server_log_bridge()
    download_task.cancel()
    post_process_task.cancel()
    schedule_task.cancel()
    epg_task.cancel()
    await asyncio.gather(download_task, post_process_task, schedule_task, epg_task, return_exceptions=True)


app = FastAPI(
    title=settings.app_name,
    description="Catchup DVR - Xtream Codes Timeshift Downloader",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    openapi_url="/openapi.json" if settings.debug else None,
)

SETUP_ALLOWED_PATHS = {
    "/api/auth/status",
    "/api/auth/csrf",
    "/api/auth/setup",
    "/api/health",
}
UNSAFE_HTTP_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


async def _is_admin_password_configured() -> bool:
    try:
        async with async_session_maker() as session:
            result = await session.execute(select(AppSettings.admin_password_hash).limit(1))
            password_hash = result.scalar_one_or_none()
            return bool(password_hash)
    except Exception:
        return False


class SetupLockdownMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        path = scope.get("path", "")
        scope_type = scope.get("type")
        if path.startswith("/api") and path not in SETUP_ALLOWED_PATHS and not await _is_admin_password_configured():
            if scope_type == "http" and scope.get("method") != "OPTIONS":
                response = JSONResponse(
                    status_code=423,
                    content={"detail": "Initial admin setup is required"},
                )
                await response(scope, receive, send)
                return
            if scope_type == "websocket":
                await send(
                    {
                        "type": "websocket.close",
                        "code": 1008,
                        "reason": "Initial admin setup is required",
                    }
                )
                return

        await self.app(scope, receive, send)


class AutoSecureSessionMiddleware(SessionMiddleware):
    def __init__(self, *args, https_mode: str = "auto", **kwargs):
        kwargs["https_only"] = False
        super().__init__(*args, **kwargs)
        self.https_mode = (https_mode or "auto").strip().lower()
        if self.https_mode not in {"auto", "always", "never"}:
            raise ValueError(f"Unsupported session cookie secure mode: {self.https_mode}")

    @staticmethod
    def _is_secure_request(scope: Scope) -> bool:
        if scope.get("scheme") == "https":
            return True
        for header_name, header_value in scope.get("headers", []):
            if header_name == b"x-forwarded-proto":
                first_proto = header_value.decode("latin-1").split(",")[0].strip().lower()
                return first_proto == "https"
        return False

    def _security_flags_for_scope(self, scope: Scope) -> str:
        secure_cookie = False
        if self.https_mode == "always":
            secure_cookie = True
        elif self.https_mode == "auto":
            secure_cookie = self._is_secure_request(scope)
        flags = self.security_flags
        if secure_cookie:
            flags = f"{flags}; secure"
        return flags

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        connection = HTTPConnection(scope)
        initial_session_was_empty = True

        if self.session_cookie in connection.cookies:
            data = connection.cookies[self.session_cookie].encode("utf-8")
            try:
                data = self.signer.unsign(data, max_age=self.max_age)
                scope["session"] = json.loads(b64decode(data))
                initial_session_was_empty = False
            except BadSignature:
                scope["session"] = {}
        else:
            scope["session"] = {}

        security_flags = self._security_flags_for_scope(scope)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                if scope["session"]:
                    data = b64encode(json.dumps(scope["session"]).encode("utf-8"))
                    data = self.signer.sign(data)
                    headers = MutableHeaders(scope=message)
                    header_value = "{session_cookie}={data}; path={path}; {max_age}{security_flags}".format(
                        session_cookie=self.session_cookie,
                        data=data.decode("utf-8"),
                        path=self.path,
                        max_age=f"Max-Age={self.max_age}; " if self.max_age else "",
                        security_flags=security_flags,
                    )
                    headers.append("Set-Cookie", header_value)
                elif not initial_session_was_empty:
                    headers = MutableHeaders(scope=message)
                    header_value = "{session_cookie}={data}; path={path}; {expires}{security_flags}".format(
                        session_cookie=self.session_cookie,
                        data="null",
                        path=self.path,
                        expires="expires=Thu, 01 Jan 1970 00:00:00 GMT; ",
                        security_flags=security_flags,
                    )
                    headers.append("Set-Cookie", header_value)
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _resolve_session_cookie_secure_mode() -> str:
    if settings.session_https_only is not None:
        return "always" if settings.session_https_only else "never"
    mode = (settings.session_cookie_secure_mode or "auto").strip().lower()
    if mode in {"auto", "always", "never"}:
        return mode
    return "auto"


class CSRFMiddleware:
    def __init__(self, app: ASGIApp, allowed_origins: list[str] | None = None):
        self.app = app
        self.allowed_origins = [origin for origin in (allowed_origins or []) if origin]

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method") or "").upper()
        path = scope.get("path", "")
        if not path.startswith("/api") or method not in UNSAFE_HTTP_METHODS or path == "/api/auth/csrf":
            await self.app(scope, receive, send)
            return

        connection = HTTPConnection(scope)
        session = scope.get("session") or {}
        expected_token = session.get(CSRF_SESSION_KEY)
        provided_token = connection.headers.get(CSRF_HEADER_NAME)
        if not expected_token or not provided_token or not hmac.compare_digest(
            str(expected_token), str(provided_token)
        ):
            response = JSONResponse(status_code=403, content={"detail": "CSRF validation failed"})
            await response(scope, receive, send)
            return

        origin = connection.headers.get("origin")
        if origin and not origin_is_allowed(origin, scope, self.allowed_origins):
            response = JSONResponse(status_code=403, content={"detail": "Origin validation failed"})
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


ALLOWED_CORS_ORIGINS = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]


# CORS for frontend development
app.add_middleware(SetupLockdownMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(
    CSRFMiddleware,
    allowed_origins=ALLOWED_CORS_ORIGINS,
)
app.add_middleware(
    AutoSecureSessionMiddleware,
    secret_key=settings.session_secret,
    same_site="lax",
    https_mode=_resolve_session_cookie_secure_mode(),
)

# API routes
app.include_router(auth_api.router, prefix="/api/auth", tags=["auth"])
app.include_router(accounts.router, prefix="/api/accounts", tags=["accounts"])
app.include_router(channels.router, prefix="/api", tags=["channels"])
app.include_router(downloads.router, prefix="/api/downloads", tags=["downloads"])
app.include_router(schedules.router, prefix="/api/schedules", tags=["schedules"])
app.include_router(vod.router, prefix="/api/vod", tags=["vod"])
app.include_router(settings_api.router, prefix="/api/settings", tags=["settings"])
app.include_router(epg.router, prefix="/api", tags=["epg"])
app.include_router(logs.router, prefix="/api", tags=["logs"])
app.include_router(onboarding.router, prefix="/api/onboarding", tags=["onboarding"])
app.include_router(admin_users.router, prefix="/api/admin", tags=["admin-users"])
app.include_router(admin_plex.router, prefix="/api/admin", tags=["admin-plex"])


@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "app": settings.app_name}


def _resolve_frontend_dist() -> Path | None:
    configured_path = os.environ.get("CATCHUP_FRONTEND_DIST")
    if configured_path:
        configured_dist = Path(configured_path).expanduser().resolve()
        if (configured_dist / "index.html").exists():
            return configured_dist

    candidate_roots = [
        Path(__file__).resolve().parents[1] / "frontend" / "dist",
        Path(__file__).resolve().parent / "frontend" / "dist",
    ]

    for candidate in candidate_roots:
        if (candidate / "index.html").exists():
            return candidate

    return None


FRONTEND_DIST = _resolve_frontend_dist()


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def frontend_index():
    if FRONTEND_DIST is None:
        raise HTTPException(status_code=404, detail="Frontend is not built")
    return FileResponse(FRONTEND_DIST / "index.html")


@app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def frontend_routes(full_path: str):
    if full_path.startswith("api/") or full_path == "api":
        raise HTTPException(status_code=404, detail="Not found")

    if FRONTEND_DIST is None:
        raise HTTPException(status_code=404, detail="Frontend is not built")

    requested_path = (FRONTEND_DIST / full_path).resolve()

    if requested_path.is_file() and requested_path.is_relative_to(FRONTEND_DIST):
        return FileResponse(requested_path)

    return FileResponse(FRONTEND_DIST / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=4177, reload=True, access_log=False)
