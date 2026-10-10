"""booking.provider on Telegram, as typed in the panel: @username, numeric id
or phone. A phone that is not among the account's contacts must still be
found."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import telegram_transport

OWNER = SimpleNamespace(id=4242)


class FakeClient:
    def __init__(self, ids=(), contacts=(), by_phone=()):
        self.ids, self.contacts, self.by_phone = set(ids), set(contacts), set(by_phone)
        self.asked: list = []

    async def get_entity(self, target):
        self.asked.append(target)
        if isinstance(target, int) and target in self.ids:
            return OWNER
        if isinstance(target, str) and (target == "@owner" or target in self.contacts):
            return OWNER
        raise ValueError("not found")

    async def __call__(self, request):
        self.asked.append(("resolve", request.phone))
        if request.phone not in self.by_phone:
            raise ValueError("PHONE_NOT_OCCUPIED")
        return SimpleNamespace(peer=SimpleNamespace(user_id=OWNER.id), users=[OWNER])


def transport(client):
    t = telegram_transport.TelegramTransport.__new__(telegram_transport.TelegramTransport)
    t.client = client
    return t


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["+371 2000 0000", "37120000000", "+1 (555) 010-9999"])
async def test_a_phone_outside_the_contacts_is_resolved(value):
    digits = "".join(c for c in value if c.isdigit())
    client = FakeClient(by_phone={digits})
    assert await transport(client)._owner_entity(value) is OWNER
    assert ("resolve", digits) in client.asked


@pytest.mark.asyncio
async def test_a_phone_in_the_contacts_needs_no_lookup():
    client = FakeClient(contacts={"+37120000000"})
    assert await transport(client)._owner_entity("+371 20000000") is OWNER
    assert not any(isinstance(a, tuple) for a in client.asked)


@pytest.mark.asyncio
async def test_usernames_and_ids_resolve_as_before():
    assert await transport(FakeClient())._owner_entity("@owner") is OWNER
    client = FakeClient(ids={7123456789})
    assert await transport(client)._owner_entity("7123456789") is OWNER
    assert client.asked == [7123456789]


@pytest.mark.asyncio
async def test_an_unknown_phone_fails_loudly():
    with pytest.raises(ValueError):
        await transport(FakeClient())._owner_entity("+37129999999")
