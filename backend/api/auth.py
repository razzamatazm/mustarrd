import hashlib
import ipaddress
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from auth import (
    get_or_create_app_settings,
    get_admin_user,
    hash_password,
    verify_password,
    get_auth_context,
    require_admin,
    require_authenticated,
    clear_auth_session,
    set_admin_user_session,
    set_download_user_session,
    AuthContext,
)
from database import get_session
from config import settings
from models import User, UserIdentity, PlexServer, UserSetupToken
from security import ensure_csrf_token
from services.credential_crypto import credential_crypto
from services.plex_service import plex_service


router = APIRouter()
RATE_LIMIT_WINDOW_SECONDS = 15 * 60
RATE_LIMITS = {
    "setup": 6,
    "login": 20,
    "plex_start": 10,
    "plex_complete": 30,
}
_attempt_log: dict[tuple[str, str], list[float]] = {}


class PasswordPayload(BaseModel):
    password: str = Field(min_length=8, max_length=128)


class SetupPayload(BaseModel):
    password: str = Field(min_length=8, max_length=128)
    username: str | None = Field(default=None, min_length=1, max_length=255)


class CredentialsLoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=128)


class AdminBootstrapCompletePayload(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    legacy_password: str = Field(min_length=1, max_length=128)


class ChangePasswordPayload(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class UserSetupPayload(BaseModel):
    password: str = Field(min_length=8, max_length=128)


class PlexCompletePayload(BaseModel):
    pin_id: int = Field(ge=1)


class PreferencesPayload(BaseModel):
    show_future_programs: bool


def _is_local_or_private_client(host: str | None) -> bool:
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def _forwarded_client_host(request: Request) -> str | None:
    """Return the client address reported by reverse-proxy forwarding headers.

    Returns None when no forwarding headers are present (direct connection).
    Uses the right-most X-Forwarded-For entry: that is the IP the trusted
    proxy (e.g. NPM) appended; left-most entries are client-controlled and
    spoofable. Falls back to X-Real-IP. A header that is present but empty
    returns "" so callers fail closed.
    """
    xff = request.headers.get("X-Forwarded-For")
    if xff is not None:
        for entry in reversed(xff.split(",")):
            entry = entry.strip()
            if entry:
                return entry
        return ""
    real_ip = request.headers.get("X-Real-IP")
    if real_ip is not None:
        return real_ip.strip()
    return None


def _setup_client_allowed(request: Request) -> bool:
    """Gate for the initial admin-setup endpoint.

    Behind a reverse proxy in Docker (e.g. Unraid + Nginx Proxy Manager) the
    socket peer is the proxy container's private IP for every request, so the
    peer alone cannot prove a request is local. When forwarding headers are
    present, the effective client is the forwarded address and it must be
    local/private as well. Forwarding headers are only consulted when the
    direct peer is itself local/private, so a public peer cannot spoof them.
    """
    if settings.allow_remote_setup:
        return True
    peer_host = request.client.host if request.client else None
    if not _is_local_or_private_client(peer_host):
        return False
    forwarded_host = _forwarded_client_host(request)
    if forwarded_host is None:
        return True
    return _is_local_or_private_client(forwarded_host)


def _client_key(request: Request) -> str:
    host = request.client.host if request.client else None
    # Trust X-Forwarded-For only when the direct peer is private (e.g. NPM on Docker
    # bridge). Public peers must not be able to spoof this header to get a fresh bucket.
    if _is_local_or_private_client(host):
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            # Use the right-most entry: that is the IP the trusted proxy (NPM)
            # appended. The left-most entry is client-controlled and spoofable.
            real_ip = xff.split(",")[-1].strip()
            if real_ip:
                return real_ip
    return host or "unknown"


def _enforce_rate_limit(action: str, request: Request):
    now = time.monotonic()
    key = (action, _client_key(request))
    max_attempts = RATE_LIMITS[action]
    attempts = _attempt_log.setdefault(key, [])
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    attempts[:] = [attempt for attempt in attempts if attempt >= cutoff]
    if len(attempts) >= max_attempts:
        raise HTTPException(status_code=429, detail="Too many authentication attempts")
    attempts.append(now)


def _hash_setup_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _normalize_username(raw_username: str | None) -> str:
    username = (raw_username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")
    return username


async def _resolve_future_visibility(context: AuthContext, session: AsyncSession) -> bool:
    app_settings = await get_or_create_app_settings(session)
    app_default = bool(app_settings.show_future_programs)
    if context.is_admin:
        return app_default
    if context.user and context.user.show_future_programs is not None:
        return bool(context.user.show_future_programs)
    return app_default


async def _bootstrap_required(session: AsyncSession) -> bool:
    app_settings = await get_or_create_app_settings(session)
    admin_user = await get_admin_user(session)
    required = bool(app_settings.admin_password_hash) and admin_user is None
    if app_settings.admin_username_bootstrap_required != required:
        app_settings.admin_username_bootstrap_required = required
        await session.commit()
    return required


@router.get("/status")
async def auth_status(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    app_settings = await get_or_create_app_settings(session)
    password_set = bool(app_settings.admin_password_hash)
    context = await get_auth_context(request, session)
    bootstrap_required = await _bootstrap_required(session)

    profile = None
    if context.user:
        profile = {
            "id": context.user.id,
            "username": context.user.username,
            "display_name": context.user.display_name,
            "role": context.user.role,
        }

    plex_result = await session.execute(select(PlexServer).limit(1))
    plex_server = plex_result.scalar_one_or_none()
    plex_login_available = bool(
        plex_server
        and plex_server.enabled
        and plex_server.auto_allow_all_server_users
        and (plex_server.connection_uri or plex_server.base_url)
        and (plex_server.access_token_encrypted or plex_server.token_encrypted)
    )

    return {
        "authenticated": context.authenticated,
        "password_set": password_set,
        "bootstrap_required": bootstrap_required,
        "role": context.role,
        "provider": context.provider,
        "is_admin": context.is_admin,
        "user": profile,
        "plex_login_available": plex_login_available,
        "password_reset_required": bool(context.user.password_reset_required) if context.user else False,
        "show_future_programs": await _resolve_future_visibility(context, session) if context.authenticated else bool(app_settings.show_future_programs),
    }


@router.get("/csrf")
async def get_csrf_token(request: Request):
    return {"csrf_token": ensure_csrf_token(request)}


@router.get("/admin-bootstrap/status")
async def admin_bootstrap_status(session: AsyncSession = Depends(get_session)):
    app_settings = await get_or_create_app_settings(session)
    return {
        "required": await _bootstrap_required(session),
        "legacy_password_set": bool(app_settings.admin_password_hash),
    }


@router.post("/admin-bootstrap/complete")
async def admin_bootstrap_complete(
    payload: AdminBootstrapCompletePayload,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    _enforce_rate_limit("login", request)
    required = await _bootstrap_required(session)
    if not required:
        raise HTTPException(status_code=400, detail="Admin bootstrap is not required")

    app_settings = await get_or_create_app_settings(session)
    if not app_settings.admin_password_hash:
        raise HTTPException(status_code=400, detail="Legacy admin password is not configured")
    if not verify_password(payload.legacy_password, app_settings.admin_password_hash):
        raise HTTPException(status_code=401, detail="Authentication failed")

    username = _normalize_username(payload.username)
    existing = await session.execute(
        select(User).where(func.lower(User.username) == username.lower())
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Username already exists")

    admin_user = User(
        role="admin",
        username=username,
        display_name="Administrator",
        status="active",
        password_hash=app_settings.admin_password_hash,
        last_login_at=datetime.utcnow(),
    )
    session.add(admin_user)
    app_settings.admin_username_bootstrap_required = False
    await session.commit()
    await session.refresh(admin_user)

    clear_auth_session(request)
    set_admin_user_session(request, admin_user.id)

    return {
        "status": "bootstrap_completed",
        "authenticated": True,
        "role": "admin",
        "user": {"id": admin_user.id, "username": admin_user.username},
    }


@router.post("/setup")
async def setup_auth(
    payload: SetupPayload,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    _enforce_rate_limit("setup", request)
    app_settings = await get_or_create_app_settings(session)

    if app_settings.admin_password_hash:
        raise HTTPException(status_code=400, detail="Admin password is already configured")
    if not _setup_client_allowed(request):
        raise HTTPException(
            status_code=403,
            detail="Initial setup is restricted to local/private network clients",
        )

    hashed = hash_password(payload.password)
    desired_username = _normalize_username(payload.username or "admin")
    app_settings.admin_password_hash = hashed

    existing_admin = await get_admin_user(session)
    if not existing_admin:
        existing_username = await session.execute(
            select(User).where(func.lower(User.username) == desired_username.lower())
        )
        if existing_username.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Admin username is unavailable")
        admin_user = User(
            role="admin",
            username=desired_username,
            display_name="Administrator",
            status="active",
            password_hash=hashed,
            last_login_at=datetime.utcnow(),
        )
        session.add(admin_user)
        app_settings.admin_username_bootstrap_required = False
        await session.commit()
        await session.refresh(admin_user)
        clear_auth_session(request)
        set_admin_user_session(request, admin_user.id)
    else:
        existing_admin.password_hash = hashed
        existing_admin.updated_at = datetime.utcnow()
        existing_admin.last_login_at = datetime.utcnow()
        await session.commit()
        clear_auth_session(request)
        set_admin_user_session(request, existing_admin.id)

    return {
        "status": "configured",
        "authenticated": True,
        "password_set": True,
        "role": "admin",
    }


@router.post("/login-credentials")
async def login_credentials(
    payload: CredentialsLoginPayload,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    _enforce_rate_limit("login", request)

    if await _bootstrap_required(session):
        raise HTTPException(status_code=423, detail="Admin username setup is required before login")

    username = _normalize_username(payload.username)
    result = await session.execute(
        select(User).where(func.lower(User.username) == username.lower())
    )
    user = result.scalar_one_or_none()
    if not user or user.status != "active" or not user.password_hash:
        raise HTTPException(status_code=401, detail="Authentication failed")
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Authentication failed")

    user.last_login_at = datetime.utcnow()
    user.updated_at = datetime.utcnow()
    await session.commit()

    clear_auth_session(request)
    if user.role == "admin":
        set_admin_user_session(request, user.id)
        provider = "admin_local"
    else:
        set_download_user_session(request, user.id, "local")
        provider = "local"

    return {
        "status": "authenticated",
        "authenticated": True,
        "role": user.role,
        "provider": provider,
        "user": {"id": user.id, "username": user.username, "display_name": user.display_name},
        "password_reset_required": bool(user.password_reset_required) if provider == "local" else False,
    }


@router.post("/login")
async def login_auth_legacy(
    payload: PasswordPayload,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    # Compatibility wrapper: finds the active admin by role, not by name.
    admin = await get_admin_user(session)
    if not admin:
        raise HTTPException(status_code=401, detail="Authentication failed")
    return await login_credentials(
        CredentialsLoginPayload(username=admin.username, password=payload.password),
        request,
        session,
    )


@router.post("/user/login")
async def login_download_user_legacy(
    payload: CredentialsLoginPayload,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    return await login_credentials(payload, request, session)


@router.post("/plex/login/start")
async def plex_login_start(request: Request, session: AsyncSession = Depends(get_session)):
    _enforce_rate_limit("plex_start", request)
    plex_result = await session.execute(select(PlexServer).limit(1))
    plex_server = plex_result.scalar_one_or_none()
    if not plex_server or not plex_server.enabled or not plex_server.auto_allow_all_server_users or not (plex_server.connection_uri or plex_server.base_url):
        raise HTTPException(status_code=403, detail="Plex login is not configured")
    if not (plex_server.access_token_encrypted or plex_server.token_encrypted):
        raise HTTPException(status_code=403, detail="Plex login is not configured")

    pin = await plex_service.create_pin()
    pin["poll_interval_seconds"] = 2
    pin["expires_in"] = pin.get("expires_in") or 300
    return pin


@router.post("/plex/login/complete")
async def plex_login_complete(
    payload: PlexCompletePayload,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    _enforce_rate_limit("plex_complete", request)

    pin_data = await plex_service.check_pin(payload.pin_id)
    auth_token = pin_data.get("authToken")
    if not auth_token:
        raise HTTPException(status_code=400, detail="Plex authentication is not complete yet")

    profile = await plex_service.get_user_profile(auth_token)
    provider_subject = str(profile.get("id") or profile.get("uuid") or "").strip()
    if not provider_subject:
        raise HTTPException(status_code=400, detail="Unable to resolve Plex user identity")

    plex_result = await session.execute(select(PlexServer).limit(1))
    plex_server = plex_result.scalar_one_or_none()
    if not plex_server or not plex_server.enabled:
        raise HTTPException(status_code=403, detail="Plex login is not configured")

    encrypted_token = plex_server.access_token_encrypted or plex_server.token_encrypted
    try:
        server_token = credential_crypto.decrypt(encrypted_token)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Stored Plex server token is invalid") from exc

    if plex_server.auto_allow_all_server_users:
        server_uri = plex_server.connection_uri or plex_server.base_url
        is_member = await plex_service.user_is_in_server(server_uri, server_token, profile)
        if not is_member:
            raise HTTPException(status_code=403, detail="Your Plex account is not on the configured server")
    else:
        raise HTTPException(status_code=403, detail="Plex login policy denies automatic access")

    identity_result = await session.execute(
        select(UserIdentity).where(
            UserIdentity.provider == "plex",
            UserIdentity.provider_subject == provider_subject,
        )
    )
    identity = identity_result.scalar_one_or_none()

    username = profile.get("username") or profile.get("email") or f"plex-{provider_subject}"
    display_name = profile.get("friendlyName") or profile.get("title") or username

    if identity:
        user_result = await session.execute(select(User).where(User.id == identity.user_id))
        user = user_result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=500, detail="Plex identity is mapped to a missing user")
        if user.status == "disabled":
            raise HTTPException(status_code=403, detail="Your account has been disabled")
    else:
        user = User(
            role="download_only",
            username=None,
            password_hash=None,
            display_name=display_name,
            status="active",
        )
        session.add(user)
        await session.flush()

        identity = UserIdentity(
            user_id=user.id,
            provider="plex",
            provider_subject=provider_subject,
            provider_username=str(username),
            provider_email=profile.get("email"),
        )
        session.add(identity)

    if user.status != "active":
        user.status = "active"
    user.last_login_at = datetime.utcnow()
    user.updated_at = datetime.utcnow()
    identity.provider_username = str(username)
    identity.provider_email = profile.get("email")
    identity.updated_at = datetime.utcnow()
    await session.commit()

    clear_auth_session(request)
    set_download_user_session(request, user.id, "plex")

    return {
        "status": "authenticated",
        "authenticated": True,
        "role": "download_only",
        "provider": "plex",
        "user": {"id": user.id, "display_name": user.display_name},
    }


@router.post("/logout")
async def logout_auth(request: Request):
    clear_auth_session(request)
    request.session.pop("plex_admin_token", None)
    request.session.pop("plex_admin_profile", None)
    return {"status": "logged_out"}


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordPayload,
    context: AuthContext = Depends(require_authenticated),
    session: AsyncSession = Depends(get_session),
):
    if not context.user or not context.user_id:
        raise HTTPException(status_code=400, detail="User is not configured")
    if int(context.user.id) != int(context.user_id):
        raise HTTPException(status_code=403, detail="You can only change your own password")
    if not context.user.password_hash:
        if context.is_admin:
            raise HTTPException(status_code=400, detail="Admin user is not configured")
        raise HTTPException(status_code=400, detail="Local password is not configured")
    if not context.is_admin and context.provider != "local":
        raise HTTPException(
            status_code=400,
            detail="Password changes are only available for local download users",
        )

    if not verify_password(payload.current_password, context.user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=400, detail="New password must be different")

    hashed = hash_password(payload.new_password)
    context.user.password_hash = hashed
    context.user.password_reset_required = False
    context.user.updated_at = datetime.utcnow()

    if context.is_admin:
        app_settings = await get_or_create_app_settings(session)
        app_settings.admin_password_hash = hashed
    await session.commit()

    return {
        "status": "password_updated",
        "role": "admin" if context.is_admin else "download_only",
    }


@router.get("/preferences")
async def get_preferences(
    context: AuthContext = Depends(require_authenticated),
    session: AsyncSession = Depends(get_session),
):
    return {
        "show_future_programs": await _resolve_future_visibility(context, session),
    }


@router.put("/preferences")
async def update_preferences(
    payload: PreferencesPayload,
    context: AuthContext = Depends(require_authenticated),
    session: AsyncSession = Depends(get_session),
):
    if context.is_admin:
        app_settings = await get_or_create_app_settings(session)
        app_settings.show_future_programs = bool(payload.show_future_programs)
        await session.commit()
    else:
        result = await session.execute(select(User).where(User.id == context.user_id))
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        user.show_future_programs = bool(payload.show_future_programs)
        user.updated_at = datetime.utcnow()
        await session.commit()

    return {
        "show_future_programs": bool(payload.show_future_programs),
    }


@router.get("/user/setup/{token}")
async def validate_user_setup_token(
    token: str,
    session: AsyncSession = Depends(get_session),
):
    token_hash = _hash_setup_token(token)
    result = await session.execute(select(UserSetupToken).where(UserSetupToken.token_hash == token_hash))
    row = result.scalar_one_or_none()
    if not row or row.used_at is not None or row.expires_at < datetime.utcnow():
        raise HTTPException(status_code=404, detail="Setup token is invalid or expired")

    user_result = await session.execute(select(User).where(User.id == row.user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "valid": True,
        "username": user.username,
        "display_name": user.display_name,
        "expires_at": row.expires_at.isoformat(),
    }


@router.post("/user/setup/{token}")
async def complete_user_setup(
    token: str,
    payload: UserSetupPayload,
    session: AsyncSession = Depends(get_session),
):
    token_hash = _hash_setup_token(token)
    result = await session.execute(select(UserSetupToken).where(UserSetupToken.token_hash == token_hash))
    row = result.scalar_one_or_none()
    if not row or row.used_at is not None or row.expires_at < datetime.utcnow():
        raise HTTPException(status_code=404, detail="Setup token is invalid or expired")

    user_result = await session.execute(select(User).where(User.id == row.user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.status == "disabled":
        raise HTTPException(status_code=403, detail="Account is disabled")

    user.password_hash = hash_password(payload.password)
    user.password_reset_required = False
    if user.status != "active":
        user.status = "active"
    user.updated_at = datetime.utcnow()
    row.used_at = datetime.utcnow()
    await session.commit()

    return {"status": "configured"}
