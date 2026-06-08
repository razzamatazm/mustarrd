import hashlib
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_admin, hash_password, AuthContext
from database import get_session
from models import (
    Download, DownloadStatus,
    ScheduledRecording, ScheduledStatus,
    User, UserIdentity, UserSetupToken,
)
from services.download_manager import download_manager


router = APIRouter()


class CreateUserPayload(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    display_name: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    status: str = Field(default="active")


class UpdateUserPayload(BaseModel):
    display_name: str | None = Field(default=None, max_length=255)
    status: str | None = Field(default=None)
    password: str | None = Field(default=None, min_length=8, max_length=128)


def _hash_setup_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _normalize_username(raw_username: str | None) -> str:
    username = (raw_username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")
    return username


async def _create_setup_token(session: AsyncSession, user_id: int, ttl_hours: int = 24) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = _hash_setup_token(token)
    await session.execute(UserSetupToken.__table__.delete().where(UserSetupToken.user_id == user_id))
    row = UserSetupToken(
        user_id=user_id,
        token_hash=token_hash,
        expires_at=datetime.utcnow() + timedelta(hours=ttl_hours),
    )
    session.add(row)
    await session.commit()
    return token


@router.get("/users")
async def list_users(
    _admin: AuthContext = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(User).where(User.role == "download_only").order_by(User.created_at.desc())
    )
    users = result.scalars().all()
    return [u.to_dict() for u in users]


@router.post("/users")
async def create_user(
    payload: CreateUserPayload,
    _admin: AuthContext = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    normalized_username = _normalize_username(payload.username)
    existing_result = await session.execute(
        select(User).where(func.lower(User.username) == normalized_username.lower())
    )
    existing = existing_result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Username already exists")

    user = User(
        role="download_only",
        username=normalized_username,
        display_name=payload.display_name,
        status=payload.status if payload.status in {"active", "disabled"} else "active",
        password_hash=hash_password(payload.password) if payload.password else None,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user.to_dict()


@router.patch("/users/{user_id}")
async def update_user(
    user_id: int,
    payload: UpdateUserPayload,
    _admin: AuthContext = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(User).where(User.id == user_id, User.role == "download_only"))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if payload.display_name is not None:
        user.display_name = payload.display_name
    if payload.status is not None:
        if payload.status not in {"active", "disabled"}:
            raise HTTPException(status_code=400, detail="Invalid status")
        user.status = payload.status
    if payload.password is not None:
        if not user.username:
            raise HTTPException(status_code=400, detail="Cannot set a local password for Plex-only users")
        user.password_hash = hash_password(payload.password)
        user.password_reset_required = True
    user.updated_at = datetime.utcnow()

    await session.commit()
    await session.refresh(user)

    if payload.status == "disabled":
        await download_manager.disconnect_user_websockets(user_id)

    return user.to_dict()


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    _admin: AuthContext = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(User).where(User.id == user_id, User.role == "download_only"))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    await session.execute(delete(UserIdentity).where(UserIdentity.user_id == user.id))
    await session.execute(delete(UserSetupToken).where(UserSetupToken.user_id == user.id))

    non_terminal_statuses = [
        ScheduledStatus.SCHEDULED.value,
        ScheduledStatus.QUEUED.value,
        ScheduledStatus.DOWNLOADING.value,
        ScheduledStatus.PROCESSING.value,
        ScheduledStatus.PAUSED_LOW_SPACE.value,
    ]
    sched_result = await session.execute(
        select(ScheduledRecording).where(
            ScheduledRecording.requested_by_user_id == user.id,
            ScheduledRecording.status.in_(non_terminal_statuses),
        )
    )
    for sched in sched_result.scalars().all():
        sched.status = ScheduledStatus.CANCELLED.value
        sched.status_message = "User deleted"

    active_dl_statuses = [
        DownloadStatus.PENDING.value,
        DownloadStatus.DOWNLOADING.value,
        DownloadStatus.PROCESSING.value,
    ]
    dl_result = await session.execute(
        select(Download).where(
            Download.requested_by_user_id == user.id,
            Download.status.in_(active_dl_statuses),
        )
    )
    active_dl_ids = [d.id for d in dl_result.scalars().all()]

    await session.delete(user)
    await session.commit()

    for dl_id in active_dl_ids:
        await download_manager.cancel_download(dl_id)

    await download_manager.disconnect_user_websockets(user.id)
    return {"status": "deleted"}


@router.post("/users/{user_id}/setup-link")
async def generate_setup_link(
    user_id: int,
    _admin: AuthContext = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(User).where(User.id == user_id, User.role == "download_only"))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.status == "disabled":
        raise HTTPException(status_code=400, detail="Cannot generate a setup link for a disabled user")
    token = await _create_setup_token(session, user_id)
    return {
        "token": token,
        "setup_path": f"/login?setup_token={token}",
    }
