"""Client routes for verification and photos (review.py), under /api/owner/*.

Mounted by panel.py without the admin login, like owner_api.py: every
route checks the client login (owner_auth.py) and the business is always
one of theirs (404 otherwise).

- Verification works while the dashboard is still locked for it (gate
  verify_identity), and for a verified login that wants to see its state.
- Photos are only for businesses whose industry requires review: a client
  adds or replaces a photo by submitting it, and the bot gets it once the
  admin approves. Removing a photo takes effect at once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response

import owner_auth
import review

router = APIRouter()

_get_pool: Callable[[], Any] = lambda: None  # noqa: E731
_get_bus: Callable[[], Any] = lambda: None  # noqa: E731
_get_data_dir: Callable[[], Path] = lambda: Path("data")  # noqa: E731


def bind(*, get_pool: Callable[[], Any], get_bus: Callable[[], Any], get_data_dir: Callable[[], Path]) -> None:
    global _get_pool, _get_bus, _get_data_dir
    _get_pool, _get_bus, _get_data_dir = get_pool, get_bus, get_data_dir


def _fail(exc: review.ReviewError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


async def verifying_owner(request: Request) -> dict[str, Any]:
    """A logged-in owner past every gate but (possibly) verification."""
    owner = await owner_auth.any_owner(request)
    reason = owner_auth.gate(owner)
    if reason not in (None, owner_auth.VERIFY_IDENTITY):
        raise HTTPException(status_code=403, detail=reason)
    return owner


# ------------------------------------------------------------ verification


@router.get("/api/owner/verification")
async def api_verification(owner: dict = Depends(verifying_owner)) -> dict[str, Any]:
    latest = await review.latest_verification(_get_pool(), owner["id"])
    return {**owner["verification"], "latest": review.public_verification(latest, with_challenge=True),
            "instructions": review.INSTRUCTIONS, "max_mb": review.MAX_VIDEO_BYTES // (1024 * 1024)}


@router.post("/api/owner/verification/challenge")
async def api_challenge(owner: dict = Depends(verifying_owner)) -> dict[str, Any]:
    if not owner["verification"]["required"]:
        raise HTTPException(status_code=409, detail="No verification is needed for your account.")
    try:
        row = await review.new_challenge(_get_pool(), owner["id"], actor=owner_auth.actor(owner))
    except review.ReviewError as exc:
        raise _fail(exc) from None
    return review.public_verification(row, with_challenge=True)


@router.put("/api/owner/verification/video")
async def api_video(request: Request, name: str = "video.mp4",
                    owner: dict = Depends(verifying_owner)) -> dict[str, Any]:
    """The raw video as the body (no form): `name` only gives its type."""
    try:
        data = await review.read_limited(request.stream(), review.MAX_VIDEO_BYTES)
        row = await review.submit_video(_get_pool(), _get_data_dir(), owner["id"], data, name,
                                        actor=owner_auth.actor(owner))
    except review.ReviewError as exc:
        raise _fail(exc) from None
    return review.public_verification(row, with_challenge=False)


# ------------------------------------------------------------------ photos


async def _review_tenant(owner: dict[str, Any], tenant_id: int) -> int:
    if tenant_id not in owner["tenant_ids"]:
        raise HTTPException(status_code=404, detail="Unknown business")
    if not await review.industry_requires_review(_get_pool(), tenant_id):
        raise HTTPException(status_code=404, detail="Photos are not managed here for this business.")
    return tenant_id


async def _tenant_row(tenant_id: int) -> dict[str, Any]:
    row = await _get_pool().fetchrow("SELECT id, name, session_id FROM tenants WHERE id = $1", tenant_id)
    return dict(row)


@router.get("/api/owner/tenants/{tenant_id}/photos")
async def api_photos(tenant_id: int, owner: dict = Depends(owner_auth.current_owner)) -> dict[str, Any]:
    await _review_tenant(owner, tenant_id)
    library = review.library_for(_get_data_dir(), await _tenant_row(tenant_id))
    return {
        "live": [{"id": i["id"], "kind": i["kind"], "description": i["description"]} for i in library.all()],
        "submissions": await review.list_submissions(_get_pool(), tenant_ids=[tenant_id], limit=100),
        "max_mb": review.MAX_PHOTO_BYTES // (1024 * 1024),
    }


@router.put("/api/owner/tenants/{tenant_id}/photos")
async def api_submit(tenant_id: int, request: Request, name: str = "photo.jpg", description: str = "",
                     replaces: Optional[int] = None,
                     owner: dict = Depends(owner_auth.current_owner)) -> dict[str, Any]:
    """The raw photo as the body; it waits for the admin's approval."""
    await _review_tenant(owner, tenant_id)
    try:
        data = await review.read_limited(request.stream(), review.MAX_PHOTO_BYTES)
        return await review.submit_photo(_get_pool(), _get_data_dir(), tenant_id, owner["id"], data,
                                         filename=name, description=description, replaces=replaces,
                                         actor=owner_auth.actor(owner))
    except review.ReviewError as exc:
        raise _fail(exc) from None


@router.get("/api/owner/tenants/{tenant_id}/photos/{item_id}/file")
async def api_photo_file(tenant_id: int, item_id: int,
                         owner: dict = Depends(owner_auth.current_owner)) -> FileResponse:
    await _review_tenant(owner, tenant_id)
    library = review.library_for(_get_data_dir(), await _tenant_row(tenant_id))
    path = library.path(item_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Unknown photo")
    return FileResponse(path, media_type=review.content_type(path.name))


@router.delete("/api/owner/tenants/{tenant_id}/photos/{item_id}")
async def api_remove(tenant_id: int, item_id: int, owner: dict = Depends(owner_auth.current_owner)) -> dict:
    await _review_tenant(owner, tenant_id)
    try:
        await review.remove_photo(_get_pool(), _get_data_dir(), tenant_id, item_id, actor=owner_auth.actor(owner))
    except review.ReviewError as exc:
        raise _fail(exc) from None
    return {"ok": True}


async def _own_submission(owner: dict[str, Any], tenant_id: int, submission_id: int) -> dict[str, Any]:
    await _review_tenant(owner, tenant_id)
    try:
        sub = await review.get_submission(_get_pool(), submission_id)
    except review.ReviewError as exc:
        raise _fail(exc) from None
    if sub["tenant_id"] != tenant_id or sub["source"] != "owner":
        raise HTTPException(status_code=404, detail="Unknown submission")
    return sub


@router.get("/api/owner/tenants/{tenant_id}/submissions/{submission_id}/file")
async def api_submission_file(tenant_id: int, submission_id: int,
                              owner: dict = Depends(owner_auth.current_owner)) -> Response:
    sub = await _own_submission(owner, tenant_id, submission_id)
    path = review.submission_path(_get_data_dir(), sub)
    if path is None:
        raise HTTPException(status_code=404, detail="The file is no longer kept.")
    return FileResponse(path, media_type=review.content_type(path.name))


@router.delete("/api/owner/tenants/{tenant_id}/submissions/{submission_id}")
async def api_withdraw(tenant_id: int, submission_id: int,
                       owner: dict = Depends(owner_auth.current_owner)) -> dict[str, Any]:
    await _own_submission(owner, tenant_id, submission_id)
    try:
        return await review.withdraw(_get_pool(), _get_data_dir(), submission_id, tenant_ids=[tenant_id],
                                     actor=owner_auth.actor(owner))
    except review.ReviewError as exc:
        raise _fail(exc) from None
