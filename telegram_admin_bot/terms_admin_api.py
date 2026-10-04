"""Admin API for the terms of service and the sign-up switch. Mounted by
panel.py behind the admin login: only the platform admin writes the terms
or opens sign-up (terms.py has the rules)."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import audit
import terms

router = APIRouter()
ACTOR = audit.ADMIN

_get_pool: Callable[[], Any] = lambda: None  # noqa: E731
_get_bus: Callable[[], Any] = lambda: None  # noqa: E731


def bind(*, get_pool: Callable[[], Any], get_bus: Callable[[], Any]) -> None:
    global _get_pool, _get_bus
    _get_pool, _get_bus = get_pool, get_bus


async def _state() -> dict[str, Any]:
    pool = _get_pool()
    history = await terms.history(pool)
    return {
        "current": history[0] if history else None,
        "history": history,
        "required_version": await terms.required_version(pool),
        # Active client logins that still have to accept the required version.
        "outstanding": await terms.outstanding(pool),
        "signup": {**await terms.signup_settings(pool), "open": await terms.signup_open(pool)},
        "starter": {"title": terms.STARTER_TITLE, "body": terms.STARTER_BODY},
        "placeholder": terms.PLACEHOLDER,
    }


@router.get("/api/platform/terms")
async def api_terms() -> dict[str, Any]:
    return await _state()


class PublishBody(BaseModel):
    title: str = Field(..., max_length=terms.MAX_TITLE + 100)
    body: str = Field(..., max_length=terms.MAX_BODY + 1000)
    change_note: str = Field("", max_length=terms.MAX_NOTE + 100)
    # false = a correction nobody has to accept again (the first version always is required).
    requires_acceptance: bool = True


@router.post("/api/platform/terms")
async def api_publish(body: PublishBody) -> dict[str, Any]:
    try:
        await terms.publish(_get_pool(), title=body.title, body=body.body, change_note=body.change_note,
                            requires_acceptance=body.requires_acceptance, actor=ACTOR)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return await _state()


class SignupBody(BaseModel):
    enabled: bool


@router.put("/api/platform/signup")
async def api_signup(body: SignupBody) -> dict[str, Any]:
    try:
        await terms.save_signup_settings(_get_pool(), enabled=body.enabled, actor=ACTOR)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return await _state()
