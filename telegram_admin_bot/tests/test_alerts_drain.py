"""alerts.drain() waits only for this event loop's deliveries.

A delivery started on a loop that has since closed (a finished test's) can
never report itself done; drain() used to wait on it forever, which hung
the suite whenever a delivery was still running when its test ended.
"""

from __future__ import annotations

import asyncio

import alerts


async def _forever() -> None:
    await asyncio.Event().wait()


def test_drain_forgets_deliveries_of_a_closed_loop():
    old = asyncio.new_event_loop()
    stranded = old.create_task(_forever())
    alerts._pending.add(stranded)
    old.close()                     # the task can never finish now
    try:
        asyncio.run(asyncio.wait_for(alerts.drain(), timeout=5))
        assert stranded not in alerts._pending
    finally:
        alerts._pending.discard(stranded)


def test_drain_still_waits_for_its_own_deliveries():
    done = []

    async def delivery():
        await asyncio.sleep(0.01)
        done.append(1)

    async def main():
        alerts._background(delivery())
        await alerts.drain()

    asyncio.run(main())
    assert done == [1] and not alerts._pending
