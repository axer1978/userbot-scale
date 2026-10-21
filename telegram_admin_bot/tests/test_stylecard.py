"""How the business types (stylecard.py): the numbers from synthetic typed
replies, the openers used in several chats only, what counts as typed by
hand, the note's limits, and when there is no note at all."""

from __future__ import annotations

import pytest

import audit
import stylecard
import tenant_config
from conftest import seed_session


def rows(chat, *messages, start=1):
    """A chat's typed replies as the query returns them. Each message is
    its text, or (text, True) when a client (or bot) message came right
    before it, which starts a new reply turn."""
    out, last = [], None
    for n, message in enumerate(messages, start=start):
        text, new_turn = (message, False) if isinstance(message, str) else message
        msg_id = chat * 1000 + n
        out.append({"chat": chat, "id": msg_id, "text": text, "prev_id": msg_id - 500 if new_turn or last is None
                    else last})
        last = msg_id
    return out


def test_the_numbers_from_typed_replies():
    typed = (
        rows(1, "hi love 😘", "what time you come?", ("ok 23:00 is fine 😘", True), "text me before.")
        + rows(2, "Hey love", ("[non-text message]", True), "here", ("[block]\n30 min 100\n1h 150\n[/block]", True))
        + rows(3, ("hii", True))
    )
    stats = stylecard.measure(typed)
    # The photo and the rates block say nothing about typing: left out, but the photo ends no turn.
    assert stats["messages"] == 7 and stats["chats"] == 3
    # Turns: [hi love, what time] [ok 23:00, text me] [Hey love] [here] [hii]. "here" is in the turn the
    # photo opened after the client wrote; the block's turn has nothing left in it.
    assert stats["turns"] == 5
    assert stats["median_messages_per_turn"] == 1.0
    assert stats["median_words"] == 2.0  # 2, 4, 4, 3, 2, 1, 1
    assert stats["lower_start_share"] == round(6 / 7, 2)  # all but "Hey love"
    assert stats["no_end_punctuation_share"] == round(5 / 7, 2)  # "?" and "." end two of them
    assert stats["emoji_share"] == round(2 / 7, 2)
    assert stats["top_emojis"] == [{"emoji": "😘", "count": 2}]


def test_openers_count_only_when_used_in_three_chats():
    typed = []
    for chat in (1, 2, 3):
        typed += rows(chat, "hey love 😘", "free now")
    typed += rows(4, "hey Toms, yes", start=1)  # a name, in one chat only
    typed += rows(5, "hey Toms", start=1) + rows(6, "yes love", start=1) + rows(7, "Yes love!", start=1)
    typed += rows(8, "200 for an hour", start=1)  # a price never opens anything
    stats = stylecard.measure(typed)
    # "hey love" in three chats; "hey" alone in five (with "hey toms"), so it is named too;
    # "hey toms" in two and "yes love" in two are not.
    assert stats["openers"] == [{"text": "hey", "chats": 5}, {"text": "hey love", "chats": 3}]
    assert "toms" not in str(stats["openers"])


def test_a_word_only_used_in_one_opener_is_not_named_twice():
    typed = []
    for chat in (1, 2, 3, 4):
        typed += rows(chat, "hii babe ❤️")
    assert [o["text"] for o in stylecard.measure(typed)["openers"]] == ["hii babe"]


def test_the_note_names_the_habits_and_stays_short():
    typed = []
    for chat in range(1, 8):
        typed += rows(chat, "hey love 😘", "what time you come", ("ok 🥰", True))
    note = stylecard.render(stylecard.measure(typed))
    assert note.startswith("HOW SHE TYPES (measured from 21 of her own replies typed by hand; match these habits, "
                           "never copy whole lines):")
    assert "- about 2 words a message, 1.5 message(s) in a row per reply" in note  # turns of 2 and of 1
    assert "starts with a small letter in 100% of messages; no . ! or ? at the end in 100%" in note
    assert "an emoji in 67% of messages, most often 😘 🥰" in note
    assert 'opens replies in several chats with (vary them): "hey love", "ok"' in note
    long = {**stylecard.measure(typed), "openers": [{"text": "x" * 120, "chats": 9}] * 5}
    assert len(stylecard.render(long)) <= stylecard.MAX_NOTE_CHARS
    assert "- no emojis" in stylecard.render({**stylecard.measure(typed), "emoji_share": 0.0, "top_emojis": []})


def test_what_counts_as_typing():
    assert stylecard.is_typing("hey love 😘")
    for not_typing in ("", "   ", "[non-text message]", "[sent photo #3: red dress]", "[photo] a mirror selfie",
                       "[deleted from the chat: arrival instructions]", "[block]rates[/block]",
                       "line\nline\nline\nline", "x" * (stylecard.BLOCK_CHARS + 1)):
        assert not stylecard.is_typing(not_typing), not_typing


# ---------------------------------------------------------------- database


async def message(pool, session, chat, direction, text, *, model=None, status=None):
    status = status or ("received" if direction == "in" else "sent")
    return await pool.fetchval(
        "INSERT INTO messages (session_id, chat_id, direction, status, text, llm_model) "
        "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id", session, chat, direction, status, text, model)


async def sent_by(pool, tenant, message_id, actor):
    await audit.record(pool, tenant_id=tenant, actor=actor, event=audit.MESSAGE_SENT,
                       payload={"message_id": message_id, "kind": "text"})


def config(**card):
    return tenant_config.resolve(None, {"style_card": {"enabled": True, "min_replies": 5, **card}}).as_dict()


async def seed_typing(pool, session, tenant, chats=range(1, 4)):
    """Per chat: the client, a bot reply, the way in the system sent, then
    two replies typed on the phone and one sent by hand from the panel."""
    for chat in chats:
        await message(pool, session, chat, "in", "hi, free now?")
        await message(pool, session, chat, "out", "Hi my love ❤️ around what hour you come? 😘",
                      model="deepseek-chat")
        way_in = await message(pool, session, chat, "out", "Door code 1234, floor 4.")
        await sent_by(pool, tenant, way_in, audit.BOT)
        await message(pool, session, chat, "in", "ok")
        await message(pool, session, chat, "out", "hey love 😘")
        await message(pool, session, chat, "out", "come now")
        from_panel = await message(pool, session, chat, "out", "see you soon")
        await sent_by(pool, tenant, from_panel, audit.ADMIN)
        await message(pool, session, chat, "out", "a draft never sent", model="deepseek-chat",
                      status="pending_approval")


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_only_replies_typed_by_hand_are_measured(pg_pool):
    pam = await seed_session(pg_pool, "pam", name="Pam")
    await seed_typing(pg_pool, "pam", pam)

    stats = await stylecard.stats_for(pg_pool, pam, config()["style_card"])

    # Per chat: one turn of three typed messages, after the way in the system sent.
    assert (stats["messages"], stats["turns"], stats["chats"]) == (9, 3, 3)
    assert stats["median_messages_per_turn"] == 3.0
    assert stats["openers"] == [{"text": "hey love", "chats": 3}]
    note = await stylecard.note_for(pg_pool, pam, config())
    assert note.startswith("HOW SHE TYPES (measured from 9 of her own replies")
    assert "1234" not in note and "around what hour" not in note


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_no_note_when_off_or_with_too_few_replies(pg_pool):
    pam = await seed_session(pg_pool, "pam", name="Pam")
    await seed_typing(pg_pool, "pam", pam)
    assert await stylecard.note_for(pg_pool, pam, config(enabled=False)) == ""
    assert await stylecard.note_for(pg_pool, pam, config(min_replies=10)) == ""
    assert await stylecard.note_for(pg_pool, pam, config(min_replies=9)) != ""
    # Only the last `days` count.
    await pg_pool.execute("UPDATE messages SET created_at = now() - interval '30 days'")
    assert await stylecard.note_for(pg_pool, pam, config(days=29)) == ""
    assert await stylecard.note_for(pg_pool, pam, config(days=31)) != ""


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_the_industry_shares_one_voice_unless_the_account_has_its_own(pg_pool):
    pam = await seed_session(pg_pool, "pam", name="Pam")
    leaui = await seed_session(pg_pool, "leaui", name="Leaui")
    await seed_typing(pg_pool, "pam", pam, chats=(1, 2))
    await seed_typing(pg_pool, "leaui", leaui, chats=(1, 2))
    industry = await stylecard.stats_for(pg_pool, leaui, config(scope="industry")["style_card"])
    account = await stylecard.stats_for(pg_pool, leaui, config(scope="account")["style_card"])
    assert (industry["messages"], industry["chats"]) == (12, 4)
    assert industry["openers"] == [{"text": "hey love", "chats": 4}]  # the same chat id, two accounts: two chats
    assert (account["messages"], account["openers"]) == (6, [])


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_the_style_card_route_previews_even_when_off(panel_client, pg_pool):
    pam = await seed_session(pg_pool, "pam", name="Pam")
    await seed_typing(pg_pool, "pam", pam)

    off = (await panel_client.get(f"/api/finetune/style-card?tenant_id={pam}")).json()
    assert (off["tenant_id"], off["enabled"], off["scope"], off["days"], off["min_replies"]) == (
        pam, False, "industry", 90, 30)
    assert off["note"] == "" and off["stats"]["messages"] == 9  # below min_replies: no note, numbers all the same

    saved = await panel_client.put(f"/api/tenants/{pam}/config", json={
        "overrides": {"style_card": {"enabled": True, "min_replies": 5}}, "reason": "try it"})
    assert saved.status_code == 200, saved.text
    on = (await panel_client.get(f"/api/finetune/style-card?tenant_id={pam}")).json()
    assert on["enabled"] is True and on["note"].startswith("HOW SHE TYPES (measured from 9 ")
    assert (await panel_client.get("/api/finetune/style-card?tenant_id=999")).status_code == 404
