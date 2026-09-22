"""Running many conversations at once without them interfering."""

from __future__ import annotations

import asyncio

import pytest


# ------------------------------------------------------------- the AI gate


@pytest.mark.asyncio
async def test_gate_caps_calls_in_flight(app):
    """Without this, a busy hour fires every chat's API call simultaneously."""
    app.config["ai"]["max_concurrent_requests"] = 3
    live = peak = 0

    async def call():
        nonlocal live, peak
        async with app.ai_gate():
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1

    await asyncio.gather(*(call() for _ in range(20)))
    assert peak == 3, f"cap of 3 exceeded: {peak} calls ran at once"


@pytest.mark.asyncio
async def test_gate_is_stable_between_calls(app):
    app.config["ai"]["max_concurrent_requests"] = 4
    assert app.ai_gate() is app.ai_gate()


@pytest.mark.asyncio
async def test_gate_is_rebuilt_when_the_setting_changes(app):
    app.config["ai"]["max_concurrent_requests"] = 2
    first = app.ai_gate()
    app.config["ai"]["max_concurrent_requests"] = 6
    assert app.ai_gate() is not first


@pytest.mark.asyncio
async def test_every_chat_eventually_gets_through(app):
    """A cap must queue work, never drop it."""
    app.config["ai"]["max_concurrent_requests"] = 2
    done = []

    async def call(n):
        async with app.ai_gate():
            done.append(n)

    await asyncio.gather(*(call(i) for i in range(15)))
    assert sorted(done) == list(range(15))


# --------------------------------------------------------------- presence


@pytest.mark.asyncio
async def test_one_chat_finishing_does_not_take_the_account_offline(app, monkeypatch):
    """Presence is one switch for the whole account. Whichever conversation
    happens to finish first must not flip it off under the others."""
    states = []

    async def fake_presence(online):
        app.presence_online = online
        states.append(online)

    monkeypatch.setattr(app, "set_presence", fake_presence)
    for k in ("go_online_delay_min", "go_online_delay_max",
              "offline_delay_min", "offline_delay_max"):
        app.config["presence"][k] = 0

    await app.go_online_for(101)
    await app.go_online_for(202)
    assert app.presence_online

    app.schedule_go_offline(101)          # first chat done
    await asyncio.sleep(0.05)
    assert app.presence_online, "went offline while another chat was still replying"

    app.schedule_go_offline(202)          # last chat done
    await asyncio.sleep(0.05)
    assert not app.presence_online
    assert not app.active_chats


@pytest.mark.asyncio
async def test_no_second_unlock_pause_when_already_online(app, monkeypatch):
    states = []

    async def fake_presence(online):
        app.presence_online = online
        states.append(online)

    monkeypatch.setattr(app, "set_presence", fake_presence)
    for k in ("go_online_delay_min", "go_online_delay_max"):
        app.config["presence"][k] = 0

    await app.go_online_for(1)
    await app.go_online_for(2)
    assert states == [True], "came online twice for two concurrent chats"


# ------------------------------------------------------- draft cancellation


@pytest.mark.asyncio
async def test_a_new_message_cancels_a_pending_draft(app):
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(30)

    task = asyncio.create_task(slow())
    app.draft_tasks[1] = task
    await started.wait()

    app.cancel_draft(1)
    await asyncio.sleep(0)
    assert task.cancelled() or task.cancelling()


@pytest.mark.asyncio
async def test_a_draft_that_is_already_sending_is_not_cancelled(app):
    """Tearing down mid-send leaves a message on Telegram with no local row."""
    async def sending():
        await asyncio.sleep(0.2)
        return "sent"

    task = asyncio.create_task(sending())
    app.draft_tasks[1] = task
    app.sending_chats.add(1)

    app.cancel_draft(1)
    assert await task == "sent", "an in-flight send was cancelled"
