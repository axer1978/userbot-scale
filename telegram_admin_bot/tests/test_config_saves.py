"""The writes that stop the account.

`halt_everything` is the emergency stop after Telegram pushes back: every
safety guard in test_safety.py monkeypatches it out and only checks that it
was *called*. This file checks what it actually does against real Postgres:
a 'telegram' soft-off hold (controls.py), which survives a restart.
"""

from __future__ import annotations

import pytest

import alerts
import audit
import controls
from database import OUT_CANCELLED, OUT_QUEUED


@pytest.mark.asyncio
async def test_halt_everything_holds_persists_and_announces(app, db, pg_pool):
    queued = await db.queue_outreach([(101, "Ann"), (102, "Bob")], "say hi")
    assert {row["status"] for row in queued} == {OUT_QUEUED}
    assert not app.paused()

    await app.halt_everything("PeerFloodError on send")

    # Soft-off in memory and in Postgres, so a restart stays halted.
    assert app.paused() and "PeerFloodError on send" in app.off_reason
    [hold] = await controls.holds(pg_pool, app.tenant_id)
    assert (hold["kind"], hold["reason"]) == (controls.TELEGRAM, "PeerFloodError on send")
    assert "PeerFloodError" in await controls.off_reason(pg_pool, app.tenant_id)

    # Nothing queued goes out after a halt.
    assert {row["status"] for row in await db.list_outreach()} == {OUT_CANCELLED}

    # Every open panel tab hears about it, with the reason.
    assert "halted" in app.hub.types() and "controls" in app.hub.types()
    halted = next(e for e in app.hub.events if e["type"] == "halted")
    assert halted["reason"] == "PeerFloodError on send"

    # The operator is alerted, and it is audited.
    [alert] = await alerts.list_alerts(pg_pool, open_only=True)
    assert alert["kind"] == "telegram" and alert["severity"] == alerts.CRITICAL
    events = [r["event"] for r in await audit.list_events(pg_pool, tenant_id=app.tenant_id)]
    assert audit.ACCOUNT_HALTED in events and audit.TENANT_SOFT_OFF in events

    # A second halt adds nothing new and does not alert twice.
    await app.halt_everything("PeerFloodError again")
    assert len(await controls.holds(pg_pool, app.tenant_id)) == 1
    [alert] = await alerts.list_alerts(pg_pool, open_only=True)
    assert alert["count"] == 2
    await alerts.drain()
