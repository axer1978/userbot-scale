"""Config writes that stop the account.

`halt_everything` is the emergency stop: every safety guard in
test_safety.py monkeypatches it out and only checks that it was *called*.
This file checks what it actually does against real Postgres.

Follow-up, not lost: the old version of this file also covered
`api_put_config` and two `api_global_pause` cases. Those are panel.py HTTP
routes (config_store.save() straight against Postgres), not SessionRuntime,
and there is no fixture for them yet. They need a panel.py test client —
httpx.AsyncClient over the FastAPI app with panel.pool / panel.registry /
panel.bus wired to the test's `pg_pool` — before they can come back.
"""

from __future__ import annotations

import pytest

import config_store
from database import OUT_CANCELLED, OUT_QUEUED


@pytest.mark.asyncio
async def test_halt_everything_pauses_persists_and_announces(app, db, pg_pool):
    queued = await db.queue_outreach([(101, "Ann"), (102, "Bob")], "say hi")
    assert {row["status"] for row in queued} == {OUT_QUEUED}
    assert app.config["behavior"]["global_pause"] is False

    await app.halt_everything("PeerFloodError on send")

    # Paused in memory and in Postgres, so a restart stays halted.
    assert app.config["behavior"]["global_pause"] is True
    stored = await config_store.load(pg_pool, app.session_id)
    assert stored["behavior"]["global_pause"] is True

    # Nothing queued goes out after a halt.
    assert {row["status"] for row in await db.list_outreach()} == {OUT_CANCELLED}

    # Every open panel tab hears about it, with the reason.
    assert app.hub.types() == ["config", "halted", "error"]
    halted = next(e for e in app.hub.events if e["type"] == "halted")
    assert halted["reason"] == "PeerFloodError on send"

    # The operator can see why after the fact.
    assert "PeerFloodError on send" in (app.data_dir / "last_halt.txt").read_text(encoding="utf-8")
    async with pg_pool.acquire() as con:
        state, reason = await con.fetchrow(
            "SELECT state, state_reason FROM telegram_sessions WHERE session_id = $1",
            app.session_id,
        )
    assert (state, reason) == ("halted", "PeerFloodError on send")
