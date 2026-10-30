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
import session_runtime
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


def test_a_message_asking_to_see_photos_is_told_apart():
    for text in ("could you send a pic while i wait?", "selfie?", "how do you look?", "pic pls",
                 "скинь фото", "как ты выглядишь?", "atsūti bildi", "show me you"):
        assert media.asks_for_photos(text), text
    for text in ("nice pics 😍", "ur pics are fake anyway", "??", "ok 22:00", "are you there?"):
        assert not media.asks_for_photos(text), text


def test_their_messages_since_our_last_one_are_read_one_by_one():
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"},
               {"role": "user", "content": "22:00?"}, {"role": "user", "content": "pic?"}]
    assert media.latest_from_them(history) == ["22:00?", "pic?"]
    assert media.latest_from_them(history[:2]) == []
    assert media.they_ask_for_photos(history)
    # The question mark of one message does not make the next one a request.
    history[-1]["content"] = "nice pics 😍"
    assert not media.they_ask_for_photos(history)


def test_the_photos_he_has_not_had_are_those_not_shown_as_sent():
    items = [{"id": 1, "kind": "photo", "description": "a"}, {"id": 2, "kind": "photo", "description": "b"}]
    history = [{"role": "assistant", "content": media.sent_placeholder(items[0])}, {"role": "user", "content": "more?"}]
    assert media.unsent(items, history) == [items[1]]
    assert media.unsent(items, history[1:]) == items


def test_the_media_note_says_what_the_chat_has_had():
    items = [{"id": 1, "kind": "photo", "description": "a"}, {"id": 2, "kind": "photo", "description": "b"}]
    assert media.sent_line(items, []) == ""
    one = [{"role": "assistant", "content": media.sent_placeholder(items[0])}]
    assert media.sent_line(items, one) == "Already sent in this chat: photo #1."
    both = one + [{"role": "assistant", "content": media.sent_placeholder(items[1])}]
    assert "had all your photos" in media.sent_line(items, both)


def test_the_photo_check_answer_is_read_safely():
    assert media.parse_photo_request('{"photos": true}')
    assert media.parse_photo_request('sure: {"photos": true}')
    assert not media.parse_photo_request('{"photos": false}')
    assert not media.parse_photo_request("yes")
    assert not media.parse_photo_request("{photos: yes")


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
    app.config["burst"]["gap_ms"] = {"min": 0, "max": 0}
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

    monkeypatch.setattr(session_runtime.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(ai_responder, "generate_reply", fake_generate_reply)
    monkeypatch.setattr(app, "go_online_for", nothing)
    monkeypatch.setattr(app, "mark_read", nothing)
    monkeypatch.setattr(app, "borrowed_context", no_context)
    monkeypatch.setattr(app, "schedule_go_offline", lambda chat_id: None)
    monkeypatch.setattr(app, "deepseek_key", "k")
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
    app.config["auto_send"] = True
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
    app.config["auto_send"] = True
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


# ------------------------------------------------- view once / on arrival


def test_flags_are_kept_and_arrival_items_are_hidden_from_the_ai(tmp_path):
    (tmp_path / "beach.jpg").write_bytes(b"jpg")
    (tmp_path / "door.jpg").write_bytes(b"jpg")
    lib = media.MediaLibrary(tmp_path)
    lib.set_flags(1, view_once=True)
    lib.set_flags(2, send_on_arrival=True, view_once=True)
    again = media.MediaLibrary(tmp_path)
    assert again.get(1).get("view_once") is True and not again.get(1).get("send_on_arrival")
    assert [i["id"] for i in again.arrival_items()] == [2]
    assert [i["id"] for i in again.for_ai()] == [1]
    assert "[send 2]" not in media.prompt_section(again.for_ai())
    again.set_flags(2, send_on_arrival=False)
    assert "send_on_arrival" not in again.get(2)
    with pytest.raises(ValueError):
        again.set_flags(1, secret=True)


def test_greeting_items_go_with_the_first_reply_and_stay_known_to_the_ai(tmp_path):
    (tmp_path / "hi.mp4").write_bytes(b"mp4")
    (tmp_path / "door.jpg").write_bytes(b"jpg")
    lib = media.MediaLibrary(tmp_path)
    ids = {item["file"]: item["id"] for item in lib.all()}
    video, door = ids["hi.mp4"], ids["door.jpg"]
    lib.set_flags(video, greeting=True)
    # An arrival item is never a greeting: it holds the way in.
    lib.set_flags(door, greeting=True, send_on_arrival=True)
    again = media.MediaLibrary(tmp_path)
    assert [i["id"] for i in again.greeting_items()] == [video]
    assert [i["id"] for i in again.for_ai()] == [video]
    line = media.greeting_line(again.greeting_items())
    assert f"video #{video}" in line and "write no [send] tag" in line
    again.set_flags(video, greeting=False)
    assert again.greeting_items() == []


# ------------------------------------------------------- the threesome girl


def _threesome_library(tmp_path):
    for name in ("me.jpg", "lina.jpg", "lina-2.jpg", "lina.mp4"):
        (tmp_path / name).write_bytes(b"x")
    lib = media.MediaLibrary(tmp_path)
    ids = {item["file"]: item["id"] for item in lib.all()}
    for name in ("lina.jpg", "lina-2.jpg", "lina.mp4"):
        lib.set_flags(ids[name], threesome=True)
    lib.describe(ids["lina.jpg"], "my friend Lina, blonde")
    return lib, ids


def test_the_threesome_girl_is_kept_apart_from_her_own_files(tmp_path):
    lib, ids = _threesome_library(tmp_path)
    lib.set_flags(ids["lina.mp4"], greeting=True)
    again = media.MediaLibrary(tmp_path)
    assert [i["id"] for i in again.for_ai()] == [ids["me.jpg"]]
    assert [i["file"] for i in again.threesome_items()] == ["lina-2.jpg", "lina.jpg", "lina.mp4"]
    # Never a greeting: the first reply would show her as me.
    assert again.greeting_items() == []
    assert f"[send {ids['lina.jpg']}]" not in media.prompt_section(again.for_ai())
    lina = again.get(ids["lina.jpg"])
    assert media.label(lina) == f"photo #{ids['lina.jpg']} (threesome girl): my friend Lina, blonde"
    # Her row in the history is still a sent file, to every reader of it.
    history = [{"role": "assistant", "content": media.sent_placeholder(lina)}]
    assert media.unsent([lina], history) == []
    assert media.split_attachments(media.sent_placeholder(lina))[1] == [ids["lina.jpg"]]


def test_the_first_time_a_photo_and_a_video_of_her_go_and_no_more(tmp_path):
    lib, ids = _threesome_library(tmp_path)
    items = lib.threesome_items()
    first = media.threesome_pick(items, [], "any")
    assert [i["file"] for i in first] == ["lina-2.jpg", "lina.mp4"]
    history = [{"role": "assistant", "content": media.sent_placeholder(i)} for i in first]
    # He asks about the threesome again (the price, say): he has seen her.
    assert media.threesome_pick(items, history, "any") is None
    # He asks for more photos of her: the ones he has not had.
    assert [i["file"] for i in media.threesome_pick(items, history, "photo")] == ["lina.jpg"]
    assert media.threesome_pick(items, history, "video") == []
    assert "no video of her left" in media.threesome_line([], "video")
    line = media.threesome_line(first)
    assert "photo #" in line and "(threesome girl)" in line and "write no [send] tag" in line


def test_only_photos_of_her_two_go(tmp_path):
    lib, ids = _threesome_library(tmp_path)
    lib.set_flags(ids["lina.mp4"], threesome=False)
    assert [i["file"] for i in media.threesome_pick(lib.threesome_items(), [], "any")] == ["lina-2.jpg", "lina.jpg"]


def test_a_message_that_may_be_about_a_threesome_is_told_apart():
    def said(*texts, mine="hi love 😘 what time do you want to come?"):
        return [{"role": "assistant", "content": mine}, *({"role": "user", "content": t} for t in texts)]

    for text in ("do you do threesome?", "3some?", "duo with another girl?", "two girls possible",
                 "do you have a friend?", "can I see her", "ffm?", "можно втроем?", "есть подруга?",
                 "trijatā var?", "un draudzene?", "trío con tu amiga?"):
        assert media.threesome_may_come_up(said(text)), text
    for text in ("22:00?", "pics?", "how much for 1 hour", "ok", "send video"):
        assert not media.threesome_may_come_up(said(text)), text
    # He says yes to seeing her; after her files went, his time is just a time.
    assert media.threesome_may_come_up(said("yes", mine="want to see my friend? 😘"))
    assert not media.threesome_may_come_up(said("22:00", mine="yes love, with my friend 😘"))
    sent_her = said("22:00", mine="want to see my friend? 😘")
    sent_her.insert(1, {"role": "assistant", "content": "[sent photo #7 (threesome girl): Lina]"})
    assert media.threesome_may_come_up(sent_her)
    sent_her[0]["content"] = "yes love, with my friend 😘"
    assert not media.threesome_may_come_up(sent_her)
    # Nothing of his since our last message: nothing to check.
    assert not media.threesome_may_come_up([{"role": "user", "content": "threesome?"},
                                            {"role": "assistant", "content": "yes love"}])


def test_the_threesome_check_answer_is_read_safely():
    assert media.parse_threesome_request('{"threesome": true, "show": "any"}') == "any"
    assert media.parse_threesome_request('ok {"threesome": true, "show": "video"}') == "video"
    assert media.parse_threesome_request('{"threesome": true, "show": "photo"}') == "photo"
    assert media.parse_threesome_request('{"threesome": true}') == "any"
    assert media.parse_threesome_request('{"threesome": true, "show": "her"}') == "any"
    assert media.parse_threesome_request('{"threesome": false, "show": "photo"}') is None
    assert media.parse_threesome_request('{"threesome": "yes"}') is None
    assert media.parse_threesome_request("yes") is None
    assert media.parse_threesome_request("[true]") is None


@pytest.fixture
def lina(library, monkeypatch):
    """The threesome girl's photo (#3) and video (#4), and a scripted
    threesome check that records whether it was asked."""
    for name in ("lina.jpg", "lina.mp4"):
        (library.dir / name).write_bytes(b"x")
    library.refresh()
    library.set_flags(3, threesome=True)
    library.set_flags(4, threesome=True)
    library.describe(3, "my friend Lina")
    check = {"answer": "any", "asked": 0}

    async def fake_check(**kw):
        check["asked"] += 1
        return check["answer"]

    monkeypatch.setattr(ai_responder, "extract_threesome_request", fake_check)
    return check


@pytest.mark.asyncio
async def test_he_asks_about_a_threesome_and_her_photo_and_video_go_with_the_reply(app, db, drafting, outbox, lina):
    app.config["auto_send"] = True  # videos still need approval: hers are the business's own choice
    await db.upsert_conversation(7, "J", None, False, 1)
    await db.record_message(7, DIR_IN, STATUS_RECEIVED, "do you do threesome?", telegram_id=1)
    drafting["reply"] = "yes love, with my friend 😘"

    await app.draft_worker(7)

    assert outbox == ["yes love, with my friend 😘", ("file", "lina.jpg"), ("file", "lina.mp4")]
    assert "the system sends photo #3 (threesome girl): my friend Lina, video #4 (threesome girl)" in drafting["prompt"]
    assert media.THREESOME_RULE in drafting["prompt"]
    # Not offered to the model as hers.
    assert "[send 3] =" not in drafting["prompt"] and "[send 1] = photo #1: me at the beach" in drafting["prompt"]
    rows = await db.get_messages(7)
    assert rows[-2]["text"] == "[sent photo #3 (threesome girl): my friend Lina]"

    # The price next: he has seen her, nothing more of hers goes.
    await db.record_message(7, DIR_IN, STATUS_RECEIVED, "how much for the threesome?", telegram_id=2)
    drafting["reply"] = "+250 love 😘"
    await app.draft_worker(7)
    assert outbox[3:] == ["+250 love 😘"]
    assert "(threesome girl)" not in drafting["prompt"].replace(media.THREESOME_RULE, "")


@pytest.mark.asyncio
async def test_the_model_never_sends_her_by_tag(app, db, drafting, outbox, lina):
    app.config["auto_send"] = True
    lina["answer"] = None
    await db.upsert_conversation(7, "J", None, False, 1)
    await db.record_message(7, DIR_IN, STATUS_RECEIVED, "pics of you?", telegram_id=1)
    drafting["reply"] = "here love [send 3] [send 1]"

    await app.draft_worker(7)
    assert outbox == ["here love", ("file", "beach.jpg")]


@pytest.mark.asyncio
async def test_no_threesome_check_without_a_word_for_it_or_without_her_files(app, db, drafting, outbox, lina, library):
    app.config["auto_send"] = True
    await db.upsert_conversation(7, "J", None, False, 1)
    await db.record_message(7, DIR_IN, STATUS_RECEIVED, "22:00?", telegram_id=1)
    drafting["reply"] = "ok love"
    await app.draft_worker(7)
    assert lina["asked"] == 0

    library.set_flags(3, threesome=False)
    library.set_flags(4, threesome=False)
    await db.record_message(7, DIR_IN, STATUS_RECEIVED, "threesome?", telegram_id=2)
    await app.draft_worker(7)
    assert lina["asked"] == 0
    assert media.THREESOME_RULE not in drafting["prompt"]


@pytest.mark.asyncio
async def test_a_failed_threesome_check_still_lets_the_reply_go(app, db, drafting, outbox, lina, monkeypatch):
    async def down(**kw):
        raise ai_responder.AIResponderError("timed out")

    monkeypatch.setattr(ai_responder, "extract_threesome_request", down)
    app.config["auto_send"] = True
    await db.upsert_conversation(7, "J", None, False, 1)
    await db.record_message(7, DIR_IN, STATUS_RECEIVED, "threesome?", telegram_id=1)
    drafting["reply"] = "yes love 😘"

    await app.draft_worker(7)
    assert outbox == ["yes love 😘"]


@pytest.mark.asyncio
async def test_when_he_has_had_all_of_her_the_reply_is_told(app, db, drafting, outbox, lina, library):
    app.config["auto_send"] = True
    await db.upsert_conversation(7, "J", None, False, 1)
    await app.send_burst(7, [], attachments=[3, 4])
    await db.record_message(7, DIR_IN, STATUS_RECEIVED, "more pics of her?", telegram_id=1)
    lina["answer"] = "photo"
    drafting["reply"] = "that's all I have of her love 😘"

    await app.draft_worker(7)
    assert "no photo of her left" in drafting["prompt"]
    assert outbox[-1] == "that's all I have of her love 😘"


@pytest.mark.asyncio
async def test_a_view_once_item_is_sent_as_view_once(app, library, monkeypatch):
    seen = {}

    async def fake_send_file(peer, chat_id, path, is_video, show_upload, view_once=False):
        seen["view_once"] = view_once

    monkeypatch.setattr(app.transport, "send_file", fake_send_file)
    library.set_flags(1, view_once=True)
    await app.deliver_file(1, 1, library.get(1), library.path(1))
    assert seen["view_once"] is True
    await app.deliver_file(1, 1, library.get(2), library.path(2))
    assert seen["view_once"] is False


@pytest.mark.asyncio
async def test_telegram_view_once_and_delete_for_both_sides(tmp_path):
    import telegram_transport

    calls = {}

    class FakeClient:
        async def send_file(self, peer, file, **kw):
            calls["send"] = kw
            return type("Sent", (), {"id": 5})()

        async def delete_messages(self, peer, ids, revoke=False):
            calls["delete"] = (peer, ids, revoke)

    transport = object.__new__(telegram_transport.TelegramTransport)
    transport.client = FakeClient()
    (tmp_path / "a.jpg").write_bytes(b"jpg")
    await transport.send_file("peer", 1, tmp_path / "a.jpg", False, False, view_once=True)
    assert calls["send"]["ttl"] == telegram_transport.VIEW_ONCE_TTL
    await transport.send_file("peer", 1, tmp_path / "a.jpg", False, False)
    assert calls["send"]["ttl"] is None
    await transport.delete_messages("peer", 1, ["7", 8])
    assert calls["delete"] == ("peer", [7, 8], True)
