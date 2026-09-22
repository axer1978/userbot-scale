"""Photos and videos the AI can attach when asked.

The model marks a file with `[send N]`; the tag comes out of the text and
the file goes to Telegram as its own message after the text, recorded with
a placeholder so the history shows what was sent. Videos are held for
approval by default even when auto-send is on.
"""

from __future__ import annotations

import pytest

import ai_responder
import media
from database import DIR_IN, DIR_OUT, STATUS_PENDING, STATUS_RECEIVED, STATUS_SENT


# ------------------------------------------------------------- the tag

def test_a_tag_is_taken_out_and_its_id_returned():
    text, ids = media.split_attachments("here you go 😊\n[send 3]")
    assert text == "here you go 😊"
    assert ids == [3]


def test_the_tag_forms_the_model_actually_writes():
    for raw in ("[send: 3]", "[SEND #3]", "[send photo 3]", "[[send 3]]", "[send video 3 ]"):
        assert media.split_attachments(raw)[1] == [3], raw


def test_a_tag_in_the_middle_of_a_line_leaves_no_hole():
    text, ids = media.split_attachments("this one [send 2] is my favourite")
    assert text == "this one is my favourite"
    assert ids == [2]


def test_repeats_are_folded_and_the_count_is_capped():
    text, ids = media.split_attachments("[send 1] [send 1] [send 2] [send 3] [send 4]")
    assert text == ""
    assert ids == [1, 2, 3][: media.MAX_ATTACHMENTS_PER_REPLY]


def test_plain_text_is_untouched():
    assert media.split_attachments("send me one|||ok") == ("send me one|||ok", [])


# --------------------------------------------------------- the library

def test_files_dropped_in_the_folder_are_picked_up(tmp_path):
    (tmp_path / "beach.jpg").write_bytes(b"x")
    (tmp_path / "clip.mp4").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")
    lib = media.MediaLibrary(tmp_path)
    kinds = {i["file"]: i["kind"] for i in lib.all()}
    assert kinds == {"beach.jpg": "photo", "clip.mp4": "video"}


def test_ids_survive_a_restart_and_are_never_reused(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"x")
    lib = media.MediaLibrary(tmp_path)
    first = lib.all()[0]["id"]
    lib.describe(first, "me at the beach")
    lib.remove(first)
    (tmp_path / "b.jpg").write_bytes(b"x")

    again = media.MediaLibrary(tmp_path)
    assert [i["file"] for i in again.all()] == ["b.jpg"]
    assert again.all()[0]["id"] != first
    assert not (tmp_path / "a.jpg").exists()


def test_a_removed_file_drops_out_on_refresh(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"x")
    lib = media.MediaLibrary(tmp_path)
    (tmp_path / "a.jpg").unlink()
    assert lib.refresh() is True
    assert lib.all() == []


def test_unsafe_names_cannot_leave_the_folder(tmp_path):
    lib = media.MediaLibrary(tmp_path)
    assert "/" not in lib.unique_name("../../etc/passwd.jpg")
    assert lib.unique_name("..\\x.jpg") == "x.jpg"
    (tmp_path / "x.jpg").write_bytes(b"x")
    assert lib.unique_name("x.jpg") == "x (2).jpg"


# ----------------------------------------------------------- the prompt

def test_the_prompt_lists_each_file_by_tag_and_description():
    items = [
        {"id": 3, "kind": "photo", "file": "beach.jpg", "description": "me at the beach"},
        {"id": 5, "kind": "video", "file": "gym_clip.mp4", "description": ""},
    ]
    note = media.prompt_section(items)
    assert "[send 3] = photo #3: me at the beach" in note
    # No description: the file name stands in, tidied up.
    assert "[send 5] = video #5: gym clip" in note
    assert media.ASK_FIRST_RULE in note


def test_the_ask_first_rule_only_appears_when_wanted():
    photo = [{"id": 1, "kind": "photo", "file": "a.jpg", "description": "a"}]
    video = [{"id": 2, "kind": "video", "file": "b.mp4", "description": "b"}]
    assert media.ASK_FIRST_RULE not in media.prompt_section(photo)
    assert media.ASK_FIRST_RULE not in media.prompt_section(video, ask_before_video=False)
    assert media.prompt_section([]) == ""


@pytest.mark.asyncio
async def test_the_note_reaches_the_system_prompt(monkeypatch):
    seen: dict = {}

    async def fake_complete(*, api_key, messages, ai_config, client=None):
        seen["system"] = messages[0]["content"]
        return "ok"

    monkeypatch.setattr(ai_responder, "_complete", fake_complete)
    await ai_responder.generate_reply(
        api_key="k", history=[{"role": "user", "content": "pic?"}],
        persona={}, ai_config={}, media_note="PHOTOS: [send 1] = photo #1: x",
    )
    assert "[send 1]" in seen["system"]


# ------------------------------------------------------------- sending

@pytest.fixture
def library(app, monkeypatch, tmp_path):
    folder = tmp_path / "media"
    folder.mkdir()
    (folder / "beach.jpg").write_bytes(b"jpg")
    (folder / "clip.mp4").write_bytes(b"mp4")
    lib = media.MediaLibrary(folder)
    lib.describe(1, "me at the beach")
    lib.describe(2, "gym clip")
    monkeypatch.setattr(app, "media_library", lib)
    return lib


@pytest.fixture
def outbox(app, monkeypatch):
    """What would go to Telegram: text, or ('file', path) for an upload."""
    sent: list = []

    async def fake_resolve_peer(chat_id):
        return chat_id

    async def fake_deliver(peer, chat_id, text, typing):
        sent.append(text)
        return type("Sent", (), {"id": 1000 + len(sent)})()

    async def fake_deliver_file(peer, chat_id, item, path):
        sent.append(("file", path.name))
        return type("Sent", (), {"id": 1000 + len(sent)})()

    monkeypatch.setattr(app, "resolve_peer", fake_resolve_peer)
    monkeypatch.setattr(app, "deliver", fake_deliver)
    monkeypatch.setattr(app, "deliver_file", fake_deliver_file)
    monkeypatch.setattr(app, "BURST_GAP_MIN_SECONDS", 0)
    monkeypatch.setattr(app, "BURST_GAP_MAX_SECONDS", 0)
    return sent


@pytest.mark.asyncio
async def test_files_follow_the_text_as_their_own_messages(app, db, library, outbox):
    await db.upsert_conversation(7, "J", None, False, 1)
    await app.send_burst(7, ["here", "hope you like it"], attachments=[1])

    assert outbox == ["here", "hope you like it", ("file", "beach.jpg")]
    rows = await db.get_messages(7)
    assert [r["text"] for r in rows] == ["here", "hope you like it", "[sent photo #1: me at the beach]"]
    assert rows[-1]["attachments"] == [1]
    assert all(r["status"] == STATUS_SENT for r in rows)
    # Three messages by every measure, the file included.
    assert await db.sent_since("1970-01-01T00:00:00+00:00") == 3


@pytest.mark.asyncio
async def test_a_draft_with_a_file_settles_and_the_file_gets_its_own_row(app, db, library, outbox):
    await db.upsert_conversation(7, "J", None, False, 1)
    draft = await db.record_message(
        7, DIR_OUT, STATUS_PENDING, "sure", bump_preview=False, attachments=[1]
    )
    await app.send_burst(7, ["sure"], draft_id=draft["id"], attachments=draft["attachments"])

    rows = await db.get_messages(7)
    assert [r["text"] for r in rows] == ["sure", "[sent photo #1: me at the beach]"]
    assert rows[0]["id"] == draft["id"]
    assert rows[0]["attachments"] == []  # the file is the second row now
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_a_file_only_draft_settles_on_the_file(app, db, library, outbox):
    await db.upsert_conversation(7, "J", None, False, 1)
    draft = await db.record_message(
        7, DIR_OUT, STATUS_PENDING, "", bump_preview=False, attachments=[1]
    )
    await app.send_burst(7, [], draft_id=draft["id"], attachments=[1])

    rows = await db.get_messages(7)
    assert len(rows) == 1
    assert rows[0]["id"] == draft["id"]
    assert rows[0]["status"] == STATUS_SENT
    assert outbox == [("file", "beach.jpg")]


@pytest.mark.asyncio
async def test_a_sent_file_shows_up_in_the_ai_history(app, db, library, outbox):
    """So the model knows what it already sent and does not send it twice."""
    await db.upsert_conversation(7, "J", None, False, 1)
    await db.record_message(7, DIR_IN, STATUS_RECEIVED, "pic?", telegram_id=1)
    await app.send_burst(7, [], attachments=[1])
    history = await db.get_history_for_ai(7)
    assert history[-1] == {"role": "assistant", "content": "[sent photo #1: me at the beach]"}


@pytest.mark.asyncio
async def test_a_file_gone_from_the_library_is_skipped(app, db, library, outbox):
    await db.upsert_conversation(7, "J", None, False, 1)
    await app.send_burst(7, ["hey"], attachments=[99])
    assert outbox == ["hey"]


# --------------------------------------------------------- the draft path

@pytest.fixture
def drafting(app, monkeypatch, library):
    """draft_worker without the waits, and with a scripted model reply."""
    script: dict = {"reply": ""}

    async def no_sleep(_s):
        return None

    async def fake_generate_reply(**kwargs):
        script["prompt"] = kwargs.get("media_note", "")
        return script["reply"]

    async def nothing(*a, **k):
        return None

    async def no_context(chat_id):
        return ""

    monkeypatch.setattr(app.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(app.ai_responder, "generate_reply", fake_generate_reply)
    monkeypatch.setattr(app, "go_online_for", nothing)
    monkeypatch.setattr(app, "mark_read", nothing)
    monkeypatch.setattr(app, "borrowed_context", no_context)
    monkeypatch.setattr(app, "schedule_go_offline", lambda chat_id: None)
    monkeypatch.setattr(app, "env", type("E", (), {"deepseek_key": "k"})())
    return script


@pytest.mark.asyncio
async def test_the_model_is_offered_the_files_and_its_tag_becomes_an_attachment(app, db, drafting, outbox):
    await db.upsert_conversation(7, "J", None, False, 1)
    await db.record_message(7, DIR_IN, STATUS_RECEIVED, "send a pic?", telegram_id=1)
    drafting["reply"] = "here you go\n[send 1]"

    await app.draft_worker(7)

    assert "[send 1] = photo #1: me at the beach" in drafting["prompt"]
    pending = await db.pending_drafts(7)
    assert len(pending) == 1
    assert pending[0]["text"] == "here you go"  # the tag never shows in the draft
    assert pending[0]["attachments"] == [1]


@pytest.mark.asyncio
async def test_a_photo_auto_sends_but_a_video_waits_for_approval(app, db, drafting, outbox):
    app.config["behavior"]["auto_send"] = True
    await db.upsert_conversation(7, "J", None, False, 1)
    await db.record_message(7, DIR_IN, STATUS_RECEIVED, "pic?", telegram_id=1)

    drafting["reply"] = "sure [send 1]"
    await app.draft_worker(7)
    assert outbox == ["sure", ("file", "beach.jpg")]

    drafting["reply"] = "ok here it is [send 2]"
    await app.draft_worker(7)
    assert outbox == ["sure", ("file", "beach.jpg")]  # nothing more went out
    pending = await db.pending_drafts(7)
    assert [p["attachments"] for p in pending] == [[2]]


@pytest.mark.asyncio
async def test_videos_go_straight_out_when_the_rule_is_off(app, db, drafting, outbox):
    app.config["behavior"]["auto_send"] = True
    app.config["media"]["videos_need_approval"] = False
    await db.upsert_conversation(7, "J", None, False, 1)
    await db.record_message(7, DIR_IN, STATUS_RECEIVED, "vid?", telegram_id=1)
    drafting["reply"] = "[send 2]"

    await app.draft_worker(7)
    assert outbox == [("file", "clip.mp4")]


@pytest.mark.asyncio
async def test_with_media_off_the_model_is_told_nothing_and_tags_are_ignored(app, db, drafting, outbox):
    app.config["media"]["enabled"] = False
    await db.upsert_conversation(7, "J", None, False, 1)
    await db.record_message(7, DIR_IN, STATUS_RECEIVED, "pic?", telegram_id=1)
    drafting["reply"] = "no pics sorry [send 1]"

    await app.draft_worker(7)
    assert drafting["prompt"] == ""
    pending = await db.pending_drafts(7)
    assert pending[0]["attachments"] == []
    assert pending[0]["text"] == "no pics sorry"


@pytest.mark.asyncio
async def test_an_outgoing_media_event_is_not_recorded_twice(app, db, library, outbox, monkeypatch):
    """Telethon reports our own upload back with no text; the send path
    already wrote the row, so the mirror handler has to stay out of it."""
    await db.upsert_conversation(7, "J", None, False, 1)
    seen = []

    async def fake_deliver_file(peer, chat_id, item, path):
        # While the upload is in flight, the outgoing event arrives.
        seen.append(dict(app.in_flight_media))
        return type("Sent", (), {"id": 5})()

    monkeypatch.setattr(app, "deliver_file", fake_deliver_file)
    await app.send_media_as_me(7, 1)
    assert seen == [{7: 1}]
    assert app.in_flight_media == {}
