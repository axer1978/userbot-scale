"""The words around bookings: the owner's typed answers, what is read out
of the model's extraction, and the lines the reply writer is given."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import booking_states as bs
import bookings

TODAY = date(2026, 10, 1)
RIGA = ZoneInfo("Europe/Riga")


def parse(text):
    return bookings.parse_owner_reply(text, today=TODAY)


@pytest.mark.parametrize("text,kind,number", [
    ("YES 7", "yes", 7), ("yes #7", "yes", 7), ("Jā 7", "yes", 7), ("да 7", "yes", 7), ("👍 7", "yes", 7),
    ("NO 7", "no", 7), ("nē 7", "no", 7), ("нет 12", "no", 12),
    ("CANCEL 7", "cancel", 7), ("atcelt 7", "cancel", 7),
    ("done 7", "done", 7), ("NOSHOW 7", "noshow", 7), ("no-show 7", "noshow", 7),
    ("yes", "yes", None), ("ok!", "yes", None), ("LIST", "list", None), ("?", "list", None),
])
def test_owner_commands(text, kind, number):
    cmd = parse(text)
    assert (cmd.kind, cmd.number) == (kind, number)


@pytest.mark.parametrize("text,at,day", [
    ("7 15:30", time(15, 30), None),
    ("7 15.30", time(15, 30), None),
    ("7 04.10 15:30", time(15, 30), date(2026, 10, 4)),
    ("7 15:30 04.10", time(15, 30), date(2026, 10, 4)),
    ("7 04/10/2026 9:05", time(9, 5), date(2026, 10, 4)),
    ("7 2026-10-04 15:30", time(15, 30), date(2026, 10, 4)),
    ("7 tomorrow 15:30", time(15, 30), date(2026, 10, 2)),
    ("7 rīt 15:30", time(15, 30), date(2026, 10, 2)),
    ("time 7 today 18:00", time(18, 0), TODAY),
    ("7 02.01 10:00", time(10, 0), date(2027, 1, 2)),  # written in autumn: next January
])
def test_owner_proposes_a_new_time(text, at, day):
    cmd = parse(text)
    assert (cmd.kind, cmd.number, cmd.at, cmd.day, cmd.error) == ("propose", 7, at, day, "")


def test_a_bad_time_is_an_error_to_explain_not_silence():
    assert parse("7 25:00").error
    assert parse("7 31.02 10:00").error
    assert parse("7 tomorrow").error == "no time given (use HH:MM)"


def test_ordinary_conversation_is_not_a_command():
    for text in ("7 people are coming", "hello", "no problem at all, see you on friday then", "", "yes I think so and more"):
        assert parse(text) is None, text


@pytest.mark.parametrize("text,kind,number", [
    ("READY 7", "ready", 7), ("ready", "ready", None), ("Ready!", "ready", None), ("gatava 7", "ready", 7),
    ("готова 7", "ready", 7),
    ("IN 7", "in_taxi", 7), ("in", "in_taxi", None), ("TAXI 7", "in_taxi", 7), ("in taxi 7", "in_taxi", 7),
    ("в такси 7", "in_taxi", 7), ("taksī", "in_taxi", None),
])
def test_the_outcall_steps_from_the_owner(text, kind, number):
    cmd = parse(text)
    assert (cmd.kind, cmd.number) == (kind, number)


def test_talk_about_being_ready_or_in_a_while_is_not_a_command():
    for text in ("in 10 min", "ready when you are", "в 10", "taxi is here soon", "ready?", "in a bit"):
        assert parse(text) is None, text


@pytest.mark.parametrize("link,ready,taxi", [
    ("https://bolt.eu/ride/share/abc", False, True),
    ("https://trip.uber.com/AbC12", False, True),
    ("https://taxi.yandex.ru/route-enter?x=1", False, True),
    ("https://baltictaxi.lv/t/123", False, True),
    ("https://example.com/r/123", False, False),
    ("https://example.com/r/123", True, True),
    ("https://maps.app.goo.gl/xyz", True, False),
    ("https://www.google.com/maps/place/Riga", True, False),
    ("https://example.com/didier", False, False),
])
def test_a_taxi_link_is_told_from_a_map(link, ready, taxi):
    assert bookings.is_taxi_link(link, ready=ready) is taxi


def test_links_are_found_in_a_message():
    assert bookings.find_links("here it is: https://bolt.eu/ride/abc. see you") == ["https://bolt.eu/ride/abc"]
    assert bookings.find_links("bolt.eu/r/xyz") == ["bolt.eu/r/xyz"]
    assert bookings.find_links("no link, 3rd floor. flat 12") == []


def outcall(**fields):
    start = datetime(2026, 10, 1, 21, 0, tzinfo=RIGA)
    return {"number": 5, "state": bs.CONFIRMED, "starts_at": start, "ends_at": start + timedelta(hours=1),
            "tz": "Europe/Riga", "notes": "Outcall. Hotel Bellevue", "customer_name": "Anna",
            "customer_username": None, "proposed_by": None, **fields}


def test_an_outcall_confirmation_says_she_needs_time_to_get_ready():
    soon = bookings.outcall_notice_line(outcall(customer_notice=bs.NOTICE_CONFIRMED),
                                        now=datetime(2026, 10, 1, 20, 30, tzinfo=RIGA), ready_minutes=20,
                                        has_address=True)
    assert "CONFIRMED" in soon and "about 20 minutes to get ready" in soon and "Do not ask them to order" in soon
    later = bookings.outcall_notice_line(outcall(customer_notice=bs.NOTICE_CONFIRMED),
                                         now=datetime(2026, 10, 1, 15, 0, tzinfo=RIGA), ready_minutes=20,
                                         has_address=True)
    assert "20 minutes" not in later and "when you are getting ready" in later


def test_the_taxi_steps_as_the_reply_writer_reads_them():
    now = datetime(2026, 10, 1, 20, 30, tzinfo=RIGA)
    ready = bookings.outcall_notice_line(outcall(customer_notice=bs.NOTICE_READY), now=now, ready_minutes=20,
                                         has_address=True)
    assert "order a taxi for you now" in ready and "its link" in ready and "separate message" in ready
    assert "In your own words" in ready
    assert "separate message" not in bookings.outcall_notice_line(
        outcall(customer_notice=bs.NOTICE_READY), now=now, ready_minutes=20, has_address=False)
    in_taxi = bookings.outcall_notice_line(outcall(customer_notice=bs.NOTICE_IN_TAXI), now=now, ready_minutes=20,
                                           has_address=True)
    assert "in the taxi now" in in_taxi and "floor" in in_taxi and "still need to find them" in in_taxi

    assert "Do not ask them to order it" in bookings.standing_line(outcall(), outcall_taxi=True)
    assert "send you its link" in bookings.standing_line(outcall(ready_at=now), outcall_taxi=True)
    assert "about to get in" in bookings.standing_line(outcall(ready_at=now, taxi_link_at=now), outcall_taxi=True)
    on_way = bookings.standing_line(outcall(ready_at=now, taxi_link_at=now, in_taxi_at=now), outcall_taxi=True)
    assert "on your way to them" in on_way and "still need something to find them" in on_way
    # Never the incall rule about the way in.
    assert bookings.NO_DIRECTIONS_RULE not in on_way
    # With the taxi steps off, an outcall reads as before.
    assert bookings.NO_DIRECTIONS_RULE in bookings.standing_line(outcall())


def test_with_the_proof_he_is_asked_for_a_photo_of_the_place_before_the_taxi():
    now = datetime(2026, 10, 1, 20, 30, tzinfo=RIGA)
    confirmed = bookings.outcall_notice_line(outcall(customer_notice=bs.NOTICE_CONFIRMED), now=now,
                                             ready_minutes=20, has_address=True, proof=True)
    assert "CONFIRMED" in confirmed and "hotel room key card" in confirmed and "do not ask for the taxi yet" in confirmed
    assert "get ready" not in confirmed
    waiting = bookings.standing_line(outcall(), outcall_taxi=True, proof=True)
    assert "No taxi until it comes" in waiting
    came = bookings.outcall_notice_line(outcall(customer_notice=bs.NOTICE_READY, proof_at=now, ready_at=now),
                                        now=now, ready_minutes=20, has_address=True, proof=True)
    assert "their photo of the place reached you" in came and "order a taxi for you now" in came
    assert "separate message" in came
    assert "Their photo of the place came" in bookings.standing_line(outcall(proof_at=now, ready_at=now),
                                                                     outcall_taxi=True, proof=True)
    # Once she is ready by READY n, the photo is no longer asked for.
    assert "photo" not in bookings.standing_line(outcall(ready_at=now), outcall_taxi=True, proof=True)
    assert "did not come through" in bookings.PROOF_AGAIN_LINE


def test_the_owner_hears_how_to_take_an_outcall_on():
    assert "send READY 5" in bookings.format_acknowledgement(outcall(), "confirm", outcall_taxi=True)
    assert "READY" not in bookings.format_acknowledgement(outcall(), "confirm")
    assert "READY" not in bookings.format_acknowledgement(outcall(notes=""), "confirm", outcall_taxi=True)
    link = bookings.format_taxi_link(outcall(), "https://bolt.eu/r/1", 5)
    assert "https://bolt.eu/r/1" in link and "Send IN 5" in link and "in 5 min" in link
    assert "Send IN" not in bookings.format_taxi_link(outcall(), "https://bolt.eu/r/1", None)
    listed = bookings.format_list([], [outcall(ready_at=datetime(2026, 10, 1, 20, 0, tzinfo=RIGA))])
    assert "Outcalls:" in listed and "#5 — Anna" in listed and "waiting for his taxi link" in listed
    # With the proof: he sends a photo of the place first; READY skips it.
    proof = bookings.format_acknowledgement(outcall(), "confirm", outcall_taxi=True, proof=True)
    assert "photo that shows he's at the place" in proof and "READY 5 skips the photo" in proof
    assert "waiting for his photo of the place" in bookings.format_list([], [outcall()], proof=True)
    came = datetime(2026, 10, 1, 20, 0, tzinfo=RIGA)
    assert "his photo came" in bookings.outcall_step(outcall(proof_at=came, ready_at=came), proof=True)
    header = bookings.format_proof_header(outcall(), "room 312")
    assert header.startswith("📷 #5 Anna") and "order your taxi now" in header and header.endswith("\nroom 312")


def test_a_date_that_looks_like_a_time_is_read_as_the_date():
    cmd = parse("7 12.10 9.30")
    assert (cmd.day, cmd.at) == (date(2026, 10, 12), time(9, 30))


def test_a_proposed_time_without_a_day_is_on_the_bookings_day():
    b = {"starts_at": datetime(2026, 10, 3, 11, 0, tzinfo=timezone.utc), "tz": "Europe/Riga"}
    assert bookings.resolve_local(parse("7 15:30"), b).isoformat() == "2026-10-03T15:30:00+03:00"
    assert bookings.resolve_local(parse("7 04.10 15:30"), b).isoformat() == "2026-10-04T15:30:00+03:00"


def test_extraction_needs_an_intent_and_a_time_to_book():
    assert bookings.parse_extraction('{"intent": "none"}') is None
    assert bookings.parse_extraction('{"intent": "book"}') is None
    assert bookings.parse_extraction('{"intent": "dance"}') is None
    assert bookings.parse_extraction("no json") is None
    found = bookings.parse_extraction('```json\n{"intent": "book", "start": "2026-10-04T15:00"}\n```')
    assert found["start"] == "2026-10-04T15:00"
    assert bookings.parse_extraction('{"intent": "cancel"}')["intent"] == "cancel"


def test_right_now_books_without_a_start():
    found = bookings.parse_extraction('{"intent": "book", "asap": true, "start": null}')
    assert found["asap"] is True
    assert bookings.parse_extraction('{"intent": "book", "asap": false, "start": null}') is None


def test_a_requested_slot_lasts_real_minutes_across_the_clock_change():
    # Riga goes back an hour at 04:00 on 25 Oct 2026.
    start, end = bookings.requested_slot({"start": "2026-10-25T02:30", "duration_minutes": 120},
                                         tz_name="Europe/Riga", default_duration=60)
    assert end.astimezone(timezone.utc) - start.astimezone(timezone.utc) == timedelta(hours=2)
    start, end = bookings.requested_slot({"start": "2026-10-04T15:00"}, tz_name="Europe/Riga", default_duration=45)
    assert end - start == timedelta(minutes=45)


def test_now_is_the_next_five_minute_mark_at_least_two_minutes_ahead():
    at = lambda h, m, s=0: datetime(2026, 10, 5, h, m, s, tzinfo=timezone.utc)  # noqa: E731
    assert bookings.soonest_start(at(22, 12, 51)) == at(22, 15)
    assert bookings.soonest_start(at(22, 15, 40)) == at(22, 20)   # the model said 22:15: already past
    assert bookings.soonest_start(at(22, 13)) == at(22, 15)
    assert bookings.soonest_start(at(22, 14, 59)) == at(22, 20)
    assert bookings.soonest_start(at(23, 58, 30)) == datetime(2026, 10, 6, 0, 5, tzinfo=timezone.utc)


def test_asap_and_a_time_that_just_passed_both_mean_now():
    now = datetime(2026, 10, 5, 19, 20, 40, tzinfo=timezone.utc)   # 22:20:40 in Riga
    soon = datetime(2026, 10, 5, 19, 25, tzinfo=timezone.utc)
    start, end = bookings.requested_slot({"asap": True, "start": None}, tz_name="Europe/Riga",
                                         default_duration=60, now=now)
    assert start == soon and end - start == timedelta(minutes=60)
    start, _ = bookings.requested_slot({"start": "2026-10-05T22:20"}, tz_name="Europe/Riga",
                                       default_duration=60, now=now)
    assert start == soon
    # Long past is left alone: the slot check says so, as before.
    start, _ = bookings.requested_slot({"start": "2026-10-05T20:00"}, tz_name="Europe/Riga",
                                       default_duration=60, now=now)
    assert start.hour == 20
    assert bookings.requested_slot({"asap": True}, tz_name="Europe/Riga", default_duration=60) is None


def test_reminder_answers_are_only_bare_1_or_2():
    assert bookings.reminder_answer("1") == "confirm"
    assert bookings.reminder_answer(" 2 ") == "cancel"
    assert bookings.reminder_answer("1 or 2?") is None
    assert bookings.reminder_answer("I'll be 2 minutes late") is None


def _b(**extra):
    start = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    return {"number": 7, "state": bs.PENDING, "starts_at": start, "ends_at": start + timedelta(hours=1),
            "tz": "Europe/Riga", "customer_name": "Anna", "customer_username": "anna", "service": "haircut",
            "notes": "", "proposed_by": None, "proposed_starts_at": None, "proposed_ends_at": None,
            "customer_notice": None, "instructions_sent_at": None, **extra}


def test_the_owner_request_names_the_number_and_how_to_answer():
    text = bookings.format_request(_b())
    assert "Booking request #7" in text and "Anna (@anna)" in text
    assert "Sun 04 Oct 2026, 15:00–16:00" in text
    assert "YES 7" in text and "NO 7" in text and "7 15:30" in text


def test_the_request_names_her_and_the_extras_he_asked_for():
    text = bookings.format_request(_b(service="", notes="Asked for now first.", extras="domination +50"),
                                   name="Pamella")
    assert text.split("\n")[:6] == [
        "📅 Booking request #7",
        "Customer: Anna (@anna)",
        "Girl: Pamella",
        "Extras: domination +50",
        "When: Sun 04 Oct 2026, 15:00–16:00",
        "Notes: Asked for now first.",
    ]
    assert text.endswith("Reply YES 7 or NO 7, or a new time like 7 15:30 or 7 04.10 15:30.")
    assert "Booking request #7 (new time)" in bookings.format_request(_b(), changed=True, name="Lola")
    # No extras in the chat: said so. No name: no line.
    assert "Extras: none mentioned" in bookings.format_request(_b())
    assert "Girl:" not in bookings.format_request(_b())


def test_owner_updates_start_with_her_name():
    moved = _b(state=bs.CONFIRMED, proposed_by=bs.CUSTOMER,
               proposed_starts_at=datetime(2026, 10, 4, 13, 0, tzinfo=timezone.utc),
               proposed_ends_at=datetime(2026, 10, 4, 14, 0, tzinfo=timezone.utc))
    assert bookings.format_move_request(moved, name="Lola").startswith("🔁 Lola — #7: Anna (@anna) asks")
    assert bookings.format_owner_update(_b(), "lapsed.", name="Lola").startswith("Lola — #7 Anna (@anna), ")
    # No name: as before.
    assert bookings.format_owner_update(_b(), "lapsed.").startswith("#7 Anna (@anna), ")


def test_the_extraction_asks_for_the_extras():
    assert '"extras"' in bookings.EXTRACT_SYSTEM_PROMPT


@pytest.mark.parametrize("text, loose", [
    ("YES 7", False), ("yes", False), ("ok!", False), ("NO 7", False),
    ("ok in 20 min", True), ("yes, see you", True), ("YES 7 after 3", True), ("no 7 sorry", True),
])
def test_a_yes_or_no_with_words_after_it_is_loose(text, loose):
    cmd = bookings.parse_owner_reply(text, today=date(2026, 10, 4))
    assert cmd is not None and cmd.loose is loose


def test_strict_commands_are_never_loose():
    for text in ("CANCEL 7", "DONE 7", "7 15:30", "LIST", "READY 7"):
        assert not bookings.parse_owner_reply(text, today=date(2026, 10, 4)).loose


def _answer(**fields):
    """A cleaned answer as parse_owner_answer gives it: every key, empty by default."""
    return {"action": None, "number": None, "start": None, "in_minutes": None, "duration_minutes": None,
            "extras_ok": [], "extras_no": [], "tell_customer": None, "ask_customer": None, "for_me": False,
            "question": None, **fields}


@pytest.mark.parametrize("raw, expected", [
    # The shape before there could be several answers: one answer.
    ('{"action": "yes", "number": 8, "start": null, "in_minutes": null}', _answer(action="yes", number=8)),
    ('Sure: ```{"action": "ready_at", "number": "#8", "in_minutes": 20.0}```',
     _answer(action="ready_at", number=8, in_minutes=20)),
    ('{"answers": [{"action": "time", "number": null, "start": "2026-10-20T15:30"}]}',
     _answer(action="time", start="2026-10-20T15:30")),
    ('{"answers": [{"action": "ready_at", "number": 0, "in_minutes": -5}]}', _answer(action="ready_at")),
    # Everything she wants passed on, checked and trimmed.
    ('{"answers": [{"action": "yes", "number": 3, "duration_minutes": "60", "extras_ok": "domination, Domination",'
     ' "extras_no": ["anal", "", 7], "tell_customer": "  bring\\n cash ", "ask_customer": "", "for_me": "true"}]}',
     _answer(action="yes", number=3, duration_minutes=60, extras_ok=["domination"], extras_no=["anal"],
             tell_customer="bring cash")),
    # A length out of range is no length.
    ('{"answers": [{"action": "yes", "number": 3, "duration_minutes": 5}]}', _answer(action="yes", number=3)),
    ('{"answers": [{"action": "yes", "number": 3, "duration_minutes": 9000}]}', _answer(action="yes", number=3)),
    # An unknown action that carries words is about the booking, nothing more.
    ('{"answers": [{"action": "relay", "number": 6, "tell_customer": "I\'m 10 min late"}]}',
     _answer(action="none", number=6, tell_customer="I'm 10 min late")),
    ('{"answers": [{"action": "none", "number": 6, "for_me": true}]}', _answer(action="none", number=6, for_me=True)),
])
def test_the_owner_answer_is_cleaned(raw, expected):
    reading = bookings.parse_owner_answer(raw)
    assert reading is not None and reading.answers == (expected,)


def test_one_message_can_answer_several_bookings_and_give_a_standing_rule():
    reading = bookings.parse_owner_answer(
        '{"answers": [{"action": "yes", "number": 3}, {"action": "no", "number": 4}, {"action": "none", "number": 5}],'
        ' "standing": "from now on   cash only"}')
    assert [(a["action"], a["number"]) for a in reading.answers] == [("yes", 3), ("no", 4)]
    assert reading.standing == "from now on cash only"
    # At most MAX_ANSWERS; long words are cut.
    many = bookings.parse_owner_answer(
        '{"answers": [' + ", ".join(f'{{"action": "yes", "number": {n}}}' for n in range(1, 20)) + "]}")
    assert len(many.answers) == bookings.MAX_ANSWERS
    long = bookings.parse_owner_answer('{"answers": [{"action": "none", "number": 1, "tell_customer": "' +
                                       "x" * 1000 + '"}]}')
    assert len(long.answers[0]["tell_customer"]) == bookings.MAX_WORDS


@pytest.mark.parametrize("raw", ['{"action": "none"}', '{"answers": []}',
                                 '{"answers": null, "standing": ""}', '{"answers": [{"action": "none"}]}'])
def test_an_answer_about_no_booking_is_an_empty_reading(raw):
    assert bookings.parse_owner_answer(raw) == bookings.OwnerReading()


@pytest.mark.parametrize("raw", ["yes", "", '{"action": ', "[1, 2]", '{"answers": "yes"}', '{"hello": 1}',
                                 # It meant something, in a shape that can't be used: not read, rather
                                 # than read as nothing (then a loose YES still stands, she is told).
                                 '{"action": "maybe"}', '{"answers": [{"action": "whatever"}, {"action": "none"}]}'])
def test_an_unusable_answer_is_none(raw):
    assert bookings.parse_owner_answer(raw) is None


@pytest.mark.parametrize("action, read_as", [("confirm", "yes"), ("Accepted", "yes"), ("decline", "no"),
                                             ("reschedule", "time"), ("taxi", "in_taxi"), ("no_show", "noshow")])
def test_the_models_other_words_for_an_action_are_understood(action, read_as):
    reading = bookings.parse_owner_answer(f'{{"answers": [{{"number": 3, "action": "{action}"}}]}}')
    assert reading.answers[0]["action"] == read_as


def test_an_unknown_action_on_a_booking_she_named_asks_her_rather_than_losing_it():
    reading = bookings.parse_owner_answer('{"answers": [{"number": 3, "action": "think about it"}]}')
    assert (reading.answers[0]["action"], reading.answers[0]["number"]) == ("unclear", 3)


@pytest.mark.parametrize("hm, expected", [((12, 20, 0), (12, 20)), ((12, 21, 0), (12, 25)), ((12, 20, 30), (12, 25)),
                                          ((23, 58, 0), (0, 0))])
def test_her_ready_time_is_a_five_minute_mark(hm, expected):
    at = datetime(2026, 10, 20, *hm, tzinfo=timezone.utc)
    got = bookings.ready_time(at)
    assert (got.hour, got.minute) == expected and got >= at


def test_the_owner_reader_sees_each_booking_and_what_he_asked_for():
    note = bookings.owner_state_note([
        _b(extras="domination +50"),
        _b(number=8, state=bs.CONFIRMED, customer_name="Khan", notes="Outcall.", ready_at=None),
        _b(number=9, state=bs.CONFIRMED, proposed_by=bs.CUSTOMER, customer_name="Ivo",
           proposed_starts_at=datetime(2026, 10, 4, 13, 0, tzinfo=timezone.utc),
           proposed_ends_at=datetime(2026, 10, 4, 14, 0, tzinfo=timezone.utc)),
    ], outcall_taxi=True)
    lines = note.split("\n")
    assert lines[0] == ("#7 Anna: the customer asks for Sun 04 Oct 2026, 15:00–16:00 (1 hour), waiting for their "
                        "answer; incall; extras he asked for: domination +50")
    assert lines[1].startswith("#8 Khan: confirmed for Sun 04 Oct 2026, 15:00–16:00 (1 hour); outcall: send READY 8")
    assert "asks to move it to Sun 04 Oct 2026, 16:00–17:00, waiting for their answer" in lines[2]
    prompt = bookings.owner_answer_prompt("them: ready in 20", "Europe/Riga",
                                          now=datetime(2026, 10, 4, 14, 40, tzinfo=ZoneInfo("Europe/Riga")),
                                          state_note=note, latest="ready in 20", replying_to=7)
    assert "Sunday 2026-10-04 14:40 (Europe/Riga)" in prompt and "replies to the message about booking #7" in prompt
    assert prompt.endswith("Their latest message, the one to decide on: ready in 20")


def test_her_name_line_says_when_to_tell_it():
    line = bookings.own_name_line("Lola")
    assert "Your name is Lola" in line and "confirmed" in line and "arrived" in line


def test_a_pending_booking_is_never_presented_as_confirmed():
    line = bookings.standing_line(_b())
    assert "Do NOT say it is confirmed" in line


def test_news_lines_carry_the_change():
    assert "CONFIRMED" in bookings.notice_line(_b(state=bs.CONFIRMED, customer_notice=bs.NOTICE_CONFIRMED))
    proposed = _b(customer_notice=bs.NOTICE_PROPOSED, proposed_by=bs.OWNER,
                  proposed_starts_at=datetime(2026, 10, 4, 13, 0, tzinfo=timezone.utc),
                  proposed_ends_at=datetime(2026, 10, 4, 14, 0, tzinfo=timezone.utc))
    assert "16:00–17:00" in bookings.notice_line(proposed)
    assert bookings.notice_line(_b()) == ""


def test_an_unavailable_time_offers_alternatives_and_the_waitlist():
    requested = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    alts = [requested + timedelta(hours=2)]
    line = bookings.unavailable_line(requested, "conflict", alts, "Europe/Riga", waitlist=True)
    assert "already taken" in line and "Sun 04 Oct 17:00" in line and "waitlist" in line
    assert "waitlist" not in bookings.unavailable_line(requested, "closed_day", [], "Europe/Riga", waitlist=False)


def test_the_reminder_line_asks_for_1_or_2_and_uses_the_instruction():
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    line = bookings.reminder_line(_b(state=bs.CONFIRMED), "Mention parking is free.", now, "https://x/b/tok")
    assert "Mention parking is free." in line and "reply 1" in line and "https://x/b/tok" in line
    assert "in about 24 hours" in line


def test_the_reply_is_always_told_only_the_owner_confirms():
    # Even with nothing booked yet, the reply may not say "booked".
    text = bookings.context_for_reply([])
    assert text.startswith("APPOINTMENTS:") and bookings.ONLY_OWNER_CONFIRMS_RULE in text


def test_the_address_goes_where_the_reply_wrote_the_tag_as_a_message_of_its_own():
    reply = "hi love\n[block]\nRATES\n[/block]\n[address]"
    filled = bookings.fill_address(reply, "Brivibas iela 1, Riga")
    assert filled == "hi love\n[block]\nRATES\n[/block]\n[block]\nBrivibas iela 1, Riga\n[/block]"
    # Any spelling of the tag, once per reply; without an address it is dropped.
    assert bookings.fill_address("here love [ Address ] [address]", "X").count("X") == 1
    assert bookings.fill_address("here love\n[address]", "") == "here love"
    assert bookings.fill_address("no tag here", "X") == "no tag here"
    assert bookings.take_address_tag("hi\n[address]\nwhat time?") == ("hi\n\nwhat time?", True)


def test_the_address_note_follows_when_the_address_may_go():
    assert bookings.address_tag_line("with_prices") == bookings.ADDRESS_TAG_LINE
    asked = bookings.address_tag_line("when_asked")
    assert "only when they ask where you are" in asked and "not with your prices" in asked
    for where in ("where are you?", "address pls", "send location", "kur tu esi?", "где ты?", "kus sa oled",
                  "what area?"):
        assert bookings.asks_where(where), where
    for other in ("how much?", "ok 22:00", "price for 1 hour", "do you do domination?", "somewhere nice"):
        assert not bookings.asks_where(other), other


# ------------------------------------- the booker's own words, as commands

NOW = datetime(2026, 10, 20, 12, 40, tzinfo=RIGA)


def _row(number, state=bs.PENDING, start="2026-10-20T21:00", minutes=60, **extra):
    begin = bookings.parse_local(start, "Europe/Riga")
    return {"id": number, "number": number, "state": state, "starts_at": begin,
            "ends_at": begin + timedelta(minutes=minutes), "tz": "Europe/Riga",
            "customer_name": extra.pop("customer_name", f"C{number}"), "customer_username": None, "service": "",
            "notes": "", "extras": "", "proposed_by": None, "proposed_starts_at": None, "proposed_ends_at": None,
            **extra}


PAIR = [_row(3, customer_name="Khan", extras="anal, domination +50"),
        _row(4, start="2026-10-20T22:00", customer_name="Marco"),
        _row(6, bs.CONFIRMED, start="2026-10-20T13:00", customer_name="Juris")]


def commands(answers, rows=PAIR, **kw):
    return bookings.owner_commands_from([_answer(**a) for a in answers], rows, now=NOW, tz_name="Europe/Riga", **kw)


def test_one_message_gives_a_command_per_booking_with_what_goes_to_him():
    yes, no = commands([
        {"action": "yes", "number": 3, "duration_minutes": 60, "extras_no": ["anal"], "tell_customer": "bring cash"},
        {"action": "no", "number": 4},
    ])
    assert (yes.kind, yes.number, yes.minutes, yes.tell, yes.extras_no) == ("yes", 3, 60, "bring cash", ("anal",))
    assert (no.kind, no.number, no.has_words) == ("no", 4, False)
    assert bookings.passing_on_words(yes) == " I'll tell him: bring cash; no anal."


def test_a_booking_she_does_not_name_is_the_only_one_that_fits_else_she_is_asked():
    # Two requests wait: which one?
    [cmd] = commands([{"action": "yes"}])
    assert (cmd.kind, cmd.missing, cmd.candidates) == ("unclear", "which", (3, 4))
    # ... unless she quoted one of them.
    [cmd] = commands([{"action": "yes"}], replied=PAIR[1])
    assert (cmd.kind, cmd.number) == ("yes", 4)
    # Words with no booking named, and only one booking at all.
    [cmd] = commands([{"action": "none", "tell_customer": "I'm 10 min late"}], rows=[PAIR[2]])
    assert (cmd.kind, cmd.number, cmd.tell) == ("none", 6, "I'm 10 min late")
    # Cancel fits the confirmed one coming up.
    [cmd] = commands([{"action": "cancel"}])
    assert (cmd.kind, cmd.number) == ("cancel", 6)


def test_a_yes_to_a_confirmed_booking_changes_nothing_but_still_carries_her_words():
    [cmd] = commands([{"action": "yes", "number": 6, "tell_customer": "come alone"}])
    assert (cmd.kind, cmd.tell) == ("stays", "come alone")


def test_another_day_after_an_hour_is_proposed_at_that_hour():
    [cmd] = commands([{"action": "time", "number": 3, "start": "2026-10-21T14:00", "duration_minutes": 60}])
    assert (cmd.kind, cmd.number, cmd.at, cmd.day, cmd.minutes) == ("propose", 3, time(14, 0), date(2026, 10, 21), 60)
    # A time she meant but none could be read: she is asked for it.
    [cmd] = commands([{"action": "time", "number": 3}])
    assert (cmd.kind, cmd.missing) == ("unclear", "time")
    assert bookings.format_question(cmd, PAIR[0]) == "#3 Khan: what time should I offer him?"


def test_two_answers_about_one_booking_are_one_command():
    [cmd] = commands([{"action": "yes", "number": 3, "tell_customer": "bring cash"},
                      {"action": "none", "number": 3, "ask_customer": "does he want 2 hours?", "extras_no": ["anal"]},
                      {"action": "none", "number": 3, "tell_customer": "bring cash", "extras_no": ["anal"]}])
    assert (cmd.kind, cmd.tell, cmd.ask, cmd.extras_no) == ("yes", "bring cash", "does he want 2 hours?", ("anal",))
    # A yes and a no for the same booking: she is asked.
    [cmd] = commands([{"action": "yes", "number": 3}, {"action": "no", "number": 3}])
    assert cmd.kind == "unclear" and cmd.number == 3
    assert bookings.format_question(cmd, PAIR[0]) == "#3 Khan, Tue 21:00: yes or no? Or give another time."
    # LIST once, however often she asks.
    assert [c.kind for c in commands([{"action": "list"}, {"action": "list"}])] == ["list"]


def test_an_outcall_step_while_the_taxi_steps_are_off_changes_nothing_but_she_is_answered():
    rows = [_row(7, bs.CONFIRMED, start="2026-10-20T13:30", notes="Outcall.")]
    [cmd] = commands([{"action": "in_taxi", "number": 7}], rows=rows, outcall_taxi=False)
    assert (cmd.kind, cmd.number, cmd.has_words) == ("none", 7, False)
    [cmd] = commands([{"action": "in_taxi", "number": 7, "tell_customer": "10 min"}], rows=rows, outcall_taxi=False)
    assert (cmd.kind, cmd.tell) == ("none", "10 min")
    # On: READY / IN with no number is left to the outcall steps to place.
    [cmd] = commands([{"action": "ready"}], rows=rows + [_row(8, bs.CONFIRMED, notes="Outcall.")])
    assert (cmd.kind, cmd.number) == ("ready", None)


def test_the_owner_command_for_one_answer_is_the_first_command():
    cmd = bookings.owner_command_from(_answer(action="yes", number=4), PAIR, now=NOW, tz_name="Europe/Riga")
    assert (cmd.kind, cmd.number) == ("yes", 4)
    cmd = bookings.owner_command_from(_answer(action="ready", number=6), PAIR, now=NOW, tz_name="Europe/Riga",
                                      outcall_taxi=False)
    assert (cmd.kind, cmd.number) == ("none", 6)


@pytest.mark.parametrize("text", ["vale", "claro que no", "cancela 7", "CONFIRMO 3", "dale"])
def test_spanish_words_are_read_never_taken_as_strict_commands(text):
    # "claro que no" is "of course not": only the reader of her words decides.
    assert parse(text) is None


@pytest.mark.parametrize("text, kind, number, loose", [
    ("CANCEL 7 tell him sorry I'm sick", "cancel", 7, True), ("DONE 4 all good", "done", 4, True),
    ("NOSHOW 4 never came", "noshow", 4, True), ("CANCEL 7", "cancel", 7, False)])
def test_cancel_done_noshow_with_words_after_them_are_read_too(text, kind, number, loose):
    cmd = parse(text)
    assert (cmd.kind, cmd.number, cmd.loose) == (kind, number, loose)


def test_the_quoted_request_names_the_booking_only_when_her_message_holds_one_answer():
    # "yes. tell the other one I'm 10 min late", quoting #3's request: the
    # words must not land on #3.
    first, second = commands([{"action": "yes", "number": 3}, {"action": "none", "tell_customer": "I'm 10 min late"}],
                             replied=PAIR[0])
    assert (first.kind, first.number, first.tell) == ("yes", 3, "")
    assert (second.kind, second.missing, second.tell) == ("unclear", "which", "I'm 10 min late")
    # One answer: the quoted request is the one.
    [cmd] = commands([{"action": "none", "tell_customer": "bring cash"}], replied=PAIR[1])
    assert (cmd.kind, cmd.number) == ("none", 4)


def test_a_booking_number_the_model_made_up_is_never_acted_on():
    # #12 is not open and she never wrote 12.
    [cmd] = commands([{"action": "cancel", "number": 12}], typed="cancel him, he's sick")
    assert (cmd.kind, cmd.number, cmd.missing) == ("unclear", None, "which")
    # She named it herself: kept (a booking further ahead than the open ones).
    [cmd] = commands([{"action": "cancel", "number": 12}], typed="cancel 12, he's sick")
    assert (cmd.kind, cmd.number) == ("cancel", 12)


def test_her_questions_repeat_what_waits_on_her_answer():
    cmd = bookings.OwnerCommand("unclear", 3, minutes=60, extras_no=("anal",), tell="bring cash")
    assert bookings.format_question(cmd, PAIR[0]) == (
        "#3 Khan, Tue 21:00: yes or no? Or give another time. With your answer: for 1 hour; tell him bring cash; "
        "no anal.")
    which = bookings.OwnerCommand("unclear", missing="which", ask="does he want 2 hours?")
    assert bookings.format_which_one(PAIR[:2], which).endswith(
        "Answer with the number. With your answer: ask him does he want 2 hours?.")


def _typed_number(text):
    """The booking number she typed, as the runtime has it (None: none, or no command)."""
    cmd = parse(text)
    return cmd.number if cmd is not None else None


@pytest.mark.parametrize("text, doubt", [
    ("ok see you", False), ("yes babe", False), ("YES 3 tell him cash", False), ("ok sure let him come", False),
    ("ok, not today", True), ("yes 3 hours", True), ("yes but in 40 min", True), ("ok in 5", True),
    ("yes after 3", True), ("jā, bet tikai stundu", True), ("да, но не сегодня", True), ("yes tomorrow", True),
    ("ok 18:00", True)])
def test_words_that_may_turn_a_yes_into_something_else(text, doubt):
    # As the runtime asks: with the booking number she typed, if any.
    assert bookings.loose_doubt(text, "yes", _typed_number(text)) is doubt


def test_her_last_word_on_an_extra_wins():
    line = bookings.booker_news_line([{"extras_no": ["anal"]}, {"extras_ok": ["Anal"], "extras_no": ["rimming"]}])
    assert "fine for them: Anal" in line and "NOT on offer for them: rimming" in line
    assert "NOT on offer for them: anal" not in line.replace("rimming", "")


# --------------------------------- what she is told, and what goes to him

def test_her_acknowledgements_say_back_what_was_understood():
    confirmed = _row(3, bs.CONFIRMED, minutes=90, customer_name="Khan")
    assert bookings.format_acknowledgement(confirmed, "confirm") == (
        "#3 confirmed ✅ for Tue 20 Oct 2026, 21:00–22:30 (1 h 30 min) — Khan will be told.")
    words = bookings.OwnerCommand("none", 3, tell="I'm 10 min late", extras_ok=("domination",),
                                  ask="does he want 2 hours")
    assert bookings.format_words_ack(confirmed, words) == (
        "#3 Khan: I'll tell him: I'm 10 min late; yes to domination. I'll ask him: does he want 2 hours? "
        "His answer will come here.")
    assert bookings.passing_on_words(bookings.OwnerCommand("yes", 3)) == ""


@pytest.mark.parametrize("minutes, text", [(60, "1 hour"), (120, "2 hours"), (90, "1 h 30 min"), (45, "45 min")])
def test_lengths_in_words(minutes, text):
    assert bookings.describe_minutes(minutes) == text


def test_her_questions_are_answered_from_the_booking():
    facts = bookings.format_facts(_row(3, customer_name="Khan", extras="anal, domination +50", extras_no="anal",
                                       notes="Hotel Radisson. Outcall.", minutes=120))
    assert facts.split("\n")[:3] == [
        "#3 Khan — Tue 20 Oct 2026, 21:00–23:00 (2 hours), outcall.",
        "Waiting for your answer.",
        "Extras he asked for: anal, domination +50.",
    ]
    assert "You said yes to: -; no to: anal." in facts and "Notes: Hotel Radisson. Outcall." in facts
    arrived = bookings.format_facts(_row(6, bs.CONFIRMED, start="2026-10-20T12:45", customer_name="Juris",
                                         arrived_at=NOW))
    assert "incall." in arrived and "Confirmed. He says he has arrived." in arrived


def test_which_one_lists_the_bookings_that_fit():
    assert bookings.format_which_one(PAIR[:2]) == (
        "Which one: #3 Khan, Tue 21:00, or #4 Marco, Tue 22:00? Answer with the number.")
    assert bookings.format_which_one([]) == "Which booking is that about? Send LIST to see them."
    # Her own question from the reader, tied to the booking.
    assert bookings.format_question(bookings.OwnerCommand("unclear", 3, question="1 or 2 hours?"), PAIR[0]) == \
        "#3: 1 or 2 hours?"


def test_when_her_words_cannot_be_read_she_is_shown_the_commands():
    assert bookings.read_failed([3]) == (
        "I couldn't read that just now — answer with YES 3 / NO 3, or a time like 3 15:30.")
    assert "YES <number>" in bookings.read_failed([3, 4])
    assert bookings.format_standing_saved("cash only") == (
        'Saved for the admin to add to your rules: "cash only". Nothing changes until they accept it.')


def test_her_words_reach_him_as_her_own_never_as_someone_elses():
    cmd = bookings.OwnerCommand("yes", 3, tell="bring cash", ask="does he want 2 hours?", extras_no=("anal",))
    item = bookings.booker_news_item(cmd, at=NOW)
    assert item["tell"] == "bring cash" and item["extras_no"] == ["anal"] and len(item["id"]) == 32
    assert item["at"] == NOW.isoformat()
    line = bookings.booker_news_line(
        [item, {"tell": "", "ask": "", "extras_ok": ["domination"], "extras_no": ["anal"]}])
    assert line.startswith(
        'NEWS TO PASS ON IN THIS REPLY: tell them "bring cash"; these extras are fine for them: domination; '
        'these extras are NOT on offer for them: anal; ask them "does he want 2 hours?".')
    assert "first person" in line and "never 'let me check'" in line and "no booker" in line
    assert bookings.booker_news_line([{"tell": "", "ask": "", "extras_ok": [], "extras_no": []}]) == ""


def test_refused_extras_stay_refused():
    assert bookings.refused_extras_line([]) == ""
    line = bookings.refused_extras_line(["anal", "rimming"])
    assert "anal, rimming" in line and "Never offer, agree to or price these" in line
    assert bookings.extras_joined("anal, Domination", ["domination", " rimming ", ""]) == "anal, Domination, rimming"
    assert bookings.extras_list(" anal ,, rimming") == ["anal", "rimming"]


def test_the_reader_is_told_her_own_wall_clock_whatever_clock_it_is_given():
    # The runtime's clock is UTC: 09:40 UTC is 12:40 in Riga.
    prompt = bookings.owner_answer_prompt("them: at 8", "Europe/Riga", now=NOW.astimezone(timezone.utc),
                                          state_note="", latest="at 8")
    assert prompt.startswith("Current date and time: Tuesday 2026-10-20 12:40 (Europe/Riga).")


def test_the_reader_is_told_how_long_and_what_she_already_decided():
    note = bookings.owner_state_note([
        _row(3, customer_name="Khan", minutes=120, extras="anal", extras_ok="domination", extras_no="anal",
             booker_asked_at=NOW),
        _row(6, bs.CONFIRMED, start="2026-10-20T12:45", customer_name="Juris", arrived_at=NOW),
    ])
    first, second = note.split("\n")
    assert "(2 hours)" in first and "extras they said yes to: domination" in first
    assert "extras they refused: anal" in first and "they asked him something" in first
    assert second.endswith("(1 hour); incall, he says he has arrived")


# ----------------------------------------- round 2: nothing she writes lost


@pytest.mark.parametrize("text, kind, number, loose", [
    ("yes 2 hours", "yes", None, True), ("ok 30 min", "yes", None, True), ("no 1 hour", "no", None, True),
    ("да 2 часа", "yes", None, True), ("jā 1 stundu", "yes", None, True), ("YES 2", "yes", 2, False),
    ("yes 2 tell him cash", "yes", 2, True)])
def test_a_length_after_yes_or_no_is_never_a_booking_number(text, kind, number, loose):
    cmd = parse(text)
    assert (cmd.kind, cmd.number, cmd.loose) == (kind, number, loose)


@pytest.mark.parametrize("text, kind, doubt", [
    ("yes at 9", "yes", True), ("ok 9pm", "yes", True), ("yes but at 21", "yes", True),
    ("ok, half an hour later", "yes", True), ("да, через полчаса", "yes", True), ("jā, pusstundu vēlāk", "yes", True),
    ("sí, a las 9", "yes", True), ("sí, media hora más tarde", "yes", True), ("yes, earlier if he can", "yes", True),
    ("NO 3 sorry not today", "no", False), ("NO 3 tomorrow at 15 instead", "no", True),
    ("CANCEL 3 tell him sorry", "cancel", False), ("CANCEL 3 move him to 18:00", "cancel", True),
    ("yes see you", "yes", False), ("ok sure", "yes", False)])
def test_what_may_turn_each_typed_command(text, kind, doubt):
    assert bookings.loose_doubt(text, kind, _typed_number(text)) is doubt


@pytest.mark.parametrize("raw, action, extra", [
    # Another word for "none" keeps what it carries ...
    ('{"answers": [{"number": 3, "action": "tell", "duration_minutes": 120}]}', "none", {"duration_minutes": 120}),
    ('{"answers": [{"number": null, "action": "extend", "duration_minutes": 120}]}', "none",
     {"duration_minutes": 120}),
    ('{"answers": [{"number": 6, "action": "relay", "start": "2026-10-20T13:30"}]}', "none",
     {"start": "2026-10-20T13:30"}),
    # ... and with only a question for her, or only the booking, she is asked.
    ('{"answers": [{"number": 1, "action": "question", "question": "Today or tomorrow?"}]}', "unclear",
     {"question": "Today or tomorrow?"}),
    ('{"answers": [{"number": 1, "action": "ask"}]}', "unclear", {}),
    # The real "none" with a time alone is kept for the time.
    ('{"answers": [{"number": 6, "action": "none", "in_minutes": 10}]}', "none", {"in_minutes": 10}),
])
def test_an_answer_in_other_words_keeps_what_it_carries(raw, action, extra):
    [answer] = bookings.parse_owner_answer(raw).answers
    assert answer["action"] == action
    assert all(answer[key] == value for key, value in extra.items())


def test_a_yes_with_another_time_asks_her_rather_than_confirming_his():
    [cmd] = commands([{"action": "yes", "number": 3, "start": "2026-10-20T23:00", "tell_customer": "cash"}])
    assert (cmd.kind, cmd.number, cmd.tell) == ("unclear", 3, "cash")
    assert bookings.format_question(cmd, PAIR[0]).startswith(
        "#3 Khan asked for Tue 21:00: confirm that (YES 3), or offer him 23:00 (3 23:00)?")
    # By his time: a yes.
    [cmd] = commands([{"action": "yes", "number": 3, "start": "2026-10-20T21:00"}])
    assert (cmd.kind, cmd.number) == ("yes", 3)
    # Another day: the date is in the command she is offered.
    [cmd] = commands([{"action": "yes", "number": 3, "start": "2026-10-21T14:00"}])
    assert "(3 21.10 14:00)" in bookings.format_question(cmd, PAIR[0])


def test_words_alone_with_a_time_ask_her_what_she_meant():
    [cmd] = commands([{"action": "none", "number": 6, "start": "2026-10-20T13:30"}])
    assert cmd.kind == "unclear"
    assert bookings.format_question(cmd, PAIR[2]).startswith(
        "#6 Juris is booked for Tue 13:00: offer him 13:30 instead (6 13:30), or say what to tell him.")
    # With words for him the words are what she meant: the time is in them.
    [cmd] = commands([{"action": "none", "number": 6, "start": "2026-10-20T13:30", "tell_customer": "come 13:30"}])
    assert (cmd.kind, cmd.tell) == ("none", "come 13:30")


def test_ready_before_his_confirmed_time_keeps_her_time_for_her_line():
    [cmd] = commands([{"action": "ready_at", "number": 6, "in_minutes": 10}])
    assert (cmd.kind, cmd.at, cmd.has_words) == ("stays", time(12, 50), False)


Y, N = bookings.OwnerCommand("yes", 3), bookings.OwnerCommand("no", 3)


def test_a_strict_command_answering_her_question_goes_through_as_typed():
    read = bookings.OwnerCommand("yes", 3, minutes=60, extras_no=("anal",))
    # The reading agrees: her earlier length and refusal are added to it.
    plan = bookings.plan_owner_answer(Y, [read])
    assert plan.commands == (replace(Y, minutes=60, extras_no=("anal",)),) and not plan.earlier_lost
    # She typed NO: the reading's yes never confirms; her earlier words fall away.
    plan = bookings.plan_owner_answer(N, [read])
    assert plan.commands == (N,) and plan.earlier_lost
    # Nothing in the reading for her booking: as typed, never asked again.
    plan = bookings.plan_owner_answer(Y, [], held_words=True)
    assert plan.commands == (Y,) and plan.earlier_lost and plan.ask is None
    plan = bookings.plan_owner_answer(Y, [])
    assert plan.commands == (Y,) and not plan.earlier_lost
    # A typed time keeps its own time.
    typed = bookings.OwnerCommand("propose", 3, at=time(15, 30))
    plan = bookings.plan_owner_answer(typed, [bookings.OwnerCommand("propose", 3, at=time(16, 0), tell="cash")])
    assert plan.commands == (replace(typed, tell="cash"),)
    # A plain YES without a number: the one booking the reading agrees on, else which.
    plan = bookings.plan_owner_answer(bookings.OwnerCommand("yes"), [bookings.OwnerCommand("none", 4, tell="cash")])
    assert plan.commands == (bookings.OwnerCommand("yes", 4, tell="cash"),)
    assert bookings.plan_owner_answer(bookings.OwnerCommand("yes"), []).ask == 0


def test_a_loose_command_is_the_readings_unless_it_misses_or_turns_her_booking():
    loose = bookings.OwnerCommand("yes", 3, loose=True)
    read = bookings.OwnerCommand("yes", 3, tell="cash")
    assert bookings.plan_owner_answer(loose, [read]).commands == (read,)
    assert bookings.plan_owner_answer(loose, [bookings.OwnerCommand("yes", 4)]).ask == 3
    assert bookings.plan_owner_answer(loose, [bookings.OwnerCommand("no", 3)]).ask == 3
    assert bookings.plan_owner_answer(replace(loose, kind="no"), [read]).ask == 3
    # Her loose yes read as a new time is the reading's (a condition of hers).
    proposal = bookings.OwnerCommand("propose", 3, at=time(15, 0))
    assert bookings.plan_owner_answer(loose, [proposal]).commands == (proposal,)
    # Read as about no booking: asked, never dropped in silence.
    assert bookings.plan_owner_answer(bookings.OwnerCommand("no", loose=True), []).ask == 0
    assert bookings.plan_owner_answer(bookings.OwnerCommand("no", loose=True), [], standing="cash only").commands == ()
    # Not a command at all: every command of the reading.
    assert bookings.plan_owner_answer(None, [read]).commands == (read,)


def test_a_typed_yes_to_a_confirmed_booking_answering_a_question_keeps_her_words():
    read = bookings.OwnerCommand("none", 6, tell="bring cash")
    plan = bookings.plan_owner_answer(bookings.OwnerCommand("yes", 6), [read], settled={6})
    assert plan.commands == (bookings.OwnerCommand("stays", 6, tell="bring cash"),)


def test_she_is_told_when_the_reading_agrees_but_drops_what_she_said_before():
    # "yes, tell him to bring cash" -> which one? -> "YES 3", read as a plain yes 3.
    plain = bookings.OwnerCommand("yes", 3)
    assert bookings.plan_owner_answer(Y, [plain], held_words=True).earlier_lost
    assert not bookings.plan_owner_answer(Y, [replace(plain, tell="cash")], held_words=True).earlier_lost
    assert not bookings.plan_owner_answer(Y, [plain]).earlier_lost
    # The same for a plain YES without a number.
    plan = bookings.plan_owner_answer(bookings.OwnerCommand("yes"), [plain], held_words=True)
    assert plan.commands == (plain,) and plan.earlier_lost


# ------------------------------------- from the first run against the real model


@pytest.mark.parametrize("typed, kinds", [
    ("yes i confirm", [("unclear", None)]),           # names neither: asked which
    ("both fine", [("yes", 3), ("yes", 4)]),
    ("оба норм", [("yes", 3), ("yes", 4)]),
    ("los dos bien", [("yes", 3), ("yes", 4)]),
    ("yes 3 and 4", [("yes", 3), ("yes", 4)]),
    ("yes to Khan and Marco", [("yes", 3), ("yes", 4)]),
    ("yes to Khan", [("yes", 3), ("unclear", 4)]),    # the other one: yes or no?
])
def test_a_yes_to_several_waiting_bookings_needs_them_named(typed, kinds):
    got = commands([{"action": "yes", "number": 3}, {"action": "yes", "number": 4}], typed=typed)
    assert [(c.kind, c.number) for c in got] == kinds
    if kinds == [("unclear", None)]:
        assert (got[0].missing, got[0].candidates) == ("which", (3, 4))


def test_one_waiting_booking_and_a_confirmed_one_are_not_asked_about():
    # A yes to the waiting one and a "stays" for the confirmed one: nothing to ask.
    got = commands([{"action": "yes", "number": 3}, {"action": "yes", "number": 6}], typed="yes i confirm")
    assert [(c.kind, c.number) for c in got] == [("yes", 3), ("stays", 6)]


def test_a_remark_about_one_customer_is_no_rule_for_all():
    rows = [_row(6, bs.CONFIRMED, customer_name="Juris")]
    assert bookings.standing_for_all("after today don't take Juris again", rows) == ""
    assert bookings.standing_for_all("from now on cash only", rows) == "from now on cash only"
    assert bookings.standing_for_all("", rows) == ""


# --------------------------------------------- round 3: the second review


@pytest.mark.parametrize("text, number", [
    ("jā 2 st.", None), ("sí 2 hs", None), ("yes 2 hours.", None), ("yes 9 pm", None), ("да 2 ч.", None),
    ("ok 30 min.", None), ("да 9 вечера", None),
    # "минет" is no unit: the number stays hers.
    ("да 1, минет можно", 1), ("YES 2 tell him cash", 2),
])
def test_a_length_or_time_of_day_after_yes_is_no_booking_number(text, number):
    assert parse(text).number == number


def test_with_commands_only_a_number_is_a_booking_number_as_always():
    cmd = bookings.parse_owner_reply("yes 2 hours", today=TODAY, lengths=False)
    assert (cmd.kind, cmd.number) == ("yes", 2)
    cmd = bookings.parse_owner_reply("no 2 hours", today=TODAY, lengths=False)
    assert (cmd.kind, cmd.number) == ("no", 2)


@pytest.mark.parametrize("text, kind, doubt", [
    ("si, manana", "yes", True), ("ja, rit", "yes", True), ("ja, bet velak", "yes", True),
    ("ok, around 9", "yes", True), ("да, к 9", "yes", True), ("si 2hs", "yes", True), ("yes tonight", "yes", True),
    ("ok friday", "yes", True), ("да, на 9", "yes", True), ("jā, uz 9", "yes", True), ("sí, para las 9", "yes", True),
    ("sí, en 10", "yes", True), ("да 9 вечера", "yes", True), ("yes 1, 2 hours", "yes", True),
    ("nē 1, rit", "no", True), ("cancel 6 move to friday", "cancel", True),
    ("ok no problem", "yes", False), ("sí, no hay problema", "yes", False), ("да, не вопрос", "yes", False),
    ("ok don't worry", "yes", False), ("ok sure", "yes", False), ("NO 3 sorry not today", "no", False),
])
def test_the_doubt_check_in_all_four_languages(text, kind, doubt):
    assert bookings.loose_doubt(text, kind, _typed_number(text)) is doubt


@pytest.mark.parametrize("raw, action", [
    ('{"answers": [{"number": 4, "action": "no-show"}]}', "noshow"),
    ('{"answers": [{"number": 4, "action": "cancelled"}]}', "cancel"),
    ('{"answers": [{"number": 4, "action": "none", "question": "today or tomorrow?"}]}', "unclear"),
])
def test_odd_answers_in_round_three(raw, action):
    assert bookings.parse_owner_answer(raw).answers[0]["action"] == action


def test_a_no_or_cancel_with_another_time_offers_that_time():
    [cmd] = commands([{"action": "no", "number": 3, "start": "2026-10-21T15:00"}])
    assert (cmd.kind, cmd.at, cmd.day) == ("propose", time(15, 0), date(2026, 10, 21))
    [cmd] = commands([{"action": "cancel", "number": 6, "start": "2026-10-20T17:00"}])
    assert (cmd.kind, cmd.at) == ("propose", time(17, 0))


def test_a_yes_at_an_earlier_clock_time_is_asked_not_his_time_confirmed():
    [cmd] = commands([{"action": "yes", "number": 3, "start": "2026-10-20T20:00"}])
    assert cmd.kind == "unclear" and "(3 20:00)" in bookings.format_question(cmd, PAIR[0])
    # Ready a little before his time by a delay: a yes.
    [cmd] = commands([{"action": "yes", "number": 3, "in_minutes": 5}],
                     rows=[_row(3, start="2026-10-20T12:50", customer_name="Khan")])
    assert cmd.kind == "yes"


def test_the_question_about_a_move_he_asked_names_the_time_he_asked_for():
    moved = _row(5, bs.CONFIRMED, start="2026-10-20T16:00", customer_name="Toms", proposed_by=bs.CUSTOMER,
                 proposed_starts_at=bookings.parse_local("2026-10-20T17:00", "Europe/Riga"),
                 proposed_ends_at=bookings.parse_local("2026-10-20T18:00", "Europe/Riga"))
    [cmd] = commands([{"action": "yes", "number": 5, "start": "2026-10-20T18:00"}], rows=[moved])
    assert bookings.format_question(cmd, moved).startswith(
        "#5 Toms asked for Tue 17:00: confirm that (YES 5), or offer him 18:00 (5 18:00)?")


def test_a_finished_booking_is_told_as_finished_and_comes_after_live_ones():
    done = _row(4, bs.COMPLETED, start="2026-10-20T11:00", customer_name="Peter", proposed_by=bs.OWNER,
                proposed_starts_at=NOW, proposed_ends_at=NOW + timedelta(hours=1))
    assert bookings.owner_state_note([done]).startswith("#4 Peter: done (it was for")
    cancelled = _row(9, bs.CANCELLED, start="2026-10-21T15:00", customer_name="Ivo")
    [cmd] = commands([{"action": "none", "tell_customer": "I'm 10 min late"}], rows=[PAIR[2], cancelled])
    assert (cmd.kind, cmd.number) == ("none", 6)


W = {3, 4}


def test_a_plain_strict_yes_with_two_waiting_and_no_open_question_is_asked_which():
    plan = bookings.plan_owner_answer(bookings.OwnerCommand("yes"), [bookings.OwnerCommand("yes", 3)], waiting=W)
    assert plan.ask == 0 and plan.commands == ()
    # With a question open about #4 only the answer for #4 counts.
    plan = bookings.plan_owner_answer(bookings.OwnerCommand("no"), [bookings.OwnerCommand("none", 3, tell="late"),
                                                                    bookings.OwnerCommand("no", 4)],
                                      waiting=W, focus={4})
    assert plan.commands == (bookings.OwnerCommand("no", 4),)


def test_a_typed_no_to_his_question_on_a_confirmed_booking_is_her_words_not_a_decline():
    plan = bookings.plan_owner_answer(bookings.OwnerCommand("no"),
                                      [bookings.OwnerCommand("none", 6, tell="no friends, alone only")],
                                      waiting={4}, settled={6}, focus={6})
    assert plan.commands == (bookings.OwnerCommand("none", 6, tell="no friends, alone only"),)


def test_a_loose_cancel_read_as_words_only_still_cancels():
    typed = bookings.OwnerCommand("cancel", 6, loose=True)
    plan = bookings.plan_owner_answer(typed, [bookings.OwnerCommand("none", 6, tell="sorry, I'm sick")],
                                      waiting=set(), settled={6})
    assert plan.commands == (bookings.OwnerCommand("cancel", 6, tell="sorry, I'm sick"),)


def test_asking_about_one_booking_still_carries_out_the_others():
    typed = bookings.OwnerCommand("yes", 4, loose=True)
    late = bookings.OwnerCommand("none", 6, tell="I'm 10 min late")
    plan = bookings.plan_owner_answer(typed, [bookings.OwnerCommand("no", 4, minutes=120), late], waiting=W)
    assert (plan.ask, plan.commands) == (4, (late,))
    assert plan.ask_with == bookings.OwnerCommand("no", 4, minutes=120)
