"""Admin API for content review (review.py). Mounted by panel.py behind the
admin login: only the platform admin sees verification videos and decides
on photos. Managers don't reach any of it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

import audit
import review

router = APIRouter()
ACTOR = audit.ADMIN

_get_pool: Callable[[], Any] = lambda: None  # noqa: E731
_get_bus: Callable[[], Any] = lambda: None  # noqa: E731
_get_data_dir: Callable[[], Path] = lambda: Path("data")  # noqa: E731


def bind(*, get_pool: Callable[[], Any], get_bus: Callable[[], Any], get_data_dir: Callable[[], Path]) -> None:
    global _get_pool, _get_bus, _get_data_dir
    _get_pool, _get_bus, _get_data_dir = get_pool, get_bus, get_data_dir


def _fail(exc: review.ReviewError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


@router.get("/api/review/summary")
async def api_summary() -> dict[str, Any]:
    """Counts for the badge, and the businesses that need review."""
    pool = _get_pool()
    rows = await pool.fetch(
        """
        SELECT t.id, t.name, i.name AS industry,
               EXISTS (SELECT 1 FROM tenant_holds h WHERE h.tenant_id = t.id AND h.kind = 'verification') AS held,
               (SELECT count(*) FROM media_submissions s WHERE s.tenant_id = t.id AND s.status = 'pending')
                 AS pending_photos,
               coalesce((SELECT json_agg(json_build_object(
                           'id', o.id, 'username', o.username,
                           'status', (SELECT status FROM verifications v WHERE v.owner_id = o.id
                                       ORDER BY v.id DESC LIMIT 1)) ORDER BY o.id)
                           FROM owner_tenants ot JOIN owners o ON o.id = ot.owner_id
                          WHERE ot.tenant_id = t.id), '[]'::json) AS owners
          FROM tenants t JOIN industries i ON i.id = t.industry_id
         WHERE i.requires_review
            OR EXISTS (SELECT 1 FROM tenant_holds h WHERE h.tenant_id = t.id AND h.kind = 'verification')
         ORDER BY lower(t.name), t.id
        """
    )
    return {
        "pending": await review.pending_counts(pool),
        "tenants": [{**dict(r), "owners": json.loads(r["owners"]) if isinstance(r["owners"], str) else r["owners"]}
                    for r in rows],
    }


# ------------------------------------------------------------ verification


@router.get("/api/review/verifications")
async def api_verifications(status: Optional[str] = None) -> list[dict[str, Any]]:
    if status not in (None, review.REQUESTED, review.SUBMITTED, review.APPROVED, review.REJECTED):
        raise HTTPException(status_code=400, detail="Unknown status")
    return await review.list_verifications(_get_pool(), status=status)


@router.get("/api/review/verifications/{verification_id}/video")
async def api_video(verification_id: int) -> Response:
    try:
        data, media_type = await review.read_video(_get_pool(), _get_data_dir(), verification_id)
    except review.ReviewError as exc:
        raise _fail(exc) from None
    return Response(content=data, media_type=media_type, headers={"Cache-Control": "no-store"})


class DecisionBody(BaseModel):
    reason: str = Field("", max_length=500)
    # Photos only: the description the bot will see, if the admin edits it.
    description: Optional[str] = Field(None, max_length=review.MAX_DESCRIPTION)


@router.post("/api/review/verifications/{verification_id}/approve")
async def api_approve_verification(verification_id: int, body: DecisionBody) -> dict[str, Any]:
    try:
        row = await review.decide_verification(_get_pool(), _get_bus(), verification_id, approve=True,
                                               actor=ACTOR, reason=body.reason)
    except review.ReviewError as exc:
        raise _fail(exc) from None
    return review.public_verification(row, with_challenge=False)


@router.post("/api/review/verifications/{verification_id}/reject")
async def api_reject_verification(verification_id: int, body: DecisionBody) -> dict[str, Any]:
    try:
        row = await review.decide_verification(_get_pool(), _get_bus(), verification_id, approve=False,
                                               actor=ACTOR, reason=body.reason)
    except review.ReviewError as exc:
        raise _fail(exc) from None
    return review.public_verification(row, with_challenge=False)


class ReasonBody(BaseModel):
    reason: str = Field("", max_length=500)


@router.post("/api/owners/{owner_id}/request-verification")
async def api_request_verification(owner_id: int, body: ReasonBody) -> dict[str, Any]:
    """Suspected: the login must send a new video; its businesses pause."""
    try:
        row = await review.request_verification(_get_pool(), _get_bus(), owner_id, actor=ACTOR, reason=body.reason)
    except review.ReviewError as exc:
        raise _fail(exc) from None
    return review.public_verification(row, with_challenge=False)


# ------------------------------------------------------------------ photos


@router.get("/api/review/photos")
async def api_photos(status: Optional[str] = "pending", tenant_id: Optional[int] = None) -> list[dict[str, Any]]:
    if status == "all":
        status = None
    return await review.list_submissions(_get_pool(), status=status,
                                         tenant_ids=[tenant_id] if tenant_id is not None else None)


@router.get("/api/review/photos/{submission_id}/file")
async def api_photo_file(submission_id: int) -> FileResponse:
    try:
        sub = await review.get_submission(_get_pool(), submission_id)
    except review.ReviewError as exc:
        raise _fail(exc) from None
    path = review.submission_path(_get_data_dir(), sub)
    if path is None:
        raise HTTPException(status_code=404, detail="The file is no longer kept.")
    return FileResponse(path, media_type=review.content_type(path.name))


@router.post("/api/review/photos/{submission_id}/approve")
async def api_approve_photo(submission_id: int, body: DecisionBody) -> dict[str, Any]:
    try:
        return await review.decide_submission(_get_pool(), _get_data_dir(), submission_id, approve=True,
                                              actor=ACTOR, reason=body.reason, description=body.description)
    except review.ReviewError as exc:
        raise _fail(exc) from None


@router.post("/api/review/photos/{submission_id}/reject")
async def api_reject_photo(submission_id: int, body: DecisionBody) -> dict[str, Any]:
    try:
        return await review.decide_submission(_get_pool(), _get_data_dir(), submission_id, approve=False,
                                              actor=ACTOR, reason=body.reason)
    except review.ReviewError as exc:
        raise _fail(exc) from None


@router.post("/api/tenants/{tenant_id}/recheck-media")
async def api_recheck(tenant_id: int, body: ReasonBody) -> dict[str, Any]:
    """Suspected: every live photo and video of the business goes back into
    review; the bot stops using them now."""
    try:
        moved = await review.recheck_tenant(_get_pool(), _get_data_dir(), tenant_id, actor=ACTOR,
                                            reason=body.reason)
    except review.ReviewError as exc:
        raise _fail(exc) from None
    return {"moved": moved}


# -------------------------------------------------------------- industries


@router.get("/api/review/industries")
async def api_industries() -> list[dict[str, Any]]:
    rows = await _get_pool().fetch(
        "SELECT i.id, i.name, i.requires_review, (SELECT count(*) FROM tenants t WHERE t.industry_id = i.id) AS "
        "tenants FROM industries i ORDER BY lower(i.name)"
    )
    return [dict(r) for r in rows]


class IndustryBody(BaseModel):
    requires_review: bool


@router.put("/api/review/industries/{industry_id}")
async def api_set_industry(industry_id: int, body: IndustryBody) -> dict[str, Any]:
    try:
        return await review.set_industry_review(_get_pool(), _get_bus(), industry_id, body.requires_review,
                                                actor=ACTOR)
    except review.ReviewError as exc:
        raise _fail(exc) from None
