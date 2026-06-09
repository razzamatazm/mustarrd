import base64
import hashlib
import hmac
import os
from dataclasses import dataclass

from fastapi import Depends, HTTPException, WebSocket
from starlette.requests import HTTPConnection
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_session
from models import AppSettings, User


SESSION_ADMIN_KEY = "is_admin"
SESSION_AUTH_ROLE_KEY = "auth_role"
SESSION_USER_ID_KEY = "auth_user_id"
SESSION_AUTH_PROVIDER_KEY = "auth_provider"


@dataclass
class AuthContext:
    authenticated: bool
    role: str | None = None
    provider: str | None = None
    user_id: int | None = None
    is_admin: bool = False
    user: User | None = None


def clear_auth_session(connection: HTTPConnection) -> None:
    connection.session.pop(SESSION_ADMIN_KEY, None)
    connection.session.pop(SESSION_AUTH_ROLE_KEY, None)
    connection.session.pop(SESSION_USER_ID_KEY, None)
    connection.session.pop(SESSION_AUTH_PROVIDER_KEY, None)


def set_admin_session(connection: HTTPConnection) -> None:
    connection.session[SESSION_ADMIN_KEY] = True
    connection.session[SESSION_AUTH_ROLE_KEY] = "admin"
    connection.session[SESSION_AUTH_PROVIDER_KEY] = "admin_local"
    connection.session.pop(SESSION_USER_ID_KEY, None)


def set_admin_user_session(connection: HTTPConnection, user_id: int) -> None:
    connection.session[SESSION_ADMIN_KEY] = True
    connection.session[SESSION_AUTH_ROLE_KEY] = "admin"
    connection.session[SESSION_AUTH_PROVIDER_KEY] = "admin_local"
    connection.session[SESSION_USER_ID_KEY] = int(user_id)


def set_download_user_session(connection: HTTPConnection, user_id: int, provider: str) -> None:
    connection.session[SESSION_ADMIN_KEY] = False
    connection.session[SESSION_AUTH_ROLE_KEY] = "download_only"
    connection.session[SESSION_USER_ID_KEY] = int(user_id)
    connection.session[SESSION_AUTH_PROVIDER_KEY] = provider


async def get_or_create_app_settings(session: AsyncSession) -> AppSettings:
    result = await session.execute(select(AppSettings))
    app_settings = result.scalar_one_or_none()
    if app_settings is None:
        app_settings = AppSettings()
        session.add(app_settings)
        await session.commit()
        await session.refresh(app_settings)
    return app_settings


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
    digest_b64 = base64.urlsafe_b64encode(digest).decode("ascii")
    return f"scrypt$16384$8$1${salt_b64}${digest_b64}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, n_value, r_value, p_value, salt_b64, digest_b64 = password_hash.split("$", 5)
    except ValueError:
        return False

    if algorithm != "scrypt":
        return False

    try:
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected_digest = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
        n_value_int = int(n_value)
        r_value_int = int(r_value)
        p_value_int = int(p_value)
    except Exception:
        return False

    actual_digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n_value_int,
        r=r_value_int,
        p=p_value_int,
    )
    return hmac.compare_digest(actual_digest, expected_digest)


async def get_auth_context(
    request: HTTPConnection,
    session: AsyncSession = Depends(get_session),
) -> AuthContext:
    role = request.session.get(SESSION_AUTH_ROLE_KEY)
    user_id = request.session.get(SESSION_USER_ID_KEY)
    provider = request.session.get(SESSION_AUTH_PROVIDER_KEY)

    if role == "admin" and user_id:
        result = await session.execute(
            select(User).where(User.id == int(user_id), User.role == "admin")
        )
        user = result.scalar_one_or_none()
        if user and user.status == "active":
            return AuthContext(
                authenticated=True,
                role="admin",
                provider=provider or "admin_local",
                user_id=user.id,
                is_admin=True,
                user=user,
            )

    if role == "download_only" and user_id:
        result = await session.execute(
            select(User).where(User.id == int(user_id), User.role == "download_only")
        )
        user = result.scalar_one_or_none()
        if user and user.status == "active":
            return AuthContext(
                authenticated=True,
                role="download_only",
                provider=provider or "local",
                user_id=user.id,
                is_admin=False,
                user=user,
            )

    return AuthContext(authenticated=False)


async def get_admin_user(session: AsyncSession) -> User | None:
    result = await session.execute(
        select(User).where(User.role == "admin", User.status == "active").limit(1)
    )
    return result.scalar_one_or_none()


async def require_authenticated(
    context: AuthContext = Depends(get_auth_context),
) -> AuthContext:
    if context.authenticated:
        return context
    raise HTTPException(status_code=401, detail="Authentication required")


async def require_admin(
    context: AuthContext = Depends(get_auth_context),
) -> AuthContext:
    if context.authenticated and context.is_admin:
        return context
    raise HTTPException(status_code=403, detail="Admin access required")


async def require_admin_or_download_user(
    context: AuthContext = Depends(require_authenticated),
) -> AuthContext:
    return context


async def require_authenticated_websocket(
    websocket: WebSocket,
    session: AsyncSession = Depends(get_session),
) -> AuthContext:
    context = await get_auth_context(websocket, session)
    if context.authenticated:
        return context
    raise HTTPException(status_code=401, detail="Authentication required")


async def require_admin_websocket(
    websocket: WebSocket,
    session: AsyncSession = Depends(get_session),
) -> AuthContext:
    context = await get_auth_context(websocket, session)
    if context.authenticated and context.is_admin:
        return context
    raise HTTPException(status_code=403, detail="Admin access required")


async def require_admin_or_download_user_websocket(
    websocket: WebSocket,
    session: AsyncSession = Depends(get_session),
) -> AuthContext:
    context = await get_auth_context(websocket, session)
    if context.authenticated:
        return context
    raise HTTPException(status_code=401, detail="Authentication required")
