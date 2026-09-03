import re
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, conint, field_validator, model_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth import AuthContext, require_admin_or_download_user
from database import get_session
from models import RecordingRule, ScheduledRecording, XtreamAccount
from services.recording_rule_service import recording_rule_service


router = APIRouter()
MatchMode = Literal["exact", "contains", "regex"]


def _validate_rule_pattern(match_mode: str, title_match: str) -> None:
    if match_mode == "regex":
        try:
            re.compile(title_match, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"Invalid regular expression: {exc}") from exc


class RecordingRuleCreate(BaseModel):
    account_id: int
    channel_id: str
    channel_name: str
    title_match: str
    match_mode: MatchMode = "exact"
    enabled: bool = True
    days_of_week: Optional[list[conint(ge=0, le=6)]] = None
    delete_after_days: Optional[conint(ge=1, le=3650)] = None
    pre_padding_minutes: conint(ge=0, le=120) = 0
    post_padding_minutes: conint(ge=0, le=120) = 0

    @field_validator("channel_id", "channel_name", "title_match")
    @classmethod
    def require_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @model_validator(mode="after")
    def validate_pattern(self):
        _validate_rule_pattern(self.match_mode, self.title_match)
        return self


class RecordingRuleUpdate(BaseModel):
    enabled: Optional[bool] = None
    title_match: Optional[str] = None
    match_mode: Optional[MatchMode] = None
    days_of_week: Optional[list[conint(ge=0, le=6)]] = None
    delete_after_days: Optional[conint(ge=1, le=3650)] = None
    pre_padding_minutes: Optional[conint(ge=0, le=120)] = None
    post_padding_minutes: Optional[conint(ge=0, le=120)] = None

    @field_validator("title_match")
    @classmethod
    def require_non_empty_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @model_validator(mode="after")
    def require_update(self):
        if not self.model_fields_set:
            raise ValueError("at least one field must be supplied")
        required_when_present = (
            "enabled",
            "title_match",
            "match_mode",
            "pre_padding_minutes",
            "post_padding_minutes",
        )
        for field in required_when_present:
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} must not be null")
        return self


def _rule_query_for_auth(auth: AuthContext):
    query = select(RecordingRule)
    if not auth.is_admin:
        query = query.where(RecordingRule.requested_by_user_id == auth.user_id)
    return query


@router.get("")
async def list_recording_rules(
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    query = _rule_query_for_auth(auth).order_by(RecordingRule.created_at.desc())
    rules = (await session.execute(query)).scalars().all()
    return [rule.to_dict() for rule in rules]


@router.post("")
async def create_recording_rule(
    data: RecordingRuleCreate,
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    account = (
        await session.execute(
            select(XtreamAccount).where(XtreamAccount.id == data.account_id)
        )
    ).scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    rule = RecordingRule(
        account_id=data.account_id,
        requested_by_user_id=auth.user_id,
        channel_id=data.channel_id,
        channel_name=data.channel_name,
        title_match=data.title_match,
        match_mode=data.match_mode,
        enabled=data.enabled,
        delete_after_days=data.delete_after_days,
        pre_padding_minutes=data.pre_padding_minutes,
        post_padding_minutes=data.post_padding_minutes,
        request_source=auth.provider or "admin_local",
    )
    rule.set_days_of_week(data.days_of_week)
    session.add(rule)
    await session.commit()
    await session.refresh(rule)

    scheduled_count = await recording_rule_service.evaluate(session, rule_id=rule.id)
    response = rule.to_dict()
    response["scheduled_count"] = scheduled_count
    return response


@router.patch("/{rule_id}")
async def update_recording_rule(
    rule_id: int,
    data: RecordingRuleUpdate,
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    rule = (
        await session.execute(
            _rule_query_for_auth(auth).where(RecordingRule.id == rule_id)
        )
    ).scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Recording rule not found")

    fields = data.model_fields_set
    new_match_mode = data.match_mode if "match_mode" in fields else rule.match_mode
    new_title_match = data.title_match if "title_match" in fields else rule.title_match
    try:
        _validate_rule_pattern(new_match_mode or "exact", new_title_match)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    for field in (
        "enabled",
        "title_match",
        "match_mode",
        "delete_after_days",
        "pre_padding_minutes",
        "post_padding_minutes",
    ):
        if field in fields:
            setattr(rule, field, getattr(data, field))
    if "days_of_week" in fields:
        rule.set_days_of_week(data.days_of_week)
    rule.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(rule)
    scheduled_count = 0
    if rule.enabled:
        scheduled_count = await recording_rule_service.evaluate(session, rule_id=rule.id)
    response = rule.to_dict()
    response["scheduled_count"] = scheduled_count
    return response


@router.delete("/{rule_id}")
async def delete_recording_rule(
    rule_id: int,
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    rule = (
        await session.execute(
            _rule_query_for_auth(auth).where(RecordingRule.id == rule_id)
        )
    ).scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Recording rule not found")

    # Existing schedules are independent snapshots. Detach them before deleting
    # the rule so no schedule or completed recording is removed as a side effect.
    await session.execute(
        update(ScheduledRecording)
        .where(ScheduledRecording.recording_rule_id == rule.id)
        .values(recording_rule_id=None)
    )
    await session.delete(rule)
    await session.commit()
    return {"status": "deleted"}
