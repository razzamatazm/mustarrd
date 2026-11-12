"""The bell (#430): list, dismiss one, dismiss all. Any signed-in user."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from auth import AuthContext, require_admin_or_download_user
from database import get_session
from services import notifications as notification_service

router = APIRouter()


@router.get("")
async def list_notifications(
    context: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    items = await notification_service.list_for(session, context)
    return {"items": items, "count": len(items)}


@router.post("/dismiss-all")
async def dismiss_all_notifications(
    context: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    count = await notification_service.dismiss_all(session, context)
    return {"dismissed": count}


@router.post("/{notification_id}/dismiss")
async def dismiss_notification(
    notification_id: int,
    context: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    if not await notification_service.dismiss(session, notification_id, context):
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"dismissed": True}
