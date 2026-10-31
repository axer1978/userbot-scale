"""booking.provider on Telegram, as typed in the panel: @username, numeric id
or phone. A phone that is not among the account's contacts must still be
found. Or a group the account is in: its invite link, its id, or
"group: <name>"."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telethon import utils as telethon_utils
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import (
    Channel,
    Chat,
    ChatInvite,
    ChatInviteAlready,
    ChatPhotoEmpty,
    InputPeerChannel,
    InputPeerChat,
    User,
)

import telegram_transport

OWNER = SimpleNamespace(id=4242)
OWNER_USER = User(id=4242, first_name="Owner")


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


# ------------------------------------------------- a group as the booker


def supergroup(channel_id=1234567890, title="Riga bookings", megagroup=True):
    return Channel(id=channel_id, title=title, photo=ChatPhotoEmpty(), date=None, megagroup=megagroup,
                   broadcast=not megagroup, access_hash=555)


BASIC = Chat(id=4321, title="Old group", photo=ChatPhotoEmpty(), participants_count=3, date=None, version=1)


class GroupClient(FakeClient):
    """Its chats are `dialogs`; `member_of` maps the invite hashes of the
    groups it is in to them; `entities` is what get_entity knows."""

    def __init__(self, dialogs=(), member_of=None, entities=None, **kw):
        super().__init__(**kw)
        self.dialogs = list(dialogs)
        self.member_of = member_of or {}
        self.entities = entities or {}

    async def get_entity(self, target):
        if isinstance(target, InputPeerChannel):
            self.asked.append(("input", target.channel_id))
            if ("channel", target.channel_id) in self.entities:
                return self.entities[("channel", target.channel_id)]
            raise ValueError("not found")
        if isinstance(target, (int, str)) and target in self.entities:
            self.asked.append(target)
            return self.entities[target]
        return await super().get_entity(target)

    async def __call__(self, request):
        if isinstance(request, CheckChatInviteRequest):
            self.asked.append(("invite", request.hash))
            if request.hash in self.member_of:
                return ChatInviteAlready(chat=self.member_of[request.hash])
            return ChatInvite(title="Riga bookings", photo=None, participants_count=4, color=0)
        return await super().__call__(request)

    async def iter_dialogs(self, limit=None):
        self.asked.append(("dialogs", limit))
        for entity in self.dialogs:
            yield SimpleNamespace(entity=entity, id=telethon_utils.get_peer_id(entity),
                                  name=getattr(entity, "title", None) or getattr(entity, "first_name", ""))


class FakeDb:
    def __init__(self, hashes=None):
        self.hashes = hashes or {}

    async def get_access_hash(self, chat_id):
        return self.hashes.get(chat_id)


def group_transport(client, hashes=None):
    t = transport(client)
    t.rt = SimpleNamespace(db=FakeDb(hashes))
    t.state = {"connected": True}
    return t


def scanned(client) -> bool:
    return any(isinstance(a, tuple) and a[0] == "dialogs" for a in client.asked)


@pytest.mark.asyncio
@pytest.mark.parametrize("link", ["https://t.me/+AbCdEf123", "t.me/joinchat/AbCdEf123",
                                  "https://telegram.me/+AbCdEf123"])
async def test_a_group_by_its_invite_link_when_the_account_is_in_it(link):
    t = group_transport(GroupClient(member_of={"AbCdEf123": supergroup()}))
    chat_id, peer = await t.resolve_owner(link)
    assert chat_id == -1001234567890          # as Telethon's events give it
    assert peer.name == "Riga bookings" and peer.access_hash == 555


@pytest.mark.asyncio
async def test_an_invite_link_of_a_group_the_account_is_not_in_is_refused():
    with pytest.raises(ValueError, match="not in that group"):
        await group_transport(GroupClient()).resolve_owner("https://t.me/+NotJoined1")


@pytest.mark.asyncio
async def test_a_group_by_its_name_needs_the_group_prefix():
    client = GroupClient(dialogs=[OWNER_USER, supergroup(), BASIC])
    chat_id, _ = await group_transport(client).resolve_owner("group:  riga BOOKINGS ")
    assert chat_id == -1001234567890
    # Without "group:" a name is never looked up among the groups: it could
    # be a stranger's @username.
    client = GroupClient(dialogs=[supergroup()])
    with pytest.raises(ValueError):
        await group_transport(client).resolve_owner("Riga bookings")
    assert not scanned(client)


@pytest.mark.asyncio
async def test_a_group_name_must_be_one_group_the_account_is_in():
    with pytest.raises(ValueError, match="in no group named"):
        await group_transport(GroupClient(dialogs=[supergroup()])).resolve_owner("group: Tallinn")
    twins = [supergroup(1, "Bookings"), supergroup(2, "bookings")]
    with pytest.raises(ValueError, match="2 groups"):
        await group_transport(GroupClient(dialogs=twins)).resolve_owner("group: Bookings")


@pytest.mark.asyncio
async def test_a_group_id_after_a_restart_comes_back_from_the_stored_access_hash():
    group = supergroup()
    client = GroupClient(entities={("channel", 1234567890): group})
    chat_id, _ = await group_transport(client, hashes={-1001234567890: 555}).resolve_owner("-1001234567890")
    assert chat_id == -1001234567890
    assert not scanned(client)
    # Nothing stored: the account's chats are looked through.
    client = GroupClient(dialogs=[group])
    chat_id, _ = await group_transport(client).resolve_owner("-1001234567890")
    assert chat_id == -1001234567890 and scanned(client)
    # A basic group's id is negative without the -100.
    chat_id, peer = await group_transport(GroupClient(dialogs=[BASIC])).resolve_owner("-4321")
    assert (chat_id, peer.name) == (-4321, "Old group")


@pytest.mark.asyncio
async def test_a_broadcast_channel_cannot_be_the_booker():
    channel = supergroup(megagroup=False)
    with pytest.raises(ValueError, match="channel"):
        await group_transport(GroupClient(member_of={"Chan1": channel})).resolve_owner("https://t.me/+Chan1")


@pytest.mark.asyncio
async def test_a_public_link_is_its_username_and_a_person_stays_a_person():
    client = GroupClient(entities={"@rigabookings": supergroup()})
    chat_id, _ = await group_transport(client).resolve_owner("https://t.me/rigabookings")
    assert chat_id == -1001234567890
    chat_id, peer = await group_transport(GroupClient(entities={"@owner": OWNER_USER})).resolve_owner("@owner")
    assert (chat_id, peer.name) == (4242, "Owner")


@pytest.mark.asyncio
async def test_the_group_list_has_groups_only():
    left = supergroup(9, "Left one")
    left.left = True
    client = GroupClient(dialogs=[OWNER_USER, supergroup(megagroup=False), left, BASIC, supergroup(7, "Agency")])
    groups = await group_transport(client).list_groups()
    assert [(g["id"], g["name"]) for g in groups] == [("-1000000000007", "Agency"), ("-4321", "Old group")]


@pytest.mark.asyncio
async def test_a_group_is_reached_after_a_restart_from_the_stored_access_hash():
    class Empty:
        async def get_input_entity(self, chat_id):
            raise ValueError("not cached")

    t = group_transport(Empty(), hashes={-1001234567890: 555})
    assert await t.resolve_peer(-1001234567890) == InputPeerChannel(1234567890, 555)
    assert await t.resolve_peer(-4321) == InputPeerChat(4321)
