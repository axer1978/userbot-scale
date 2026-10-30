"""What the bot says to the booker (booker_texts): the main messages, word
for word as before in English, the account's own versions of them, her
language, and the checks on what the account writes."""

from __future__ import annotations

import re
import string
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import booker_texts as bt
import booking_states as bs
import bookings

RIGA = ZoneInfo("Europe/Riga")
START = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)  # Sun 15:00 in Riga
MOVE = datetime(2026, 10, 5, 13, 0, tzinfo=timezone.utc)  # Mon 16:00 in Riga


def _b(**extra):
    return {"number": 7, "state": bs.PENDING, "starts_at": START, "ends_at": START + timedelta(hours=1),
            "tz": "Europe/Riga", "customer_name": "Anna", "customer_username": "anna", "service": "haircut",
            "notes": "Outcall. hotel", "extras": "kissing", "proposed_by": None, "proposed_starts_at": None,
            "proposed_ends_at": None, **extra}


def _moving(**extra):
    return _b(**{"state": bs.CONFIRMED, "proposed_by": bs.CUSTOMER, "proposed_starts_at": MOVE,
                 "proposed_ends_at": MOVE + timedelta(hours=1), **extra})


# Each main message with the default texts in English, and what it said
# before booker_texts, word for word.
ENGLISH_TODAY = {
    "request": (
        lambda t: bookings.format_request(_b(), changed=True, name="Lola", t=t),
        "📅 Booking request #7 (new time)\nCustomer: Anna (@anna)\nGirl: Lola\nExtras: kissing\n"
        "When: Sun 04 Oct 2026, 15:00–16:00\nWhat: haircut\nNotes: Outcall. hotel\n\n"
        "Reply YES 7 or NO 7, or a new time like 7 15:30 or 7 04.10 15:30."),
    "move_request": (
        lambda t: bookings.format_move_request(_moving(), name="Lola", t=t),
        "🔁 Lola — #7: Anna (@anna) asks to move their booking\nFrom: Sun 04 Oct 2026, 15:00–16:00\n"
        "To:   Mon 05 Oct 2026, 16:00–17:00\n\n"
        "Reply YES 7 to move it, NO 7 to keep the old time, or a different time like 7 15:30."),
    "confirmed": (
        lambda t: bookings.format_acknowledgement(_b(state=bs.CONFIRMED), "confirm", t=t),
        "#7 confirmed ✅ for Sun 04 Oct 2026, 15:00–16:00 (1 hour) — Anna will be told."),
    "declined": (
        lambda t: bookings.format_acknowledgement(_b(state=bs.CANCELLED), "decline", t=t),
        "#7 declined ❌ — Anna will be told."),
    "proposed": (
        lambda t: bookings.format_acknowledgement(_moving(proposed_by=bs.OWNER), "propose", t=t),
        "#7: Mon 05 Oct 2026, 16:00–17:00 proposed to Anna. You'll hear back when they answer."),
    "moved": (
        lambda t: bookings.format_acknowledgement(_b(state=bs.CONFIRMED), "reschedule", t=t),
        "#7 moved to Sun 04 Oct 2026, 15:00–16:00 ✅ — Anna will be told."),
    "cancelled": (
        lambda t: bookings.format_acknowledgement(_b(state=bs.CANCELLED), "cancel", t=t),
        "#7 cancelled ❌ — Anna will be told."),
    "customer_cancelled": (
        lambda t: bookings.format_customer_cancelled(_b(state=bs.CANCELLED), name="Lola", t=t),
        "Lola — #7 Anna (@anna), Sun 04 Oct 2026, 15:00–16:00: cancelled by the customer."),
    "arrived": (
        lambda t: bookings.format_arrived(_b(state=bs.CONFIRMED), "I'm at the door", way_in_sent=False,
                                          name="Lola", t=t),
        "Lola — #7 Anna (@anna), Sun 04 Oct 2026, 15:00–16:00: he's here — go and meet him.\nI'm at the door"),
    "ready_reminder": (
        lambda t: bookings.format_ready_reminder(_b(state=bs.CONFIRMED), t=t),
        "🚕 #7 Anna (@anna), Sun 04 Oct 2026, 15:00–16:00 (outcall): send READY 7 when you're ready — then he "
        "orders the taxi."),
}


def test_every_template_is_covered():
    assert set(ENGLISH_TODAY) == set(bt.TEMPLATES) == set(bt.DEFAULTS["en"])


@pytest.mark.parametrize("key", sorted(ENGLISH_TODAY))
def test_english_defaults_say_what_was_said_before(key):
    make, today = ENGLISH_TODAY[key]
    assert make(bt.ENGLISH) == today
    # The default argument is English too.
    assert make(bt.Texts()) == today


def test_the_old_ways_of_saying_them_still_agree():
    b = _b(state=bs.CANCELLED)
    assert bookings.format_customer_cancelled(b, name="Lola") == \
        bookings.format_owner_update(b, "cancelled by the customer.", name="Lola")
    assert bookings.format_arrived(_b(), "", way_in_sent=True) == \
        bookings.format_owner_update(_b(), "he's here — the way in was sent to him.")
    # Nothing he said: no empty line after it.
    assert not bookings.format_arrived(_b(), "", way_in_sent=False).endswith("\n")
    assert bookings.ANSWER_HELP.format(n=3) == bt.ENGLISH.say("answer_help", n=3)
    assert bookings.READ_NOTHING_FAILED == bt.ENGLISH.say("read_nothing_failed")


def test_every_default_passes_its_own_check():
    for lang, texts in bt.DEFAULTS.items():
        assert set(texts) == set(bt.TEMPLATES), lang
        for key, text in texts.items():
            assert bt.check(key, text) == [], (lang, key)


# ------------------------------------------------- the account's own texts


def test_the_accounts_own_text_is_used():
    t = bt.Texts("en", {"confirmed": "Done #{number}, {customer} {when} ({length})"})
    assert bookings.format_acknowledgement(_b(state=bs.CONFIRMED), "confirm", t=t) == \
        "Done #7, Anna Sun 04 Oct 2026, 15:00–16:00 (1 hour)"
    # Only that one: the others stay the default.
    assert bookings.format_acknowledgement(_b(), "decline", t=t) == ENGLISH_TODAY["declined"][1]


def test_a_line_whose_blanks_are_all_empty_is_left_out():
    t = bt.Texts("en", {"request": "#{number} {customer}\nGirl: {girl}\nAbout: {service}{notes}\nfixed line\n{commands}"})
    text = bookings.format_request(_b(service="", notes=""), t=t)
    assert text == "#7 Anna\nfixed line\nReply YES 7 or NO 7, or a new time like 7 15:30 or 7 04.10 15:30."
    # One blank filled is enough to keep it.
    assert "About: haircut" in bookings.format_request(_b(notes=""), t=t)
    assert "Girl: Mia" in bookings.format_request(_b(), name="Mia", t=t)


def test_the_number_and_the_commands_are_always_kept():
    t = bt.Texts("en", {"request": "New one from {customer}", "confirmed": "OK, {customer} is in."})
    assert bookings.format_request(_b(), t=t) == (
        "#7 New one from Anna\n\nReply YES 7 or NO 7, or a new time like 7 15:30 or 7 04.10 15:30.")
    # A message without a commands line gets none added.
    assert bookings.format_acknowledgement(_b(), "confirm", t=t) == "#7 OK, Anna is in."
    assert bookings.format_ready_reminder(_b(), t=bt.Texts("en", {"ready_reminder": "#{number} go"})) == (
        "#7 go\n\nsend READY 7 when you're ready — then he orders the taxi.")


@pytest.mark.parametrize("bad", ["Done {nope}", "Done {number", "Done {when:>9}", "Done {0}", "x" * 1001])
def test_a_text_that_does_not_pass_the_check_falls_back_to_the_default(bad):
    t = bt.Texts("en", {"confirmed": bad})
    assert bookings.format_acknowledgement(_b(), "confirm", t=t) == ENGLISH_TODAY["confirmed"][1]
    # In her language, the default of her language.
    ru = bt.Texts("ru", {"confirmed": bad})
    assert bookings.format_acknowledgement(_b(), "confirm", t=ru).startswith("#7 подтверждена ✅ на ")


def test_texts_of_the_settings():
    t = bt.Texts.of({"booker_language": "de", "booker_messages": {
        "confirmed": "   ", "declined": "No #{number}", "moved": None, "cancelled": ""}})
    assert t.lang == "en" and dict(t.custom) == {"declined": "No #{number}"}
    assert bt.Texts.of({}) == bt.Texts()
    assert bt.Texts.of({"booker_language": None, "booker_messages": None}) == bt.Texts()
    assert bt.Texts.of({"booker_language": "lv"}).lang == "lv"


# ---------------------------------------------------------------- check


def test_check_finds_what_is_wrong():
    assert bt.check("confirmed", "") == []
    assert bt.check("confirmed", "#{number} ok {{braces}}") == []
    [unknown] = bt.check("confirmed", "{nope}")
    assert unknown.startswith("{nope} is not a blank of this message") and "{number}" in unknown
    assert bt.check("confirmed", "{when:>9}") == ["write the blank as {when}, with nothing after its name"]
    assert bt.check("confirmed", "{when!r}") == ["write the blank as {when}, with nothing after its name"]
    assert bt.check("confirmed", "{0}")[0].startswith("{0} is not a blank")
    assert bt.check("confirmed", "{a.b}")[0].startswith("{a.b} is not a blank")
    assert bt.check("confirmed", "{}") == ["{} needs a name inside, e.g. {number}"]
    assert bt.check("confirmed", "{number")[0].startswith("a { or } without its pair")
    assert bt.check("confirmed", "number}")[0].startswith("a { or } without its pair")
    assert bt.check("confirmed", "x" * 1001) == ["too long (1001 characters; at most 1000)"]
    assert bt.check("nope", "x") == ["there is no message called 'nope'"]
    assert bt.check("confirmed", 5) == ["must be text"]
    # The same problem twice is said once.
    assert len(bt.check("confirmed", "{x} {x}")) == 1
    assert bt.check_all({"confirmed": "{x}", "declined": "ok"}) == [("confirmed", bt.check("confirmed", "{x}")[0])]


# ------------------------------------------------------- her language


@pytest.mark.parametrize("lang,minutes", [
    ("en", ["1 hour", "2 hours", "5 hours", "21 hours", "1 h 30 min", "45 min"]),
    ("ru", ["1 час", "2 часа", "5 часов", "21 час", "1 ч 30 мин", "45 мин"]),
    ("lv", ["1 stunda", "2 stundas", "5 stundas", "21 stunda", "1 st. 30 min", "45 min"]),
    ("es", ["1 hora", "2 horas", "5 horas", "21 horas", "1 h 30 min", "45 min"]),
])
def test_lengths_in_her_language(lang, minutes):
    t = bt.Texts(lang)
    assert [t.minutes(m) for m in (60, 120, 300, 1260, 90, 45)] == minutes
    if lang == "ru":
        assert [t.minutes(m * 60) for m in (11, 12, 14, 22, 25)] == ["11 часов", "12 часов", "14 часов", "22 часа",
                                                                     "25 часов"]
    if lang == "en":
        # English is describe_minutes, as before.
        assert [t.minutes(m) for m in range(5, 13 * 60, 5)] == \
            [bookings.describe_minutes(m) for m in range(5, 13 * 60, 5)]


@pytest.mark.parametrize("lang,same_day,overnight", [
    ("en", "Tue 20 Oct 2026, 21:00–22:00", "Tue 20 Oct 2026, 23:00 – Wed 21 Oct 01:30"),
    ("ru", "вт, 20 окт 2026, 21:00–22:00", "вт, 20 окт 2026, 23:00 – ср, 21 окт 01:30"),
    ("lv", "otrd., 20. okt. 2026, 21:00–22:00", "otrd., 20. okt. 2026, 23:00 – trešd., 21. okt. 01:30"),
    ("es", "mar 20 oct 2026, 21:00–22:00", "mar 20 oct 2026, 23:00 – mié 21 oct 01:30"),
])
def test_days_and_times_in_her_language(lang, same_day, overnight):
    t = bt.Texts(lang)
    assert t.span(datetime(2026, 10, 20, 21, 0, tzinfo=RIGA), datetime(2026, 10, 20, 22, 0, tzinfo=RIGA)) == same_day
    assert t.span(datetime(2026, 10, 20, 23, 0, tzinfo=RIGA), datetime(2026, 10, 21, 1, 30, tzinfo=RIGA)) == overnight


def test_english_days_are_describe_span():
    for hours in range(0, 24 * 40, 5):
        start = START + timedelta(hours=hours)
        for length in (30, 60, 600):
            end = start + timedelta(minutes=length)
            assert bookings.span_for(bt.ENGLISH, start, end, "Europe/Riga") == \
                bookings.describe_span(start, end, "Europe/Riga")


def test_a_message_in_her_language():
    ru = bt.Texts("ru")
    assert bookings.format_acknowledgement(_b(state=bs.CONFIRMED), "confirm", t=ru) == \
        "#7 подтверждена ✅ на вс, 4 окт 2026, 15:00–16:00 (1 час) — Anna узнает об этом."
    assert ru.say("list_nothing") == "Сейчас ничего не ждёт твоего ответа."


def test_a_fixed_line_with_no_translation_is_said_in_english(monkeypatch):
    monkeypatch.setitem(bt.TEXTS, "only_english", {"en": "#{n} only in English"})
    assert bt.Texts("ru").say("only_english", n=3) == "#3 only in English"
    # One that can't be filled in her language is said in English too.
    monkeypatch.setitem(bt.TEXTS, "broken", {"en": "#{n} ok", "lv": "#{nope}"})
    assert bt.Texts("lv").say("broken", n=3) == "#3 ok"


def test_rule_errors_in_her_language(monkeypatch):
    message = "cannot confirm booking #3: it is cancelled"
    assert bt.ENGLISH.rule_error(message) == message
    assert bt.Texts("ru").rule_error(message) == "бронь #3: нельзя подтвердить — она отменена"
    assert bt.Texts("lv").rule_error(message) == "rezervācija #3: nevar apstiprināt, jo tā ir atcelta"
    assert bt.Texts("es").rule_error(message) == "reserva #3: no se puede confirmar porque está cancelada"
    assert bt.Texts("ru").rule_error("something new") == "something new"
    # A word of the rules with no translation goes as it is.
    assert bt.Texts("es").rule_error("cannot fly booking #3: it is lost") == \
        "reserva #3: no se puede fly porque lost"
    monkeypatch.setitem(bt.RULE_ERRORS, r"cannot (?P<action>.+) booking #(?P<number>\d+): it is (?P<state>\w+)",
                        {"en": "cannot {action} booking #{number}: it is {state}", "ru": "#{number}: {state}"})
    assert bt.Texts("ru").rule_error(message) == "#3: отменена"
    assert bt.Texts("lv").rule_error(message) == message  # no text in her language: as it is
    assert bt.ENGLISH.rule_error(message) == message


# ------------------------------------------------------- texts, preview


def test_every_text_has_english_and_its_blanks_are_well_formed():
    for key, forms in bt.TEXTS.items():
        assert forms.get("en"), key
        for lang, text in forms.items():
            assert lang in bt.LANGUAGES, (key, lang)
            list(string.Formatter().parse(text))  # no unpaired brace


def test_the_preview_shows_every_main_message():
    now = datetime(2026, 10, 20, 12, 0, tzinfo=RIGA)
    shown = bookings.preview_messages(bt.ENGLISH, tz="Europe/Riga", now=now)
    assert set(shown) == set(bt.TEMPLATES)
    assert all("#3" in text for text in shown.values())
    assert shown["request"].startswith("📅 Booking request #3\nCustomer: Alex (@alex_m)\nGirl: Mia\n"
                                       "Extras: kissing, shower together\nWhen: Wed 21 Oct 2026, 21:00–22:00\n")
    assert shown["move_request"].split("\n")[2] == "To:   Thu 22 Oct 2026, 20:00–21:00"
    assert shown["arrived"].endswith("the way in was sent to him.\nI'm at the door 🙂")
    # The account's own text, and her language.
    own = bookings.preview_messages(bt.Texts("en", {"confirmed": "Yes #{number} {customer}"}), tz="Europe/Riga",
                                    now=now, name="Lola")
    assert own["confirmed"] == "Yes #3 Alex" and "Girl: Lola" in own["request"]
    ru = bookings.preview_messages(bt.Texts("ru"), tz="Europe/Riga", now=now)
    assert ru["request"].startswith("📅 Запрос на бронь #3\nКлиент: Alex (@alex_m)\nДевушка: Mia\n")
    assert "ср, 21 окт 2026, 21:00–22:00" in ru["request"]


# ------------------------------------------- Russian, Latvian, Spanish


OTHER = ("ru", "lv", "es")


def _blank_names(text):
    return {name for _, name, _, _ in string.Formatter().parse(text) if name is not None}


def _edges(text):
    """The spaces a text starts and ends with (some are glued onto others)."""
    return text[:len(text) - len(text.lstrip())], text[len(text.rstrip()):]


@pytest.mark.parametrize("lang", OTHER)
def test_every_text_is_in_her_language_with_the_same_blanks(lang):
    for key, forms in bt.TEXTS.items():
        assert forms.get(lang), (lang, key)
        assert _blank_names(forms[lang]) == _blank_names(forms["en"]), (lang, key)
        assert _edges(forms[lang]) == _edges(forms["en"]), (lang, key)
        # Said in her language, not left in English.
        if any(c.isalpha() for c in forms["en"].replace("{", " ").split("}")[-1]) and key not in (
                "owner_update", "list_request", "list_outcall", "passed_on", "words_ack", "facts_head",
                "ack_other"):
            assert forms[lang] != forms["en"], (lang, key)


@pytest.mark.parametrize("lang", OTHER)
def test_her_defaults_pass_the_check_and_have_the_english_blanks(lang):
    assert set(bt.DEFAULTS[lang]) == set(bt.TEMPLATES)
    for key, text in bt.DEFAULTS[lang].items():
        assert bt.check(key, text) == [], (lang, key)
        assert _blank_names(text) == _blank_names(bt.DEFAULTS["en"][key]), (lang, key)
        assert _edges(text) == _edges(bt.DEFAULTS["en"][key]), (lang, key)


def _sample(text):
    """Made-up values for every blank of `text`."""
    values = {name: "x" for name in _blank_names(text)}
    values.update({k: v for k, v in {"n": 3, "number": "3", "minutes": 10, "kind": "YES",
                                     "offer": "3 16:00"}.items() if k in values})
    return values


@pytest.mark.parametrize("lang", ("en",) + OTHER)
def test_every_text_can_be_said(lang, caplog):
    t = bt.Texts(lang)
    for key, forms in bt.TEXTS.items():
        said = t.say(key, **_sample(forms["en"]))
        assert said == forms[lang].format(**_sample(forms["en"])), (lang, key)
    assert "can't be filled" not in caplog.text


# The command words she is shown, per language: the ones parse_owner_reply
# reads in it; English where it has none (Spanish READY, LIST, ...).
COMMAND_WORDS = {
    "en": {"yes": "YES", "no": "NO", "ready": "READY", "in_taxi": "IN", "list": "LIST", "cancel": "CANCEL",
           "done": "DONE", "noshow": "NOSHOW"},
    "ru": {"yes": "ДА", "no": "НЕТ", "ready": "ГОТОВА", "in_taxi": "В ТАКСИ", "list": "СПИСОК", "cancel": "ОТМЕНА",
           "done": "ГОТОВО", "noshow": "НЕЯВКА"},
    "lv": {"yes": "JĀ", "no": "NĒ", "ready": "GATAVA", "in_taxi": "TAKSĪ", "list": "SARAKSTS", "cancel": "ATCELT",
           "done": "NOTIKA", "noshow": "NEIERADĀS"},
    "es": {"yes": "SÍ", "no": "NO", "ready": "READY", "in_taxi": "TAXI", "list": "LIST", "cancel": "CANCEL",
           "done": "DONE", "noshow": "NOSHOW"},
}
_ARG = r"(\d+|n(?!\w)|<[^>]+>)"
_TIME_EXAMPLE = re.compile(r"(?<![\w#.:<])" + _ARG + r" ((?:\d\d\.\d\d )?\d\d:\d\d)(?![\w:])")


def _commands(text, lang):
    """Every command shown in `text` ("YES 3", "ДА <номер>", "LIST", "3 15:30",
    "n 04.10 15:30"), as (what it should be, what she would type)."""
    words = COMMAND_WORDS[lang]
    found = []
    by_word = {word: kind for kind, word in words.items()}
    alternatives = "|".join(re.escape(w) for w in sorted(by_word, key=len, reverse=True))
    for m in re.finditer(r"(?<!\w)(" + alternatives + r")(?: " + _ARG + r")?(?![\w:])", text):
        word, arg = m.group(1), m.group(2)
        kind = by_word[word]
        if arg is None and kind != "list":
            continue
        number = int(arg) if arg and arg.isdigit() else 3
        found.append((kind, number, word if arg is None else f"{word} {number}"))
    for m in _TIME_EXAMPLE.finditer(text):
        number = int(m.group(1)) if m.group(1).isdigit() else 3
        found.append(("propose", number, f"{number} {m.group(2)}"))
    return found


def _with_commands(lang):
    """Each text with a command in it, filled ({kind} with her word for yes:
    `which` gets the command word she typed)."""
    out = {}
    for key, forms in bt.TEXTS.items():
        values = _sample(forms["en"])
        if "kind" in values:
            values["kind"] = COMMAND_WORDS[lang]["yes"]
        out[key] = forms[lang].format(**values)
    return out


def test_the_english_texts_show_commands_where_expected():
    shown = {key for key, text in _with_commands("en").items() if _commands(text, "en")}
    assert {"answer_help", "move_commands", "ready_reminder_commands", "list_commands", "ack_outcall_ready",
            "ack_outcall_proof", "taxi_link_in", "step_taxi_link", "step_wait_proof", "step_send_ready",
            "which", "which_about", "changed_meanwhile", "read_failed", "read_rest_failed", "nothing_moved",
            "nothing_confirmed", "ask_confirm_or_offer", "ask_offer_or_confirm", "ask_offer_instead_booked",
            "news_in_taxi_late"} <= shown


@pytest.mark.parametrize("lang", ("en",) + OTHER)
def test_every_command_she_is_shown_is_one_the_bot_reads(lang):
    today = datetime(2026, 10, 3).date()
    english = _with_commands("en")
    for key, text in _with_commands(lang).items():
        found = _commands(text, lang)
        # The same commands as in English, none lost or added.
        assert sorted(k for k, _, _ in found) == sorted(k for k, _, _ in _commands(english[key], "en")), \
            (lang, key, text)
        for kind, number, typed in found:
            cmd = bookings.parse_owner_reply(typed, today=today)
            assert cmd is not None, (lang, key, typed)
            assert cmd.kind == kind, (lang, key, typed, cmd)
            assert cmd.number == (None if kind == "list" else number), (lang, key, typed, cmd)
            if kind == "propose":
                assert cmd.at is not None and not cmd.error, (lang, key, typed, cmd)
            # Typed in small letters, the same.
            assert bookings.parse_owner_reply(typed.lower(), today=today) == cmd, (lang, key, typed)


@pytest.mark.parametrize("lang", ("ru", "lv"))
def test_russian_and_latvian_show_their_own_command_words(lang):
    english_only = re.compile(r"(?<!\w)(YES|NO|READY|IN|LIST|CANCEL|DONE|NOSHOW)(?!\w)")
    for key, forms in bt.TEXTS.items():
        assert not english_only.search(forms[lang]), (lang, key)
    for key, text in bt.DEFAULTS[lang].items():
        assert not english_only.search(text), (lang, key)


def test_spanish_shows_si_for_yes():
    for key, forms in bt.TEXTS.items():
        assert "YES" not in forms["es"], key


def test_the_main_messages_in_her_language_carry_her_commands():
    ru, lv, es = (bt.Texts(lang) for lang in OTHER)
    assert bookings.format_request(_b(), t=ru).endswith(
        "Ответь ДА 7 или НЕТ 7 либо напиши новое время, например: 7 15:30 или 7 04.10 15:30.")
    assert bookings.format_move_request(_moving(), t=lv).endswith(
        "Atbildi JĀ 7, lai pārceltu, NĒ 7, lai paliktu vecais laiks, vai citu laiku, piem., 7 15:30.")
    assert bookings.format_ready_reminder(_b(state=bs.CONFIRMED), t=es) == (
        "🚕 #7 Anna (@anna), dom 4 oct 2026, 15:00–16:00 (salida): envía READY 7 cuando estés lista — "
        "entonces él pide el taxi.")


@pytest.mark.parametrize("lang,head", [
    ("en", "📅 Booking request #3\nCustomer: Alex (@alex_m)\nGirl: Mia\n"),
    ("ru", "📅 Запрос на бронь #3\nКлиент: Alex (@alex_m)\nДевушка: Mia\n"),
    ("lv", "📅 Rezervācijas pieprasījums #3\nKlients: Alex (@alex_m)\nMeitene: Mia\n"),
    ("es", "📅 Solicitud de reserva #3\nCliente: Alex (@alex_m)\nChica: Mia\n"),
])
def test_the_preview_in_each_language(lang, head):
    now = datetime(2026, 10, 20, 12, 0, tzinfo=RIGA)
    shown = bookings.preview_messages(bt.Texts(lang), tz="Europe/Riga", now=now)
    assert set(shown) == set(bt.TEMPLATES)
    assert all("#3" in text for text in shown.values()), shown
    assert shown["request"].startswith(head)
    if lang != "en":
        # Nothing English left in it but the names and what he wrote.
        english = bookings.preview_messages(bt.ENGLISH, tz="Europe/Riga", now=now)
        assert all(shown[key] != english[key] for key in shown)


@pytest.mark.parametrize("lang,june,length", [
    ("en", "Tue 01 Jun 2027", ["11 hours", "1 h 5 min"]),
    ("ru", "вт, 1 июня 2027", ["11 часов", "1 ч 5 мин"]),
    ("lv", "otrd., 1. jūn. 2027", ["11 stundas", "1 st. 5 min"]),
    ("es", "mar 1 jun 2027", ["11 horas", "1 h 5 min"]),
])
def test_more_days_and_lengths(lang, june, length):
    t = bt.Texts(lang)
    assert t.day(datetime(2027, 6, 1, 12, 0)) == june
    assert [t.minutes(11 * 60), t.minutes(65)] == length


@pytest.mark.parametrize("lang", OTHER)
def test_rule_errors_have_her_language_with_the_same_blanks(lang):
    for pattern, forms in bt.RULE_ERRORS.items():
        assert forms.get(lang), (lang, pattern)
        assert _blank_names(forms[lang]) == _blank_names(forms["en"]) == set(re.compile(pattern).groupindex), \
            (lang, pattern)


def _rule_words(source, pattern):
    return set(re.findall(pattern, source))


@pytest.mark.parametrize("lang", OTHER)
def test_every_word_the_rules_use_is_translated(lang):
    import inspect
    source = inspect.getsource(bs)
    words = bt._RULE_WORDS[lang]
    assert _rule_words(source, r'_require\(\s*booking, "([^"]+)"') <= set(words["action"])
    assert _rule_words(source, r'_after_start\(booking, now, "([^"]+)"\)') <= set(words["action"])
    assert {bs.REQUESTED, bs.PENDING, bs.CONFIRMED, bs.CANCELLED, bs.NO_SHOW, bs.COMPLETED} <= set(words["state"])
    assert {bs.OWNER, bs.CUSTOMER, bs.ADMIN, bs.SYSTEM} <= set(words["actor"])
    assert _rule_words(source, r'_future\([^\n]*?"([^"]+)"\)') <= set(words["what"])


@pytest.mark.parametrize("message,ru,lv,es", [
    ("owner cannot reschedule a booking", "ты не можешь перенести", "tu nevari pārcelt", "no puedes cambiar de hora"),
    ("the new time is in the past", "новое время — уже в прошлом", "jaunais laiks jau ir pagātnē",
     "la nueva hora ya pasó"),
    ("cannot mark as missed booking #4 before it starts", "бронь #4: нельзя отметить неявку до её начала",
     "rezervācija #4: nevar atzīmēt neierašanos, pirms tā sākas",
     "reserva #4: no se puede marcar que no vino antes de que empiece"),
    ("25:00 is not a time", "25:00 — это не время", "25:00 nav laiks", "25:00 no es una hora"),
    ("no time given (use HH:MM)", "не указано время (пиши ЧЧ:ММ)", "nav norādīts laiks (raksti SS:MM)",
     "falta la hora (usa HH:MM)"),
])
def test_rule_errors_read_in_her_language(message, ru, lv, es):
    assert [bt.Texts(lang).rule_error(message) for lang in OTHER] == [ru, lv, es]
    assert bt.ENGLISH.rule_error(message) == message


def test_real_rule_errors_come_out_in_her_language():
    """The messages booking_states really raises, through rule_error: none
    left in English."""
    now = START - timedelta(days=1)
    cases = [
        lambda: bs.confirm(_b(state=bs.CANCELLED), actor=bs.OWNER, now=now),
        lambda: bs.mark_no_show(_b(state=bs.CONFIRMED), actor=bs.OWNER, now=now),
        lambda: bs.confirm(_b(starts_at=now - timedelta(hours=1)), actor=bs.OWNER, now=now),
    ]
    for case in cases:
        with pytest.raises(bs.IllegalTransition) as caught:
            case()
        message = str(caught.value)
        for lang in OTHER:
            said = bt.Texts(lang).rule_error(message)
            assert said != message and "booking" not in said and "cannot" not in said, (lang, message, said)


@pytest.mark.parametrize("lang", sorted(bt.LANGUAGES))
@pytest.mark.parametrize("kind, parsed", [("yes", "yes"), ("no", "no"), ("cancel", "cancel"), ("done", "done"),
                                          ("noshow", "noshow"), ("ready", "ready"), ("in", "in_taxi")])
def test_the_command_word_shown_in_her_language_is_one_she_can_type(lang, kind, parsed):
    t = bt.Texts(lang)
    typed = bookings.parse_owner_reply(f"{t.command(kind)} 7", today=datetime(2030, 3, 4).date())
    assert typed is not None and (typed.kind, typed.number) == (parsed, 7)
    assert t.command(kind) in bookings.format_which(kind, t=t)
