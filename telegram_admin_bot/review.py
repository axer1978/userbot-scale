"""Content review: identity/age verification by video, and admin review of
every photo a client submits (migration 0008).

Who it applies to
- An industry marked `requires_review` (the escort market). Every tenant in
  it is held ('verification' hold, controls.py) until at least one client
  login linked to it has an approved verification, and its client can only
  add photos through review.
- Any client login the admin asks to verify again ("suspected"): its
  tenants are held until the new video is approved, whatever the industry.

Verification
- The client asks for a challenge: a random code to write on paper and a
  random gesture. Within CHALLENGE_TTL they upload a short video of
  themselves showing both (a phone records it straight from the page).
  A video made earlier can't know the code.
- The video is kept AES-GCM encrypted under the master key (crypto.py),
  never in plain on disk, served only to the admin, and deleted
  VIDEO_RETENTION_DAYS after the decision.
- The newest verifications row is the login's state: 'approved' = verified.

Photos
- A submission waits under DATA_DIR/review/, outside the media folder the
  bot reads (media.MediaLibrary), so the bot can't send a photo nobody
  approved. Approving copies it into the library (replacing the photo it
  was meant to replace); rejecting keeps the file for the retention period.
- "Recheck" pulls every live file of a tenant back into review.
- An upload must really be the image type its name says (magic bytes).

Every step writes an audit row, and anything new to review raises the
platform alert 'review_pending' (delivered by e-mail/webhook, alerts.py).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import asyncpg

import alerts
import audit
import controls
import crypto
import media
import tenants

log = logging.getLogger("review")

SYSTEM = audit.SYSTEM
ALERT_KIND = "review_pending"

CHALLENGE_TTL = timedelta(minutes=30)
VIDEO_RETENTION_DAYS = 30
MAX_VIDEO_BYTES = 80 * 1024 * 1024
MAX_PHOTO_BYTES = 15 * 1024 * 1024
MAX_DESCRIPTION = 300

# No 0/O/1/I/L: the code is read off a sheet of paper in a video.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
GESTURES = (
    "hold up two fingers next to your face",
    "hold up three fingers next to your face",
    "touch your left ear",
    "touch your right ear",
    "give a thumbs up",
    "turn your head to the left, then to the right",
    "cover one eye with your hand, then show your whole face again",
    "wave at the camera",
)
INSTRUCTIONS = [
    "Write the code below clearly on a sheet of paper.",
    "Record a short video (5 to 20 seconds) of yourself, face clearly visible, in good light.",
    "In the video, hold the paper with the code next to your face, then do the gesture below.",
    "Upload the video here within 30 minutes. After that you need a new code.",
    "Only the platform administrator sees the video. It is deleted 30 days after the decision.",
]

VIDEO_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/mp4", ".webm": "video/webm",
               ".3gp": "video/3gpp"}
PHOTO_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

# Verification states
REQUESTED, SUBMITTED, APPROVED, REJECTED = "requested", "submitted", "approved", "rejected"
# Submission states
PENDING, WITHDRAWN = "pending", "withdrawn"

EVENT_VERIFICATION_REQUESTED = "verification_requested"
EVENT_VERIFICATION_SUBMITTED = "verification_submitted"
EVENT_VERIFICATION_APPROVED = "verification_approved"
EVENT_VERIFICATION_REJECTED = "verification_rejected"
EVENT_MEDIA_SUBMITTED = "media_submitted"
EVENT_MEDIA_APPROVED = "media_approved"
EVENT_MEDIA_REJECTED = "media_rejected"
EVENT_MEDIA_WITHDRAWN = "media_withdrawn"
EVENT_MEDIA_REMOVED = "media_removed"
EVENT_MEDIA_RECHECK = "media_recheck"
EVENT_INDUSTRY_REVIEW = "industry_review_changed"


class ReviewError(Exception):
    """Something the person asking can fix; `status` is the HTTP status."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _iso(value: Any) -> Optional[str]:
    return value.isoformat(timespec="seconds") if value else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ paths


def review_root(data_root: Path) -> Path:
    return Path(data_root) / "review"


def _photo_dir(data_root: Path, tenant_id: int) -> Path:
    return review_root(data_root) / "photos" / str(int(tenant_id))


def _video_path(data_root: Path, name: str) -> Path:
    return review_root(data_root) / "verifications" / Path(name).name


def submission_path(data_root: Path, sub: dict[str, Any]) -> Optional[Path]:
    """The file of a submission, or None once it was deleted."""
    root = _photo_dir(data_root, sub["tenant_id"]).resolve()
    path = (root / Path(sub["file"]).name).resolve()
    return path if path.parent == root and path.is_file() else None


def content_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    return PHOTO_TYPES.get(ext) or VIDEO_TYPES.get(ext) or "application/octet-stream"


def sniff_photo(head: bytes) -> Optional[str]:
    """The real extension of an image from its first bytes, or None."""
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return None


def sniff_video(head: bytes) -> bool:
    """An ISO media file (mp4/mov/3gp: 'ftyp' at byte 4) or WebM/Matroska."""
    return head[4:8] == b"ftyp" or head.startswith(b"\x1a\x45\xdf\xa3")


async def read_limited(chunks: AsyncIterator[bytes], limit: int) -> bytes:
    data = bytearray()
    async for chunk in chunks:
        data += chunk
        if len(data) > limit:
            raise ReviewError(f"The file is larger than {limit // (1024 * 1024)} MB.", 413)
    if not data:
        raise ReviewError("The file is empty.")
    return bytes(data)


def _video_aad(verification_id: int) -> bytes:
    return crypto.aad_for(f"verification:{verification_id}", "video")


# ----------------------------------------------------------- who needs it


async def industry_requires_review(executor: Any, tenant_id: int) -> bool:
    return bool(await executor.fetchval(
        "SELECT i.requires_review FROM tenants t JOIN industries i ON i.id = t.industry_id WHERE t.id = $1",
        tenant_id,
    ))


async def latest_verification(executor: Any, owner_id: int) -> Optional[dict[str, Any]]:
    row = await executor.fetchrow("SELECT * FROM verifications WHERE owner_id = $1 ORDER BY id DESC LIMIT 1",
                                  owner_id)
    return dict(row) if row else None


def public_verification(row: Optional[dict[str, Any]], *, with_challenge: bool) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    out = {
        "id": row["id"], "status": row["status"], "reason": row["reason"], "requested_by": row["requested_by"],
        "submitted_at": _iso(row["submitted_at"]), "reviewed_at": _iso(row["reviewed_at"]),
        "review_reason": row["review_reason"], "created_at": _iso(row["created_at"]),
    }
    if with_challenge and row["status"] == REQUESTED and row["challenge"] and row["challenge_at"]:
        expires = row["challenge_at"] + CHALLENGE_TTL
        if expires > _now():
            out["challenge"] = {"code": row["challenge"], "gesture": row["gesture"], "expires_at": _iso(expires)}
    return out


async def owner_state(executor: Any, owner_id: int) -> dict[str, Any]:
    """{required, status, ok}: required = linked to a business whose
    industry requires review, or asked to verify by the admin."""
    linked = await executor.fetchval(
        """
        SELECT EXISTS (SELECT 1 FROM owner_tenants ot JOIN tenants t ON t.id = ot.tenant_id
                         JOIN industries i ON i.id = t.industry_id
                        WHERE ot.owner_id = $1 AND i.requires_review)
        """,
        owner_id,
    )
    latest = await latest_verification(executor, owner_id)
    status = latest["status"] if latest else None
    required = bool(linked) or (latest is not None and status != APPROVED)
    return {"required": required, "status": status, "ok": not required or status == APPROVED}


# ----------------------------------------------------------- the alert


async def pending_counts(executor: Any) -> dict[str, int]:
    row = await executor.fetchrow(
        "SELECT (SELECT count(*) FROM verifications WHERE status = 'submitted') AS verifications, "
        "(SELECT count(*) FROM media_submissions WHERE status = 'pending') AS photos"
    )
    return {"verifications": row["verifications"], "photos": row["photos"]}


async def _announce(pool: asyncpg.Pool, what: str, payload: dict[str, Any]) -> None:
    counts = await pending_counts(pool)
    await alerts.raise_alert(
        pool, tenant_id=None, kind=ALERT_KIND, severity=alerts.WARNING,
        message=f"Waiting for your review: {counts['verifications']} verification video(s), "
                f"{counts['photos']} photo(s). Newest: {what}",
        payload=payload,
    )


async def _settle_alert(pool: asyncpg.Pool, actor: str) -> None:
    counts = await pending_counts(pool)
    if not counts["verifications"] and not counts["photos"]:
        await alerts.resolve(pool, tenant_id=None, kind=ALERT_KIND, by=actor)


async def _audit_owner(con: Any, owner_id: int, actor: str, event: str, reason: str,
                       payload: dict[str, Any]) -> None:
    """One row per business of the login (a platform row when it has none)."""
    ids = [r["tenant_id"] for r in await con.fetch(
        "SELECT tenant_id FROM owner_tenants WHERE owner_id = $1 ORDER BY tenant_id", owner_id)]
    for tenant_id in ids or [None]:
        await audit.record(con, tenant_id=tenant_id, actor=actor, event=event, reason=reason,
                           payload={"owner_id": owner_id, **payload})


# ------------------------------------------------------------ verification


async def new_challenge(pool: asyncpg.Pool, owner_id: int, *, actor: str) -> dict[str, Any]:
    """A fresh code and gesture on the open round, opening one if needed."""
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
    code = f"{code[:3]}-{code[3:]}"
    gesture = secrets.choice(GESTURES)
    async with pool.acquire() as con, con.transaction():
        latest = await latest_verification(con, owner_id)
        if latest and latest["status"] == SUBMITTED:
            raise ReviewError("Your video is waiting for review.", 409)
        if latest and latest["status"] == REQUESTED:
            row = await con.fetchrow(
                "UPDATE verifications SET challenge = $2, gesture = $3, challenge_at = now() WHERE id = $1 RETURNING *",
                latest["id"], code, gesture,
            )
        else:
            row = await con.fetchrow(
                "INSERT INTO verifications (owner_id, status, reason, requested_by, challenge, gesture, challenge_at) "
                "VALUES ($1, 'requested', $2, $3, $4, $5, now()) RETURNING *",
                owner_id, "identity and age check", actor, code, gesture,
            )
    return dict(row)


async def submit_video(pool: asyncpg.Pool, data_root: Path, owner_id: int, data: bytes, filename: str, *,
                       actor: str) -> dict[str, Any]:
    ext = Path(filename).suffix.lower()
    if ext not in VIDEO_TYPES:
        raise ReviewError("Upload a video (mp4, mov, webm or 3gp).")
    if not sniff_video(data[:16]):
        raise ReviewError("That file is not a video.")
    latest = await latest_verification(pool, owner_id)
    if latest is None or latest["status"] != REQUESTED or not latest["challenge_at"]:
        raise ReviewError("Get a code first.", 409)
    if latest["challenge_at"] + CHALLENGE_TTL < _now():
        raise ReviewError("Your code expired. Get a new code and record the video again.", 409)
    name = f"{latest['id']}-{uuid.uuid4().hex}.bin"
    path = _video_path(data_root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(crypto.encrypt(data, aad=_video_aad(latest["id"])))
    os.replace(tmp, path)
    async with pool.acquire() as con, con.transaction():
        row = await con.fetchrow(
            "UPDATE verifications SET status = 'submitted', video_file = $2, video_type = $3, video_bytes = $4, "
            "submitted_at = now() WHERE id = $1 AND status = 'requested' RETURNING *",
            latest["id"], name, VIDEO_TYPES[ext], len(data),
        )
        if row is None:
            path.unlink(missing_ok=True)
            raise ReviewError("This verification changed in the meantime. Reload the page.", 409)
        await _audit_owner(con, owner_id, actor, EVENT_VERIFICATION_SUBMITTED, "verification video uploaded",
                           {"verification_id": row["id"], "bytes": len(data)})
    await _announce(pool, f"verification video from {actor}", {"verification_id": row["id"]})
    return dict(row)


async def read_video(pool: asyncpg.Pool, data_root: Path, verification_id: int) -> tuple[bytes, str]:
    row = await pool.fetchrow("SELECT id, video_file, video_type FROM verifications WHERE id = $1", verification_id)
    if row is None:
        raise ReviewError("Unknown verification", 404)
    if not row["video_file"]:
        raise ReviewError("The video was deleted.", 404)
    path = _video_path(data_root, row["video_file"])
    try:
        blob = path.read_bytes()
    except FileNotFoundError:
        raise ReviewError("The video file is missing.", 404) from None
    return crypto.decrypt(blob, aad=_video_aad(row["id"])), row["video_type"] or "video/mp4"


async def decide_verification(pool: asyncpg.Pool, bus: Any, verification_id: int, *, approve: bool, actor: str,
                              reason: str) -> dict[str, Any]:
    reason = reason.strip()
    if not approve and not reason:
        raise ReviewError("Give a reason: the client sees it.")
    async with pool.acquire() as con, con.transaction():
        row = await con.fetchrow(
            "UPDATE verifications SET status = $2, reviewed_by = $3, reviewed_at = now(), review_reason = $4 "
            "WHERE id = $1 AND status = 'submitted' RETURNING *",
            verification_id, APPROVED if approve else REJECTED, actor, reason,
        )
        if row is None:
            raise ReviewError("Only a submitted video can be approved or rejected.", 409)
        await _audit_owner(con, row["owner_id"], actor,
                           EVENT_VERIFICATION_APPROVED if approve else EVENT_VERIFICATION_REJECTED,
                           reason or ("verified" if approve else "rejected"), {"verification_id": row["id"]})
    await _settle_alert(pool, actor)
    await sync_holds(pool, bus)
    return dict(row)


async def request_verification(pool: asyncpg.Pool, bus: Any, owner_id: int, *, actor: str,
                               reason: str) -> dict[str, Any]:
    """The admin suspects something: the login must verify again, and its
    businesses are held until the new video is approved."""
    reason = reason.strip()
    if not reason:
        raise ReviewError("Give a reason: the client sees it.")
    async with pool.acquire() as con, con.transaction():
        if await con.fetchval("SELECT id FROM owners WHERE id = $1", owner_id) is None:
            raise ReviewError("Unknown client login", 404)
        latest = await latest_verification(con, owner_id)
        if latest and latest["status"] in (REQUESTED, SUBMITTED):
            raise ReviewError("A verification is already open for this login.", 409)
        row = await con.fetchrow(
            "INSERT INTO verifications (owner_id, status, reason, requested_by) VALUES ($1, 'requested', $2, $3) "
            "RETURNING *",
            owner_id, reason, actor,
        )
        await _audit_owner(con, owner_id, actor, EVENT_VERIFICATION_REQUESTED, reason,
                           {"verification_id": row["id"]})
    await sync_holds(pool, bus)
    return dict(row)


async def list_verifications(pool: asyncpg.Pool, *, status: Optional[str] = None,
                             limit: int = 200) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        f"""
        SELECT v.*, o.username, o.display_name, o.company, o.email, o.phone,
               coalesce((SELECT json_agg(json_build_object('id', t.id, 'name', t.name) ORDER BY t.id)
                           FROM owner_tenants ot JOIN tenants t ON t.id = ot.tenant_id
                          WHERE ot.owner_id = o.id), '[]'::json) AS tenants
          FROM verifications v JOIN owners o ON o.id = v.owner_id
         {"WHERE v.status = $2" if status else ""}
         ORDER BY v.status <> 'submitted', v.id DESC LIMIT $1
        """,
        *([limit, status] if status else [limit]),
    )
    out = []
    for r in rows:
        linked = r["tenants"]
        out.append({
            **public_verification(dict(r), with_challenge=False),
            "owner_id": r["owner_id"], "username": r["username"], "display_name": r["display_name"],
            "company": r["company"], "email": r["email"], "phone": r["phone"],
            "tenants": json.loads(linked) if isinstance(linked, str) else (linked or []),
            # What the video must show, for the reviewer to compare.
            "challenge": r["challenge"], "gesture": r["gesture"], "challenge_at": _iso(r["challenge_at"]),
            "has_video": bool(r["video_file"]), "video_bytes": r["video_bytes"],
            "video_deleted_at": _iso(r["video_deleted_at"]),
        })
    return out


# ---------------------------------------------------------------- photos


def _submission(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"], "tenant_id": row["tenant_id"], "owner_id": row["owner_id"], "source": row["source"],
        "kind": row["kind"], "original_name": row["original_name"], "bytes": row["bytes"],
        "description": row["description"], "replaces_item": row["replaces_item"], "status": row["status"],
        "media_item": row["media_item"], "reviewed_by": row["reviewed_by"], "reviewed_at": _iso(row["reviewed_at"]),
        "review_reason": row["review_reason"], "created_at": _iso(row["created_at"]),
    }


async def _tenant(executor: Any, tenant_id: int) -> dict[str, Any]:
    row = await executor.fetchrow("SELECT id, name, session_id FROM tenants WHERE id = $1", tenant_id)
    if row is None:
        raise ReviewError("Unknown business", 404)
    return dict(row)


def library_for(data_root: Path, tenant: dict[str, Any]) -> media.MediaLibrary:
    return media.MediaLibrary(tenants.tenant_data_dir(Path(data_root), tenant["id"], tenant["session_id"]) / "media")


async def submit_photo(pool: asyncpg.Pool, data_root: Path, tenant_id: int, owner_id: int, data: bytes, *,
                       filename: str, description: str, replaces: Optional[int], actor: str) -> dict[str, Any]:
    description = description.strip()
    if len(description) > MAX_DESCRIPTION:
        raise ReviewError(f"The description is limited to {MAX_DESCRIPTION} characters.")
    ext = sniff_photo(data[:16])
    if ext is None:
        raise ReviewError("Upload a photo (jpg, png or webp).")
    tenant = await _tenant(pool, tenant_id)
    if replaces is not None and library_for(data_root, tenant).get(replaces) is None:
        raise ReviewError("The photo to replace no longer exists.", 404)
    name = f"{uuid.uuid4().hex}{ext}"
    folder = _photo_dir(data_root, tenant_id)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".{name}.part"
    tmp.write_bytes(data)
    os.replace(tmp, folder / name)
    async with pool.acquire() as con, con.transaction():
        row = await con.fetchrow(
            "INSERT INTO media_submissions (tenant_id, owner_id, source, file, original_name, kind, bytes, "
            "description, replaces_item) VALUES ($1, $2, 'owner', $3, $4, 'photo', $5, $6, $7) RETURNING *",
            tenant_id, owner_id, name, media.safe_filename(filename)[:200], len(data), description, replaces,
        )
        await audit.record(con, tenant_id=tenant_id, actor=actor, event=EVENT_MEDIA_SUBMITTED,
                           reason="replacement photo" if replaces else "new photo",
                           payload={"submission_id": row["id"], "replaces_item": replaces, "bytes": len(data)})
    await _announce(pool, f"photo for {tenant['name']}", {"submission_id": row["id"], "tenant_id": tenant_id})
    return _submission(row)


async def list_submissions(executor: Any, *, tenant_ids: Optional[list[int]] = None,
                           status: Optional[str] = None, limit: int = 300) -> list[dict[str, Any]]:
    where, args = [], []
    if tenant_ids is not None:
        args.append(tenant_ids)
        where.append(f"s.tenant_id = ANY(${len(args)})")
    if status:
        args.append(status)
        where.append(f"s.status = ${len(args)}")
    args.append(limit)
    rows = await executor.fetch(
        "SELECT s.*, t.name AS tenant_name, t.session_id, o.username FROM media_submissions s "
        "JOIN tenants t ON t.id = s.tenant_id LEFT JOIN owners o ON o.id = s.owner_id"
        + (" WHERE " + " AND ".join(where) if where else "")
        + f" ORDER BY s.status <> 'pending', s.id DESC LIMIT ${len(args)}",
        *args,
    )
    return [{**_submission(r), "tenant_name": r["tenant_name"], "session_id": r["session_id"],
             "username": r["username"]} for r in rows]


async def get_submission(executor: Any, submission_id: int) -> dict[str, Any]:
    row = await executor.fetchrow("SELECT * FROM media_submissions WHERE id = $1", submission_id)
    if row is None:
        raise ReviewError("Unknown submission", 404)
    return {**_submission(row), "file": row["file"]}


async def withdraw(pool: asyncpg.Pool, data_root: Path, submission_id: int, *, tenant_ids: list[int],
                   actor: str) -> dict[str, Any]:
    async with pool.acquire() as con, con.transaction():
        row = await con.fetchrow(
            "UPDATE media_submissions SET status = 'withdrawn', reviewed_by = $3, reviewed_at = now() "
            "WHERE id = $1 AND tenant_id = ANY($2) AND status = 'pending' AND source = 'owner' RETURNING *",
            submission_id, tenant_ids, actor,
        )
        if row is None:
            raise ReviewError("Unknown or already reviewed submission", 404)
        await audit.record(con, tenant_id=row["tenant_id"], actor=actor, event=EVENT_MEDIA_WITHDRAWN,
                           reason="withdrawn by the client", payload={"submission_id": submission_id})
    path = submission_path(data_root, {"tenant_id": row["tenant_id"], "file": row["file"]})
    if path:
        path.unlink(missing_ok=True)
    await _settle_alert(pool, actor)
    return _submission(row)


async def decide_submission(pool: asyncpg.Pool, data_root: Path, submission_id: int, *, approve: bool, actor: str,
                            reason: str = "", description: Optional[str] = None) -> dict[str, Any]:
    """Approving copies the file into the tenant's media library (and drops
    the photo it replaces); the bot can use it from its next reply."""
    reason = reason.strip()
    if not approve and not reason:
        raise ReviewError("Give a reason: the client sees it.")
    sub = await get_submission(pool, submission_id)
    if sub["status"] != PENDING:
        raise ReviewError("This submission was already decided.", 409)
    media_item = None
    if approve:
        source = submission_path(data_root, sub)
        if source is None:
            raise ReviewError("The submitted file is missing.", 404)
        tenant = await _tenant(pool, sub["tenant_id"])
        library = library_for(data_root, tenant)
        target_name = library.unique_name(f"photo-{sub['id']}{source.suffix}")
        library.dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, library.dir / target_name)
        final_description = (description if description is not None else sub["description"]).strip()
        item = library.add_file(target_name, final_description[:MAX_DESCRIPTION])
        media_item = item["id"]
        if sub["replaces_item"] is not None:
            library.remove(sub["replaces_item"])
    async with pool.acquire() as con, con.transaction():
        row = await con.fetchrow(
            "UPDATE media_submissions SET status = $2, reviewed_by = $3, reviewed_at = now(), review_reason = $4, "
            "media_item = $5 WHERE id = $1 AND status = 'pending' RETURNING *",
            submission_id, "approved" if approve else "rejected", actor, reason, media_item,
        )
        if row is None:
            raise ReviewError("This submission was already decided.", 409)
        await audit.record(con, tenant_id=row["tenant_id"], actor=actor,
                           event=EVENT_MEDIA_APPROVED if approve else EVENT_MEDIA_REJECTED,
                           reason=reason or ("approved" if approve else "rejected"),
                           payload={"submission_id": submission_id, "media_item": media_item,
                                    "replaced_item": row["replaces_item"] if approve else None})
    if approve:
        path = submission_path(data_root, sub)
        if path:
            path.unlink(missing_ok=True)
    await _settle_alert(pool, actor)
    return _submission(row)


async def remove_photo(pool: asyncpg.Pool, data_root: Path, tenant_id: int, item_id: int, *,
                       actor: str) -> None:
    """Taking a photo away needs no review."""
    tenant = await _tenant(pool, tenant_id)
    library = library_for(data_root, tenant)
    item = library.get(item_id)
    if item is None:
        raise ReviewError("Unknown photo", 404)
    library.remove(item_id)
    await audit.record(pool, tenant_id=tenant_id, actor=actor, event=EVENT_MEDIA_REMOVED,
                       reason=f"removed {media.label(item)}", payload={"media_item": item_id})


async def recheck_tenant(pool: asyncpg.Pool, data_root: Path, tenant_id: int, *, actor: str,
                         reason: str) -> int:
    """Every live file of the tenant goes back into review: out of the
    media library (the bot stops using it now) and into the queue."""
    reason = reason.strip()
    if not reason:
        raise ReviewError("Give a reason.")
    tenant = await _tenant(pool, tenant_id)
    library = library_for(data_root, tenant)
    folder = _photo_dir(data_root, tenant_id)
    folder.mkdir(parents=True, exist_ok=True)
    moved = 0
    for item in library.all():
        path = library.path(item["id"])
        if path is None:
            continue
        name = f"{uuid.uuid4().hex}{path.suffix.lower()}"
        size = path.stat().st_size
        os.replace(path, folder / name)
        library.remove(item["id"])
        async with pool.acquire() as con, con.transaction():
            row = await con.fetchrow(
                "INSERT INTO media_submissions (tenant_id, source, file, original_name, kind, bytes, description, "
                "status) VALUES ($1, 'recheck', $2, $3, $4, $5, $6, 'pending') RETURNING id",
                tenant_id, name, item["file"][:200], item["kind"], size, item["description"][:MAX_DESCRIPTION],
            )
            await audit.record(con, tenant_id=tenant_id, actor=actor, event=EVENT_MEDIA_RECHECK, reason=reason,
                               payload={"media_item": item["id"], "submission_id": row["id"]})
        moved += 1
    if moved:
        await _announce(pool, f"{moved} file(s) of {tenant['name']} sent back to review",
                        {"tenant_id": tenant_id})
    return moved


# ------------------------------------------------------------- industries


async def set_industry_review(pool: asyncpg.Pool, bus: Any, industry_id: int, requires_review: bool, *,
                              actor: str) -> dict[str, Any]:
    async with pool.acquire() as con, con.transaction():
        row = await con.fetchrow(
            "UPDATE industries SET requires_review = $2, updated_at = now() WHERE id = $1 "
            "RETURNING id, name, requires_review",
            industry_id, requires_review,
        )
        if row is None:
            raise ReviewError("Unknown industry", 404)
        await audit.record(con, tenant_id=None, actor=actor, event=EVENT_INDUSTRY_REVIEW,
                           reason=f"{row['name']}: review {'required' if requires_review else 'not required'}",
                           payload={"industry_id": industry_id, "requires_review": requires_review})
    await sync_holds(pool, bus)
    return dict(row)


# ----------------------------------------------------------------- holds


_SHOULD_HOLD = """
WITH owner_state AS (
  SELECT o.id AS owner_id,
         (SELECT status FROM verifications v WHERE v.owner_id = o.id ORDER BY v.id DESC LIMIT 1) AS status
    FROM owners o
)
SELECT t.id, t.session_id, i.requires_review,
       EXISTS (SELECT 1 FROM owner_tenants ot JOIN owner_state s ON s.owner_id = ot.owner_id
                WHERE ot.tenant_id = t.id AND s.status = 'approved') AS verified,
       (SELECT string_agg(v.reason, '; ') FROM owner_tenants ot
          JOIN owner_state s ON s.owner_id = ot.owner_id
          JOIN LATERAL (SELECT reason FROM verifications WHERE owner_id = ot.owner_id ORDER BY id DESC LIMIT 1) v
            ON true
         WHERE ot.tenant_id = t.id AND s.status IS NOT NULL AND s.status <> 'approved') AS open_reason,
       EXISTS (SELECT 1 FROM tenant_holds h WHERE h.tenant_id = t.id AND h.kind = 'verification') AS held
  FROM tenants t JOIN industries i ON i.id = t.industry_id
"""


async def sync_holds(pool: asyncpg.Pool, bus: Any = None) -> list[str]:
    """Put the 'verification' hold on every tenant that needs it and take it
    off every other one. Returns the accounts whose switches changed (and
    tells them). Run after each decision and on every scheduler tick."""
    changed: list[str] = []
    for row in await pool.fetch(_SHOULD_HOLD):
        reason = None
        if row["open_reason"]:
            reason = f"identity verification asked for: {row['open_reason']}"[:500]
        elif row["requires_review"] and not row["verified"]:
            reason = "waiting for an approved identity verification video"
        if reason and not row["held"]:
            await controls.add_hold(pool, row["id"], controls.VERIFICATION, reason, actor=SYSTEM)
        elif not reason and row["held"]:
            await controls.remove_hold(pool, row["id"], controls.VERIFICATION, actor=SYSTEM,
                                       reason="identity verification approved or no longer required")
        else:
            continue
        if row["session_id"]:
            changed.append(row["session_id"])
    if changed and bus is not None:
        await controls.reload_controls(pool, bus, changed)
    return changed


# ---------------------------------------------------------------- retention


async def purge(pool: asyncpg.Pool, data_root: Path, *, days: int = VIDEO_RETENTION_DAYS) -> int:
    """Delete verification videos, and files of rejected or withdrawn
    photos, `days` after the decision."""
    cutoff = _now() - timedelta(days=days)
    removed = 0
    for row in await pool.fetch(
        "SELECT id, video_file FROM verifications WHERE video_file IS NOT NULL AND reviewed_at < $1", cutoff,
    ):
        _video_path(data_root, row["video_file"]).unlink(missing_ok=True)
        await pool.execute("UPDATE verifications SET video_file = NULL, video_deleted_at = now() WHERE id = $1",
                           row["id"])
        removed += 1
    for row in await pool.fetch(
        "SELECT tenant_id, file FROM media_submissions WHERE status IN ('rejected', 'withdrawn') "
        "AND reviewed_at < $1", cutoff,
    ):
        path = submission_path(data_root, dict(row))
        if path:
            path.unlink()
            removed += 1
    return removed


async def tick(pool: asyncpg.Pool, bus: Any, data_root: Path) -> None:
    """The scheduler's round: keep the holds right, then the retention."""
    await sync_holds(pool, bus)
    await purge(pool, data_root)
