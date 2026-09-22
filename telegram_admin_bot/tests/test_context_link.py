"""Carrying one chat's context into another.

The dangerous failure here is not a missing link but a wrong one — someone
else's conversation feeding a reply — so most of this is about what does
*not* get linked, and about an unlink staying done.
"""

from __future__ import annotations

import pytest

import ai_responder
import config_store
import context_link
from database import DIR_IN, DIR_OUT, LINK_AUTO, LINK_MANUAL, STATUS_RECEIVED, STATUS_SENT


def conv(chat_id, name, username=None, is_bot=False):
    return {
        "chat_id": chat_id,
        "display_name": name,
        "username": username,
        "is_bot": is_bot,
    }


def settings(**overrides):
    return {**config_store.normalize({})["context_link"], **overrides}


# ---------------------------------------------------------------- matching


def test_same_username_is_the_same_person():
    score, reason = context_link.match_score(
        conv(1, "Anna", "anna_k"), conv(2, "Anna K", "Anna_K")
    )
    assert score >= context_link.AUTO_LINK_THRESHOLD
    assert "anna_k" in reason


def test_same_full_name_is_enough_to_link():
    score, _ = context_link.match_score(conv(1, "Anna Keller"), conv(2, "anna keller"))
    assert score >= context_link.AUTO_LINK_THRESHOLD


def test_decoration_in_a_display_name_does_not_hide_a_match():
    score, _ = context_link.match_score(conv(1, "Anna Keller ✨"), conv(2, "Anna  Keller."))
    assert score >= context_link.AUTO_LINK_THRESHOLD


def test_a_shared_first_name_is_not_an_identity():
    """Two people called Alex are two people."""
    score, _ = context_link.match_score(conv(1, "Alex"), conv(2, "alex"))
    assert 0 < score < context_link.AUTO_LINK_THRESHOLD


def test_different_people_do_not_match():
    score, reason = context_link.match_score(
        conv(1, "Anna Keller", "anna_k"), conv(2, "Boris Lang", "b_lang")
    )
    assert (score, reason) == (0.0, "")


def test_a_bot_is_never_the_same_person_as_anyone():
    score, _ = context_link.match_score(
        conv(1, "Support", "support"), conv(2, "Support", "support", is_bot=True)
    )
    assert score == 0.0


def test_a_chat_is_not_a_match_for_itself():
    score, _ = context_link.match_score(conv(1, "Anna", "anna"), conv(1, "Anna", "anna"))
    assert score == 0.0


# ------------------------------------------------------------------ linking


@pytest.mark.asyncio
async def test_a_second_account_is_linked_to_the_first(db):
    await db.upsert_conversation(1, "Anna Keller", "anna_k", False)
    second = await db.upsert_conversation(2, "Anna Keller", "anna2", False)

    created = await context_link.autolink(db, second, settings())

    assert [link["source_id"] for link in created] == [1]
    # Both directions: whichever account she writes from sees the other.
    assert [link["source_id"] for link in await db.get_links(2)] == [1]
    assert [link["source_id"] for link in await db.get_links(1)] == [2]


@pytest.mark.asyncio
async def test_a_weak_match_is_left_for_me_to_decide(db):
    await db.upsert_conversation(1, "Alex", None, False)
    second = await db.upsert_conversation(2, "alex", None, False)

    assert await context_link.autolink(db, second, settings()) == []
    assert await db.get_links(2) == []
    # …but it is still offered as a suggestion in the panel.
    suggestions = await context_link.find_candidates(
        db, second, min_score=context_link.SINGLE_NAME_SCORE
    )
    assert [c["chat_id"] for c, _, _ in suggestions] == [1]


@pytest.mark.asyncio
async def test_auto_detect_off_means_no_links_are_guessed(db):
    await db.upsert_conversation(1, "Anna Keller", "anna_k", False)
    second = await db.upsert_conversation(2, "Anna Keller", "anna_k", False)
    assert await context_link.autolink(db, second, settings(auto_detect=False)) == []


@pytest.mark.asyncio
async def test_only_as_many_sources_as_configured(db):
    for chat_id in (1, 2, 3):
        await db.upsert_conversation(chat_id, "Anna Keller", f"anna{chat_id}", False)
    newest = await db.upsert_conversation(4, "Anna Keller", "anna4", False)

    await context_link.autolink(db, newest, settings(max_sources=2))
    assert len(await db.get_links(4)) == 2


@pytest.mark.asyncio
async def test_unlinking_stays_done(db):
    """Otherwise detection would re-link the pair on their very next message."""
    await db.upsert_conversation(1, "Anna Keller", "anna_k", False)
    second = await db.upsert_conversation(2, "Anna Keller", "anna_k", False)
    await context_link.autolink(db, second, settings())

    await db.unlink_chats(2, 1)
    assert await db.get_links(2) == []
    assert await db.get_links(1) == []

    assert await context_link.autolink(db, second, settings()) == []
    assert await db.get_links(2) == []


@pytest.mark.asyncio
async def test_linking_by_hand_overrides_an_earlier_unlink(db):
    await db.upsert_conversation(1, "Anna", None, False)
    await db.upsert_conversation(2, "Boris", None, False)
    await db.link_chats(2, 1, LINK_AUTO, "guessed", 0.9)
    await db.unlink_chats(2, 1)

    await context_link.link_by_hand(db, 2, 1)
    links = await db.get_links(2)
    assert [(l["source_id"], l["origin"]) for l in links] == [(1, LINK_MANUAL)]


@pytest.mark.asyncio
async def test_a_link_carries_the_reason_it_was_made(db):
    await db.upsert_conversation(1, "Anna Keller", "anna_k", False)
    second = await db.upsert_conversation(2, "Anna Keller", "anna2", False)
    created = await context_link.autolink(db, second, settings())
    assert created[0]["origin"] == LINK_AUTO
    assert "anna keller" in created[0]["reason"].lower()
    assert created[0]["source_name"] == "Anna Keller"


@pytest.mark.asyncio
async def test_a_chat_cannot_be_linked_to_itself(db):
    await db.upsert_conversation(1, "Anna", None, False)
    with pytest.raises(ValueError):
        await db.link_chats(1, 1)


# ----------------------------------------------------------------- summaries


async def seed(db, chat_id, name, *lines):
    await db.upsert_conversation(chat_id, name, None, False)
    for i, (direction, text) in enumerate(lines):
        status = STATUS_RECEIVED if direction == DIR_IN else STATUS_SENT
        await db.record_message(chat_id, direction, status, text, telegram_id=1000 * chat_id + i)


@pytest.mark.asyncio
async def test_a_summary_is_reused_until_the_chat_moves_on(db, monkeypatch):
    calls = []

    async def fake_summary(**kwargs):
        calls.append(kwargs["history"])
        return "- they are moving flat in March"

    monkeypatch.setattr(ai_responder, "summarize_conversation", fake_summary)
    await seed(db, 1, "Anna", (DIR_IN, "hey"), (DIR_OUT, "hi"))

    kwargs = dict(api_key="k", ai_config={}, refresh_after=5)
    first = await context_link.ensure_summary(db, 1, **kwargs)
    second = await context_link.ensure_summary(db, 1, **kwargs)

    assert first == second == "- they are moving flat in March"
    assert len(calls) == 1, "summarised twice for the same conversation"


@pytest.mark.asyncio
async def test_a_summary_is_rebuilt_once_enough_has_been_said(db, monkeypatch):
    calls = []

    async def fake_summary(**kwargs):
        calls.append(len(kwargs["history"]))
        return f"- brief {len(calls)}"

    monkeypatch.setattr(ai_responder, "summarize_conversation", fake_summary)
    await seed(db, 1, "Anna", (DIR_IN, "hey"))

    kwargs = dict(api_key="k", ai_config={}, refresh_after=2)
    assert await context_link.ensure_summary(db, 1, **kwargs) == "- brief 1"
    await db.record_message(1, DIR_IN, STATUS_RECEIVED, "still there?", telegram_id=99)
    await db.record_message(1, DIR_IN, STATUS_RECEIVED, "hello?", telegram_id=98)
    assert await context_link.ensure_summary(db, 1, **kwargs) == "- brief 2"


@pytest.mark.asyncio
async def test_a_failed_summary_falls_back_to_the_last_good_one(db, monkeypatch):
    """A dead API must cost the extra context, never the reply itself."""
    await seed(db, 1, "Anna", (DIR_IN, "hey"))

    async def ok(**kwargs):
        return "- brief"

    monkeypatch.setattr(ai_responder, "summarize_conversation", ok)
    kwargs = dict(api_key="k", ai_config={}, refresh_after=1)
    assert await context_link.ensure_summary(db, 1, **kwargs) == "- brief"

    async def boom(**kwargs):
        raise ai_responder.AIResponderError("DeepSeek is down.")

    monkeypatch.setattr(ai_responder, "summarize_conversation", boom)
    await db.record_message(1, DIR_IN, STATUS_RECEIVED, "you there?", telegram_id=97)
    assert await context_link.ensure_summary(db, 1, **kwargs) == "- brief"


@pytest.mark.asyncio
async def test_an_empty_chat_is_not_summarised(db, monkeypatch):
    async def boom(**kwargs):
        raise AssertionError("should not have called the API")

    monkeypatch.setattr(ai_responder, "summarize_conversation", boom)
    await db.upsert_conversation(1, "Anna", None, False)
    assert await context_link.ensure_summary(db, 1, api_key="k", ai_config={}) == ""


# ---------------------------------------------------------------- background


@pytest.mark.asyncio
async def test_background_is_built_from_the_linked_chat(db, monkeypatch):
    async def fake_summary(**kwargs):
        return "- they asked about the invoice"

    monkeypatch.setattr(ai_responder, "summarize_conversation", fake_summary)
    await seed(db, 1, "Anna", (DIR_IN, "about that invoice"))
    await db.upsert_conversation(2, "Anna", None, False)
    await db.link_chats(2, 1, LINK_AUTO, "same name", 0.85)

    background = await context_link.build_background(
        db, 2, api_key="k", ai_config={}, settings=settings()
    )
    assert background == "- they asked about the invoice"


@pytest.mark.asyncio
async def test_an_unlinked_chat_has_no_background(db):
    await seed(db, 1, "Anna", (DIR_IN, "hi"))
    assert await context_link.build_background(
        db, 1, api_key="k", ai_config={}, settings=settings()
    ) == ""


@pytest.mark.asyncio
async def test_the_feature_switched_off_transfers_nothing(db, monkeypatch):
    async def boom(**kwargs):
        raise AssertionError("should not have summarised anything")

    monkeypatch.setattr(ai_responder, "summarize_conversation", boom)
    await seed(db, 1, "Anna", (DIR_IN, "hi"))
    await db.upsert_conversation(2, "Anna", None, False)
    await db.link_chats(2, 1, LINK_AUTO, "same name", 0.85)

    assert await context_link.build_background(
        db, 2, api_key="k", ai_config={}, settings=settings(enabled=False)
    ) == ""


# --------------------------------------------------------------- the prompt


def test_background_reaches_the_prompt_as_something_already_known():
    system = ai_responder.build_system_prompt({"purpose": "", "tone": ""})
    assert "invoice" not in system
    header = ai_responder.BACKGROUND_HEADER
    assert "never mention the other chat" in header.lower()


def test_a_blank_background_adds_nothing_to_the_prompt():
    assert ai_responder.render_transcript([]) == ""


def test_a_transcript_reads_as_a_conversation():
    transcript = ai_responder.render_transcript([
        {"role": "user", "content": "can you still do friday?"},
        {"role": "assistant", "content": "yeah should be fine"},
    ])
    assert transcript == "them: can you still do friday?\nme: yeah should be fine"


# --------------------------------------------------------- the drafting path


@pytest.mark.asyncio
async def test_a_reply_is_drafted_with_the_linked_chat_s_context(app, db, monkeypatch):
    """The whole point, end to end: she writes from a new account and the
    draft is made knowing what the old one established."""
    captured: dict = {}

    async def fake_reply(**kwargs):
        captured.update(kwargs)
        return "friday still works"

    async def fake_summary(**kwargs):
        return "- they asked to move friday"

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(ai_responder, "generate_reply", fake_reply)
    monkeypatch.setattr(ai_responder, "summarize_conversation", fake_summary)
    # `env` is only bound at startup, so there is nothing to replace yet.
    monkeypatch.setattr(
        app, "env", type("Env", (), {"deepseek_key": "k"})(), raising=False
    )
    monkeypatch.setattr(app, "go_online_for", nothing)
    monkeypatch.setattr(app, "mark_read", nothing)
    monkeypatch.setattr(app, "schedule_go_offline", lambda chat_id: None)
    app.config["timing"]["min_delay_seconds"] = 0
    app.config["timing"]["max_delay_seconds"] = 0

    await seed(db, 1, "Anna Keller", (DIR_IN, "can we move friday?"))
    await seed(db, 2, "Anna Keller", (DIR_IN, "hey, me again on my new number"))
    await db.link_chats(2, 1, LINK_AUTO, "same full name", 0.85)

    await app.draft_worker(2)

    assert captured["background"] == "- they asked to move friday"
    drafts = await db.pending_drafts(2)
    assert [d["text"] for d in drafts] == ["friday still works"]


@pytest.mark.asyncio
async def test_an_unlinked_chat_drafts_with_no_borrowed_context(app, db, monkeypatch):
    captured: dict = {}

    async def fake_reply(**kwargs):
        captured.update(kwargs)
        return "hey"

    async def boom(**kwargs):
        raise AssertionError("nothing to summarise for an unlinked chat")

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(ai_responder, "generate_reply", fake_reply)
    monkeypatch.setattr(ai_responder, "summarize_conversation", boom)
    # `env` is only bound at startup, so there is nothing to replace yet.
    monkeypatch.setattr(
        app, "env", type("Env", (), {"deepseek_key": "k"})(), raising=False
    )
    monkeypatch.setattr(app, "go_online_for", nothing)
    monkeypatch.setattr(app, "mark_read", nothing)
    monkeypatch.setattr(app, "schedule_go_offline", lambda chat_id: None)
    app.config["timing"]["min_delay_seconds"] = 0
    app.config["timing"]["max_delay_seconds"] = 0

    await seed(db, 1, "Anna", (DIR_IN, "hi"))
    await app.draft_worker(1)

    assert captured["background"] == ""
