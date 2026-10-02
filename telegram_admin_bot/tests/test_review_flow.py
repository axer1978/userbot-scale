"""Identity verification by video and the review of client photos
(review.py, owner_review_api.py, review_admin_api.py, migration 0008).

The points: a business in an industry that requires review is paused until
its client's video is approved; a video is only accepted for a fresh code
and is never stored in plain; a photo a client submits never reaches the
bot's media library before the admin approves it; the admin can pull a
login or a whole library back into review.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest

import controls
import media
import owner_auth
import review
from conftest import seed_session

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

TEMP = "temporary-pass-1"
MINE = "my-own-password-2"
VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2000
JPEG = b"\xff\xd8\xff\xe0" + b"\x01" * 3000
PNG = b"\x89PNG\r\n\x1a\n" + b"\x02" * 3000


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    import owner_api

    monkeypatch.setattr(owner_auth, "_failures", {})
    monkeypatch.setattr(owner_auth, "_last_totp_step", {})
    monkeypatch.setattr(owner_api, "_pending_totp", {})


def client() -> httpx.AsyncClient:
    import panel

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=panel.app, client=("10.0.0.5", 1)),
                             base_url="http://test")


@asynccontextmanager
async def owner_for(panel_client, tenant_ids, username="anna"):
    """A signed-in client past the password change."""
    r = await panel_client.post("/api/owners", json={"username": username, "password": TEMP,
                                                     "tenant_ids": list(tenant_ids)})
    assert r.status_code == 200, r.text
    async with client() as c:
        await c.post("/api/owner/login", json={"username": username, "password": TEMP})
        assert (await c.post("/api/owner/password", json={"current": TEMP, "new": MINE})).status_code == 200
        yield c


async def held(pg_pool, tenant_id) -> bool:
    return any(h["kind"] == controls.VERIFICATION for h in await controls.holds(pg_pool, tenant_id))


async def mark_escort(panel_client, industry_id=1):
    r = await panel_client.put(f"/api/review/industries/{industry_id}", json={"requires_review": True})
    assert r.status_code == 200 and r.json()["requires_review"], r.text


async def verify(c: httpx.AsyncClient, panel_client) -> int:
    """Get a code, upload a video, have the admin approve it."""
    r = await c.post("/api/owner/verification/challenge")
    assert r.status_code == 200, r.text
    r = await c.put("/api/owner/verification/video?name=me.mp4", content=VIDEO,
                    headers={"Content-Type": "video/mp4"})
    assert r.status_code == 200 and r.json()["status"] == "submitted", r.text
    vid = r.json()["id"]
    assert (await panel_client.post(f"/api/review/verifications/{vid}/approve", json={})).status_code == 200
    return vid


# ------------------------------------------------------------ verification


async def test_an_escort_business_is_paused_until_its_client_is_verified(panel_client, pg_pool, tmp_path):
    tenant = await seed_session(pg_pool, "acct_a")
    async with owner_for(panel_client, [tenant]) as c:
        assert (await c.get("/api/owner/me")).status_code == 200
        await mark_escort(panel_client)
        assert await held(pg_pool, tenant)
        r = await c.get("/api/owner/me")
        assert r.status_code == 403 and r.json()["detail"] == owner_auth.VERIFY_IDENTITY
        state = (await c.get("/api/owner/verification")).json()
        assert state["required"] and state["latest"] is None and state["instructions"]

        # A video without a code is refused; a code is shown once asked for.
        r = await c.put("/api/owner/verification/video?name=me.mp4", content=VIDEO,
                        headers={"Content-Type": "video/mp4"})
        assert r.status_code == 409
        challenge = (await c.post("/api/owner/verification/challenge")).json()["challenge"]
        assert len(challenge["code"]) == 7 and challenge["gesture"] in review.GESTURES
        bad = await c.put("/api/owner/verification/video?name=me.mp4", content=b"<html>hi</html>",
                          headers={"Content-Type": "video/mp4"})
        assert bad.status_code == 400
        r = await c.put("/api/owner/verification/video?name=me.mp4", content=VIDEO,
                        headers={"Content-Type": "video/mp4"})
        assert r.status_code == 200
        vid = r.json()["id"]
        # Waiting for review: still locked, still paused, no second upload.
        assert (await c.get("/api/owner/me")).status_code == 403
        assert (await c.post("/api/owner/verification/challenge")).status_code == 409

        # Stored encrypted; only the admin gets it back.
        [stored] = list((tmp_path / "review" / "verifications").iterdir())
        assert VIDEO not in stored.read_bytes()
        assert (await c.get(f"/api/review/verifications/{vid}/video")).status_code == 401
        r = await panel_client.get(f"/api/review/verifications/{vid}/video")
        assert r.status_code == 200 and r.content == VIDEO
        [listed] = (await panel_client.get("/api/review/verifications", params={"status": "submitted"})).json()
        assert listed["challenge"] == challenge["code"] and listed["username"] == "anna"

        assert (await panel_client.post(f"/api/review/verifications/{vid}/approve", json={})).status_code == 200
        assert not await held(pg_pool, tenant)
        assert (await c.get("/api/owner/me")).status_code == 200
    events = {r["event"] for r in await pg_pool.fetch("SELECT event FROM audit_log")}
    assert {"verification_submitted", "verification_approved", "industry_review_changed"} <= events


async def test_an_expired_code_and_a_rejection(panel_client, pg_pool):
    tenant = await seed_session(pg_pool, "acct_a")
    await mark_escort(panel_client)
    async with owner_for(panel_client, [tenant]) as c:
        await c.post("/api/owner/verification/challenge")
        await pg_pool.execute("UPDATE verifications SET challenge_at = challenge_at - $1::interval",
                              timedelta(minutes=31))
        assert (await c.get("/api/owner/verification")).json()["latest"].get("challenge") is None
        r = await c.put("/api/owner/verification/video?name=me.mp4", content=VIDEO,
                        headers={"Content-Type": "video/mp4"})
        assert r.status_code == 409 and "expired" in r.json()["detail"]

        await c.post("/api/owner/verification/challenge")
        vid = (await c.put("/api/owner/verification/video?name=me.mov", content=VIDEO,
                           headers={"Content-Type": "video/quicktime"})).json()["id"]
        assert (await panel_client.post(f"/api/review/verifications/{vid}/reject", json={})).status_code == 400
        r = await panel_client.post(f"/api/review/verifications/{vid}/reject", json={"reason": "code not readable"})
        assert r.status_code == 200
        state = (await c.get("/api/owner/verification")).json()
        assert state["latest"]["status"] == "rejected" and state["latest"]["review_reason"] == "code not readable"
        assert await held(pg_pool, tenant)
        # A new round starts with a new code.
        assert (await c.post("/api/owner/verification/challenge")).status_code == 200


async def test_the_admin_can_ask_any_client_to_verify_again(panel_client, pg_pool):
    tenant = await seed_session(pg_pool, "acct_a")  # not an escort business
    async with owner_for(panel_client, [tenant]) as c:
        owner_id = await pg_pool.fetchval("SELECT id FROM owners")
        r = await panel_client.post(f"/api/owners/{owner_id}/request-verification", json={"reason": ""})
        assert r.status_code == 400
        r = await panel_client.post(f"/api/owners/{owner_id}/request-verification",
                                    json={"reason": "photos look like someone else"})
        assert r.status_code == 200
        assert (await panel_client.post(f"/api/owners/{owner_id}/request-verification",
                                        json={"reason": "again"})).status_code == 409
        assert await held(pg_pool, tenant)
        assert (await c.get("/api/owner/me")).json()["detail"] == owner_auth.VERIFY_IDENTITY
        assert (await c.get("/api/owner/verification")).json()["latest"]["reason"] == "photos look like someone else"
        # The hold can't be lifted by hand; only an approved video lifts it.
        r = await panel_client.post(f"/api/tenants/{tenant}/resume", json={"kind": "verification"})
        assert r.status_code == 400
        await verify(c, panel_client)
        assert not await held(pg_pool, tenant)
        assert (await c.get("/api/owner/me")).status_code == 200


# ------------------------------------------------------------------ photos


async def test_a_photo_reaches_the_bot_only_once_approved(panel_client, pg_pool, tmp_path):
    tenant = await seed_session(pg_pool, "acct_a")
    await mark_escort(panel_client)
    async with owner_for(panel_client, [tenant]) as c:
        await verify(c, panel_client)
        path = f"/api/owner/tenants/{tenant}/photos"
        assert (await c.put(path + "?name=x.jpg", content=b"<script>", headers={"Content-Type": "image/jpeg"})
                ).status_code == 400
        r = await c.put(path + "?name=me.jpg&description=on the sofa", content=JPEG,
                        headers={"Content-Type": "image/jpeg"})
        assert r.status_code == 200 and r.json()["status"] == "pending"
        sub = r.json()["id"]
        library = review.library_for(tmp_path, {"id": tenant, "session_id": "acct_a"})
        assert library.all() == []  # nothing the bot can send yet
        assert (await c.get(f"/api/owner/tenants/{tenant}/submissions/{sub}/file")).content == JPEG
        assert (await panel_client.get("/api/review/summary")).json()["pending"] == {"verifications": 0,
                                                                                     "photos": 1}

        r = await panel_client.post(f"/api/review/photos/{sub}/approve", json={"description": "sofa, evening"})
        assert r.status_code == 200 and r.json()["media_item"] is not None
        library.refresh()
        [item] = library.all()
        assert item["description"] == "sofa, evening" and library.path(item["id"]).read_bytes() == JPEG
        assert (await c.get(f"{path}/{item['id']}/file")).content == JPEG

        # A replacement swaps the photo only when approved.
        r = await c.put(f"{path}?name=new.png&replaces={item['id']}", content=PNG, headers={"Content-Type": "image/png"})
        new_sub = r.json()["id"]
        library.refresh()
        assert [i["id"] for i in library.all()] == [item["id"]]
        await panel_client.post(f"/api/review/photos/{new_sub}/approve", json={})
        library.refresh()
        [replaced] = library.all()
        assert replaced["id"] != item["id"] and library.path(replaced["id"]).read_bytes() == PNG

        # Rejected and withdrawn ones never arrive.
        rejected = (await c.put(path + "?name=a.jpg", content=JPEG, headers={"Content-Type": "image/jpeg"})).json()
        assert (await panel_client.post(f"/api/review/photos/{rejected['id']}/reject", json={})).status_code == 400
        await panel_client.post(f"/api/review/photos/{rejected['id']}/reject", json={"reason": "face of a minor?"})
        withdrawn = (await c.put(path + "?name=b.jpg", content=JPEG, headers={"Content-Type": "image/jpeg"})).json()
        assert (await c.delete(f"/api/owner/tenants/{tenant}/submissions/{withdrawn['id']}")).status_code == 200
        library.refresh()
        assert len(library.all()) == 1
        statuses = {s["id"]: s["status"] for s in (await c.get(path)).json()["submissions"]}
        assert statuses[rejected["id"]] == "rejected" and statuses[withdrawn["id"]] == "withdrawn"

        # Taking a photo away needs nobody.
        assert (await c.delete(f"{path}/{replaced['id']}")).status_code == 200
        library.refresh()
        assert library.all() == []


async def test_photos_are_only_for_review_businesses_and_their_own_client(panel_client, pg_pool):
    a = await seed_session(pg_pool, "acct_a")
    b = await seed_session(pg_pool, "acct_b")
    async with owner_for(panel_client, [a]) as c:
        assert (await c.get(f"/api/owner/tenants/{a}/photos")).status_code == 404  # not an escort business
        await mark_escort(panel_client)
        await verify(c, panel_client)
        assert (await c.get(f"/api/owner/tenants/{a}/photos")).status_code == 200
        assert (await c.get(f"/api/owner/tenants/{b}/photos")).status_code == 404


async def test_recheck_pulls_every_live_file_back_into_review(panel_client, pg_pool, tmp_path):
    tenant = await seed_session(pg_pool, "acct_a")
    library = review.library_for(tmp_path, {"id": tenant, "session_id": "acct_a"})
    (library.dir / "one.jpg").write_bytes(JPEG)
    library.add_file("one.jpg", "first")
    (library.dir / "two.jpg").write_bytes(JPEG)
    library.add_file("two.jpg", "second")
    assert (await panel_client.post(f"/api/tenants/{tenant}/recheck-media", json={"reason": ""})).status_code == 400
    r = await panel_client.post(f"/api/tenants/{tenant}/recheck-media", json={"reason": "reported"})
    assert r.json() == {"moved": 2}
    library.refresh()
    assert library.all() == []
    pending = (await panel_client.get("/api/review/photos")).json()
    assert sorted(p["description"] for p in pending) == ["first", "second"]
    assert all(p["source"] == "recheck" for p in pending)
    assert (await panel_client.get(f"/api/review/photos/{pending[0]['id']}/file")).content == JPEG


async def test_old_videos_and_rejected_files_are_deleted(panel_client, pg_pool, tmp_path):
    tenant = await seed_session(pg_pool, "acct_a")
    await mark_escort(panel_client)
    async with owner_for(panel_client, [tenant]) as c:
        vid = await verify(c, panel_client)
        sub = (await c.put(f"/api/owner/tenants/{tenant}/photos?name=a.jpg", content=JPEG,
                           headers={"Content-Type": "image/jpeg"})).json()["id"]
        await panel_client.post(f"/api/review/photos/{sub}/reject", json={"reason": "no"})
    assert await review.purge(pg_pool, tmp_path) == 0  # not old enough
    await pg_pool.execute("UPDATE verifications SET reviewed_at = reviewed_at - interval '31 days'")
    await pg_pool.execute("UPDATE media_submissions SET reviewed_at = reviewed_at - interval '31 days'")
    assert await review.purge(pg_pool, tmp_path) == 2
    assert not list((tmp_path / "review" / "verifications").iterdir())
    assert (await panel_client.get(f"/api/review/verifications/{vid}/video")).status_code == 404
    assert (await panel_client.get(f"/api/review/photos/{sub}/file")).status_code == 404


async def test_the_scheduler_round_keeps_holds_right(pg_pool, tmp_path):
    tenant = await seed_session(pg_pool, "acct_a")
    await pg_pool.execute("UPDATE industries SET requires_review = true")
    await review.tick(pg_pool, None, tmp_path)
    assert await held(pg_pool, tenant)
    await pg_pool.execute("UPDATE industries SET requires_review = false")
    await review.tick(pg_pool, None, tmp_path)
    assert not await held(pg_pool, tenant)


async def test_the_media_library_sees_another_process_s_changes(tmp_path):
    bot = media.MediaLibrary(tmp_path)  # the running account's copy
    panel = media.MediaLibrary(tmp_path)
    (tmp_path / "a.jpg").write_bytes(JPEG)
    added = panel.add_file("a.jpg", "the description the model needs")
    bot.refresh()
    assert bot.get(added["id"])["description"] == "the description the model needs"
    assert len(bot.all()) == 1
