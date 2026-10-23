"""The words around bookings: the owner's typed answers, what is read out
of the model's extraction, and the lines the reply writer is given."""

from __future__ import annotations

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


@pytest.mark.parametrize("raw, expected", [
    ('{"action": "yes", "number": 8, "start": null, "in_minutes": null}',
     {"action": "yes", "number": 8, "start": None, "in_minutes": None}),
    ('Sure: ```{"action": "ready_at", "number": "#8", "in_minutes": 20.0}```',
     {"action": "ready_at", "number": 8, "start": None, "in_minutes": 20}),
    ('{"action": "time", "number": null, "start": "2026-10-20T15:30"}',
     {"action": "time", "number": None, "start": "2026-10-20T15:30", "in_minutes": None}),
    ('{"action": "ready_at", "number": 0, "in_minutes": -5}',
     {"action": "ready_at", "number": None, "start": None, "in_minutes": None}),
])
def test_the_owner_answer_is_cleaned(raw, expected):
    assert bookings.parse_owner_answer(raw) == expected


@pytest.mark.parametrize("raw", ['{"action": "none"}', '{"action": "maybe"}', "yes", "", '{"action": '])
def test_no_owner_answer_when_not_about_a_booking_or_unusable(raw):
    assert bookings.parse_owner_answer(raw) is None


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
    assert lines[0] == ("#7 Anna: the customer asks for Sun 04 Oct 2026, 15:00–16:00, waiting for their answer; "
                        "extras: domination +50")
    assert lines[1].startswith("#8 Khan: confirmed for Sun 04 Oct 2026, 15:00–16:00; outcall: send READY 8")
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
