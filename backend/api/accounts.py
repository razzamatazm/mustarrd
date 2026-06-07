import asyncio
import logging
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from auth import require_admin, require_admin_or_download_user, AuthContext
from database import get_session
from models import AppSettings, XtreamAccount, ScheduledRecording, ScheduledStatus, Download, DownloadStatus
from services.download_manager import download_manager
from services.epg_ingest_manager import epg_ingest_manager
from services.account_credentials import (
    encrypt_account_password,
    resolve_account_password_with_migration,
)
from services.xtream_client import XtreamClient


router = APIRouter()
logger = logging.getLogger(__name__)


def _log_task_result(task: asyncio.Task):
    try:
        task.result()
    except Exception as exc:
        logger.exception("EPG refresh task failed: %s", exc)


class AccountCreate(BaseModel):
    name: str
    server_url: str
    username: str
    password: str
    guide_offset_hours: int = 0


class AccountUpdate(BaseModel):
    name: str | None = None
    server_url: str | None = None
    username: str | None = None
    password: str | None = None
    is_active: bool | None = None
    guide_offset_hours: int | None = None


@router.get("")
async def list_accounts(
    _admin: None = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """List all Xtream accounts."""
    result = await session.execute(select(XtreamAccount).order_by(XtreamAccount.name))
    accounts = result.scalars().all()
    return [acc.to_dict() for acc in accounts]


@router.get("/public")
async def list_accounts_public(
    _auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    """List minimal account info for non-admin browsing flows."""
    result = await session.execute(select(XtreamAccount).order_by(XtreamAccount.name))
    accounts = result.scalars().all()
    return [
        {
            "id": acc.id,
            "name": acc.name,
            "is_active": acc.is_active,
            "guide_offset_hours": int(acc.guide_offset_hours or 0),
        }
        for acc in accounts
    ]


@router.post("")
async def create_account(
    account: AccountCreate,
    _admin: None = Depends(require_admin),
    session: AsyncSession = Depends(get_session)
):
    """Create a new Xtream account (validates connection)."""
    # Test connection first
    client = XtreamClient(account.server_url, account.username, account.password)
    try:
        auth_data = await client.authenticate()
    except Exception:
        logger.exception(
            "Account validation failed during create account_name=%s server_url=%s username=%s",
            account.name,
            account.server_url,
            account.username,
        )
        raise HTTPException(status_code=400, detail="Provider connection failed")
    finally:
        await client.close()

    # Extract account info from auth response
    user_info = auth_data.get("user_info", {})
    server_info = auth_data.get("server_info", {})

    # Parse expiration date
    exp_date = None
    if user_info.get("exp_date"):
        try:
            exp_date = datetime.fromtimestamp(int(user_info["exp_date"]))
        except (ValueError, TypeError):
            pass

    # Create account
    db_account = XtreamAccount(
        name=account.name,
        server_url=account.server_url,
        username=account.username,
        password="",
        password_encrypted=encrypt_account_password(account.password),
        max_connections=user_info.get("max_connections"),
        active_connections=user_info.get("active_cons"),
        expiration_date=exp_date,
        guide_offset_hours=int(account.guide_offset_hours or 0),
    )

    session.add(db_account)
    await session.commit()
    await session.refresh(db_account)

    refresh_task = asyncio.create_task(epg_ingest_manager.refresh_account_by_id(db_account.id))
    refresh_task.add_done_callback(_log_task_result)

    return db_account.to_dict()


@router.get("/{account_id}")
async def get_account(
    account_id: int,
    _admin: None = Depends(require_admin),
    session: AsyncSession = Depends(get_session)
):
    """Get a specific account."""
    result = await session.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalar_one_or_none()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    return account.to_dict()


@router.put("/{account_id}")
async def update_account(
    account_id: int,
    update_data: AccountUpdate,
    _admin: None = Depends(require_admin),
    session: AsyncSession = Depends(get_session)
):
    """Update an account."""
    result = await session.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalar_one_or_none()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    # Update fields
    update_dict = update_data.model_dump(exclude_unset=True)
    for field, value in update_dict.items():
        if field == "password":
            if value is None:
                continue
            account.password_encrypted = encrypt_account_password(value)
            account.password = ""
            continue
        if field == "guide_offset_hours" and value is not None:
            value = int(value)
        setattr(account, field, value)

    await session.commit()
    await session.refresh(account)

    return account.to_dict()


@router.delete("/{account_id}")
async def delete_account(
    account_id: int,
    _admin: None = Depends(require_admin),
    session: AsyncSession = Depends(get_session)
):
    """Delete an account."""
    result = await session.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalar_one_or_none()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    settings_result = await session.execute(select(AppSettings))
    app_settings = settings_result.scalar_one_or_none()
    if app_settings and app_settings.default_account_id == account.id:
        app_settings.default_account_id = None

    active_schedule_statuses = [
        ScheduledStatus.SCHEDULED.value,
        ScheduledStatus.QUEUED.value,
        ScheduledStatus.DOWNLOADING.value,
        ScheduledStatus.PROCESSING.value,
        ScheduledStatus.PAUSED_LOW_SPACE.value,
    ]
    sched_result = await session.execute(
        select(ScheduledRecording).where(
            ScheduledRecording.account_id == account.id,
            ScheduledRecording.status.in_(active_schedule_statuses),
        )
    )
    for sched in sched_result.scalars().all():
        sched.status = ScheduledStatus.CANCELLED.value
        sched.status_message = "Account deleted"

    active_download_statuses = [
        DownloadStatus.PENDING.value,
        DownloadStatus.DOWNLOADING.value,
        DownloadStatus.PROCESSING.value,
    ]
    dl_result = await session.execute(
        select(Download).where(
            Download.account_id == account.id,
            Download.status.in_(active_download_statuses),
        )
    )
    active_dl_ids = [d.id for d in dl_result.scalars().all()]

    await session.delete(account)
    await session.commit()

    for dl_id in active_dl_ids:
        await download_manager.cancel_download(dl_id)

    return {"status": "deleted"}


@router.post("/{account_id}/test")
async def test_account(
    account_id: int,
    _admin: None = Depends(require_admin),
    session: AsyncSession = Depends(get_session)
):
    """Test connection to an account."""
    result = await session.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalar_one_or_none()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    try:
        password = await resolve_account_password_with_migration(session, account)
    except ValueError:
        logger.exception("Account credentials missing or invalid account_id=%s", account_id)
        raise HTTPException(status_code=400, detail="Account credentials are not configured")

    client = XtreamClient(account.server_url, account.username, password)
    try:
        auth_data = await client.authenticate()

        # Update cached info
        user_info = auth_data.get("user_info", {})

        exp_date = None
        if user_info.get("exp_date"):
            try:
                exp_date = datetime.fromtimestamp(int(user_info["exp_date"]))
            except (ValueError, TypeError):
                pass

        account.max_connections = user_info.get("max_connections")
        account.active_connections = user_info.get("active_cons")
        account.expiration_date = exp_date
        account.last_used = datetime.utcnow()

        await session.commit()

        return {
            "status": "connected",
            "user_info": user_info,
            "server_info": auth_data.get("server_info", {}),
        }

    except Exception:
        logger.exception("Account test failed account_id=%s", account_id)
        raise HTTPException(status_code=400, detail="Provider connection failed")
    finally:
        await client.close()
