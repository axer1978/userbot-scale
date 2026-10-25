"""The words of the messages the bot sends the booker (booking.provider),
in her language (booking.booker_language), with the main ones rewritten by
the account if it wants (booking.booker_messages).

Two kinds of text live here:

- TEMPLATES: the main messages (a new request, "he wants to move it",
  confirmed, ...). Each has blanks ({number}, {customer}, {when}, ...) and a
  default per language; the account may replace it with its own text. A
  line whose blanks are all empty is left out ("Girl: {girl}" when she has
  no name). The booking number and the commands line are always kept: a
  text without them gets them added. One that can't be used (it should
  not pass check(), but whatever happens) falls back to the default.
- TEXTS: every other line to her (questions, acknowledgements, "couldn't
  read that"): fixed, only translated.

Commands shown to her in another language use words parse_owner_reply
understands in that language (ДА 3, JĀ 3, SÍ 3), so she can copy them.

Nothing here touches the database or the network.
"""

from __future__ import annotations

import logging
import re
import string
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional

log = logging.getLogger(__name__)

LANGUAGES: dict[str, str] = {"en": "English", "ru": "Русский", "lv": "Latviešu", "es": "Español"}
DEFAULT_LANGUAGE = "en"
MAX_TEMPLATE = 1000


@dataclass(frozen=True)
class Template:
    """One message the account may rewrite: its name in the panel, what it
    is for, its blanks, and whether a commands line ({commands}) belongs to
    it (then it is always kept)."""
    label: str
    about: str
    blanks: tuple[str, ...]
    commands: bool = False


# What each blank holds, for the panel (with an example).
BLANKS: dict[str, str] = {
    "number": "the booking number, e.g. 3",
    "customer": "the customer's name, e.g. Alex",
    "handle": "his @username in brackets with a space before it, e.g. \" (@alex_m)\"; empty when he has none",
    "girl": "her name (booking.name, else the account's), e.g. Mia; empty when there is none",
    "girl_tag": "her name and a dash, e.g. \"Mia — \"; empty when there is none",
    "extras": "the extras he asked for, e.g. kissing, shower",
    "when": "the day and time, e.g. Tue 20 Oct 2026, 21:00–22:00",
    "length": "how long, e.g. 1 hour",
    "to": "the time he wants to move it to",
    "proposed": "the time you offered him",
    "service": "what he asked for, when it is noted; empty otherwise",
    "notes": "notes on the booking; empty when there are none",
    "changed": "\" (new time)\" when he changed the time before you answered; empty otherwise",
    "said": "what he wrote when he arrived; empty when nothing",
    "way_in": "whether the way in was sent to him, or she goes out to meet him",
    "commands": "how to answer (YES 3 / NO 3 / a new time) — always kept",
}

TEMPLATES: dict[str, Template] = {
    "request": Template(
        "New booking request", "A customer asks for a time (or changed it before you answered).",
        ("number", "customer", "handle", "girl", "girl_tag", "extras", "when", "length", "service", "notes",
         "changed", "commands"), commands=True),
    "move_request": Template(
        "He wants to move it", "The customer asks to move a confirmed booking.",
        ("number", "customer", "handle", "girl", "girl_tag", "when", "length", "to", "commands"), commands=True),
    "confirmed": Template(
        "Confirmed", "Her answer back after she confirms (or takes the time he asked to move to).",
        ("number", "customer", "when", "length")),
    "declined": Template("Declined", "Her answer back after she says no.", ("number", "customer", "when", "length")),
    "proposed": Template(
        "New time offered", "Her answer back after she offers him another time.",
        ("number", "customer", "proposed", "when")),
    "moved": Template("Moved", "Her answer back after the booking is moved.", ("number", "customer", "when", "length")),
    "cancelled": Template(
        "Cancelled", "Her answer back after she cancels.", ("number", "customer", "when", "length")),
    "customer_cancelled": Template(
        "Cancelled by him", "The customer cancelled his booking.",
        ("number", "customer", "handle", "girl", "girl_tag", "when", "length")),
    "arrived": Template(
        "He has arrived", "The customer says he is at the door (incall).",
        ("number", "customer", "handle", "girl", "girl_tag", "when", "length", "said", "way_in")),
    "ready_reminder": Template(
        "Outcall: send READY", "Before an outcall: a reminder to say when she is ready.",
        ("number", "customer", "handle", "girl", "girl_tag", "when", "length", "commands"), commands=True),
}

# The default text of each template, per language. English is today's text,
# word for word.
DEFAULTS: dict[str, dict[str, str]] = {
    "en": {
        "request": ("📅 Booking request #{number}{changed}\nCustomer: {customer}{handle}\nGirl: {girl}\n"
                    "Extras: {extras}\nWhen: {when}\nWhat: {service}\nNotes: {notes}\n\n{commands}"),
        "move_request": ("🔁 {girl_tag}#{number}: {customer}{handle} asks to move their booking\nFrom: {when}\n"
                         "To:   {to}\n\n{commands}"),
        "confirmed": "#{number} confirmed ✅ for {when} ({length}) — {customer} will be told.",
        "declined": "#{number} declined ❌ — {customer} will be told.",
        "proposed": "#{number}: {proposed} proposed to {customer}. You'll hear back when they answer.",
        "moved": "#{number} moved to {when} ✅ — {customer} will be told.",
        "cancelled": "#{number} cancelled ❌ — {customer} will be told.",
        "customer_cancelled": "{girl_tag}#{number} {customer}{handle}, {when}: cancelled by the customer.",
        "arrived": "{girl_tag}#{number} {customer}{handle}, {when}: he's here — {way_in}\n{said}",
        "ready_reminder": "🚕 #{number} {customer}{handle}, {when} (outcall): {commands}",
    },
    "ru": {
        "request": ("📅 Запрос на бронь #{number}{changed}\nКлиент: {customer}{handle}\nДевушка: {girl}\n"
                    "Доп. услуги: {extras}\nКогда: {when}\nУслуга: {service}\nЗаметки: {notes}\n\n{commands}"),
        "move_request": ("🔁 {girl_tag}#{number}: {customer}{handle} просит перенести бронь\nСейчас: {when}\n"
                         "На: {to}\n\n{commands}"),
        "confirmed": "#{number} подтверждена ✅ на {when} ({length}) — {customer} узнает об этом.",
        "declined": "#{number} отклонена ❌ — {customer} узнает об этом.",
        "proposed": "#{number}: {customer} — предложено {proposed}. Его ответ придёт сюда.",
        "moved": "#{number} перенесена на {when} ✅ — {customer} узнает об этом.",
        "cancelled": "#{number} отменена ❌ — {customer} узнает об этом.",
        "customer_cancelled": "{girl_tag}#{number} {customer}{handle}, {when}: клиент отменил бронь.",
        "arrived": "{girl_tag}#{number} {customer}{handle}, {when}: он на месте — {way_in}\n{said}",
        "ready_reminder": "🚕 #{number} {customer}{handle}, {when} (выезд): {commands}",
    },
    "lv": {
        "request": ("📅 Rezervācijas pieprasījums #{number}{changed}\nKlients: {customer}{handle}\n"
                    "Meitene: {girl}\nPapildpakalpojumi: {extras}\nKad: {when}\nPakalpojums: {service}\n"
                    "Piezīmes: {notes}\n\n{commands}"),
        "move_request": ("🔁 {girl_tag}#{number}: {customer}{handle} lūdz pārcelt rezervāciju\nTagad: {when}\n"
                         "Uz: {to}\n\n{commands}"),
        "confirmed": "#{number} apstiprināta ✅ uz {when} ({length}) — {customer} to uzzinās.",
        "declined": "#{number} atteikta ❌ — {customer} to uzzinās.",
        "proposed": "#{number}: {customer} — piedāvāts {proposed}. Viņa atbilde atnāks šurp.",
        "moved": "#{number} pārcelta uz {when} ✅ — {customer} to uzzinās.",
        "cancelled": "#{number} atcelta ❌ — {customer} to uzzinās.",
        "customer_cancelled": "{girl_tag}#{number} {customer}{handle}, {when}: klients atcēla rezervāciju.",
        "arrived": "{girl_tag}#{number} {customer}{handle}, {when}: viņš ir klāt — {way_in}\n{said}",
        "ready_reminder": "🚕 #{number} {customer}{handle}, {when} (izbraukums): {commands}",
    },
    "es": {
        "request": ("📅 Solicitud de reserva #{number}{changed}\nCliente: {customer}{handle}\nChica: {girl}\n"
                    "Extras: {extras}\nCuándo: {when}\nServicio: {service}\nNotas: {notes}\n\n{commands}"),
        "move_request": ("🔁 {girl_tag}#{number}: {customer}{handle} pide cambiar su reserva\nDe: {when}\n"
                         "A:  {to}\n\n{commands}"),
        "confirmed": "#{number} confirmada ✅ para {when} ({length}) — {customer} será avisado.",
        "declined": "#{number} rechazada ❌ — {customer} será avisado.",
        "proposed": "#{number}: {customer} — hora propuesta: {proposed}. Su respuesta llegará aquí.",
        "moved": "#{number} cambiada a {when} ✅ — {customer} será avisado.",
        "cancelled": "#{number} cancelada ❌ — {customer} será avisado.",
        "customer_cancelled": "{girl_tag}#{number} {customer}{handle}, {when}: el cliente canceló.",
        "arrived": "{girl_tag}#{number} {customer}{handle}, {when}: ya llegó — {way_in}\n{said}",
        "ready_reminder": "🚕 #{number} {customer}{handle}, {when} (salida): {commands}",
    },
}

# Every other line to her: key -> language -> text with {named} blanks.
# English is required; a language without the key says it in English.
# The other languages are added from _TRANSLATIONS below.
# Pieces that are joined into a longer line (a sentence added after
# another) carry no leading space: the code puts it there.
TEXTS: dict[str, dict[str, str]] = {
    # --- the parts of the main messages
    "unknown_customer": {"en": "Unknown customer"},
    "the_customer": {"en": "the customer"},
    "unknown": {"en": "unknown"},
    "request_new_time": {"en": " (new time)"},
    "request_no_extras": {"en": "none mentioned"},
    "answer_help": {"en": "Reply YES {n} or NO {n}, or a new time like {n} 15:30 or {n} 04.10 15:30."},
    "move_commands": {"en": "Reply YES {n} to move it, NO {n} to keep the old time, or a different time like "
                            "{n} 15:30."},
    "owner_update": {"en": "{girl_tag}#{n} {who}, {when}: {what}"},
    "way_in_sent": {"en": "the way in was sent to him."},
    "way_in_meet": {"en": "go and meet him."},
    "ready_reminder_commands": {"en": "send READY {n} when you're ready — then he orders the taxi."},
    # --- her answer back (format_acknowledgement), besides the templates
    "ack_change_length": {"en": "#{n} now {when} ({length}) ✅ — {who} will be told."},
    "ack_reject_proposal": {"en": "#{n} stays at {when} — {who} will be told."},
    "ack_mark_completed": {"en": "#{n} marked as done."},
    "ack_mark_no_show": {"en": "#{n} marked as missed."},
    "ack_ready": {"en": "#{n} ready ✅ — {who} is asked to order the taxi to your address and send its link. It "
                        "will come here."},
    "ack_in_taxi": {"en": "#{n} in the taxi ✅ — {who} is asked for the address, floor, way in and a photo of the "
                          "place. They will come here."},
    "ack_other": {"en": "#{n} {action}"},
    "ack_outcall_proof": {"en": "It's an outcall: {who} is asked for a photo that shows he's at the place (hotel "
                                "card or the building). It comes here, then he orders your taxi. READY {n} skips "
                                "the photo."},
    "ack_outcall_ready": {"en": "It's an outcall: send READY {n} when you're ready, and {who} is told to order the "
                                "taxi."},
    "from_panel": {"en": "(from the panel)"},
    # --- where an outcall's taxi steps stand
    "step_in_taxi": {"en": "in the taxi"},
    "step_taxi_link": {"en": "taxi link sent — send IN {n} when you're in"},
    "step_proof_came": {"en": "his photo came — waiting for his taxi link"},
    "step_ready": {"en": "ready — waiting for his taxi link"},
    "step_wait_proof": {"en": "waiting for his photo of the place (READY {n} skips it)"},
    "step_send_ready": {"en": "send READY {n} when you're ready"},
    # --- LIST
    "list_nothing": {"en": "Nothing is waiting for your answer right now."},
    "list_waiting": {"en": "Waiting for your answer:"},
    "list_move": {"en": "#{n} — {who} wants to move {when} → {to}"},
    "list_proposed": {"en": "#{n} — {who}, you proposed {proposed} (waiting for them)"},
    "list_request": {"en": "#{n} — {who}, {when}"},
    "list_commands": {"en": "Reply YES <number>, NO <number>, or <number> <new time>."},
    "list_outcalls": {"en": "Outcalls:"},
    "list_outcall": {"en": "#{n} — {who}, {when}: {step}"},
    # --- what he sends, passed on to her
    "taxi_link": {"en": "🚕 #{n} {who} sent the taxi link:\n{text}"},
    "taxi_link_in": {"en": "Send IN {n} when you're in the taxi (if you don't, he is told you're in it in "
                           "{minutes} min)."},
    "proof_header": {"en": "📷 #{n} {who} sent his photo of the place (below). He's asked to order your taxi "
                           "now; the link will come here."},
    "passed_on": {"en": "💬 #{n} {who}:\n{text}"},
    "photo_header": {"en": "📷 #{n} {who} sent a photo:"},
    "photo_not_fetched": {"en": "[a photo that could not be fetched — it may be view once; {again}]"},
    "photo_again_asked": {"en": "he is asked to send it again"},
    "photo_again_ask": {"en": "ask him to send it again"},
    # --- news she did not make herself (format_owner_update's `what`)
    "news_accept_proposal": {"en": "the customer took the time you proposed — confirmed."},
    "news_reschedule": {"en": "the customer took the new time — moved."},
    "news_reject_proposal_cancelled": {"en": "the customer turned down the time you proposed. The booking is "
                                             "cancelled."},
    "news_reject_proposal_stays": {"en": "the customer turned down the time you proposed. It stays as it was."},
    "news_lapsed": {"en": "lapsed — it was not answered in time."},
    "news_arrived_photo_ok": {"en": "has arrived (their photo matches the entrance); the way in was sent."},
    "news_arrived_photo_bad": {"en": "sent an arrival photo that does not look like the entrance — check the "
                                     "chat."},
    "news_told_in_taxi": {"en": "he was told you're in the taxi{late}. His address, floor and photos will come "
                                "here."},
    "news_in_taxi_late": {"en": "(no IN {n} within {minutes} min of the taxi link)"},
    # --- questions to her
    "which": {"en": "Which booking? Add its number, e.g. {kind} 7. Send LIST to see them."},
    "which_about": {"en": "Which booking is that about? Send LIST to see them."},
    "which_one": {"en": "Which one: {listed}? Answer with the number."},
    "or_last": {"en": "{items}, or {last}"},
    "question_time": {"en": "#{n} {who}: what time should I offer him?"},
    "question_yes_no": {"en": "#{n} {who}, {when}: yes or no? Or give another time."},
    "ask_confirm_or_offer": {"en": "#{n} {who} asked for {his}: confirm that (YES {n}), or offer him {offered} "
                                   "({offer})?"},
    "ask_offer_instead_booked": {"en": "#{n} {who} is booked for {his}: offer him {offered} instead ({offer}), "
                                       "or say what to tell him."},
    "ask_offer_or_confirm": {"en": "#{n} {who} asked for {his}: offer him {offered} instead ({offer}), or "
                                   "confirm his time (YES {n})?"},
    "held": {"en": "With your answer: {items}."},
    "held_for": {"en": "for {length}"},
    "held_tell": {"en": "tell him {words}"},
    "held_ask": {"en": "ask him {words}"},
    "held_lost": {"en": "#{n}: what you wrote with it before{what} was not passed on with this — send it again "
                        "if it still matters."},
    # --- her words for him, said back to her
    "extra_yes": {"en": "yes to {extra}"},
    "extra_no": {"en": "no {extra}"},
    "words_tell": {"en": "I'll tell him: {items}."},
    "words_ask": {"en": "I'll ask him: {question}"},
    "words_ack": {"en": "#{n} {who}:"},
    "answer_comes": {"en": "His answer will come here."},
    "words_wait": {"en": "His chat is not being answered right now, so this waits; it is dropped if it can't go "
                         "within a day."},
    "words_after_quiet_hours": {"en": "His replies wait for the quiet hours to end (about {until}), so this goes "
                                      "to him then."},
    "words_dropped": {"en": "#{n} {who}: this did not reach him in time and was dropped: {said}. Tell him "
                            "yourself if it still matters."},
    # --- her question about a booking, answered (format_facts)
    "facts_head": {"en": "#{n} {who} — {when} ({length}), {where}."},
    "facts_outcall": {"en": "outcall"},
    "facts_incall": {"en": "incall"},
    "facts_you_proposed": {"en": "You proposed {proposed}; waiting for him."},
    "facts_waiting": {"en": "Waiting for your answer."},
    "facts_confirmed": {"en": "Confirmed."},
    "facts_move": {"en": "He asks to move it to {to} — waiting for your answer."},
    "facts_you_proposed_move": {"en": "You proposed moving it to {proposed}; waiting for him."},
    "facts_outcall_step": {"en": "Outcall: {step}."},
    "facts_arrived": {"en": "He says he has arrived."},
    "facts_cancelled": {"en": "Cancelled."},
    "facts_completed": {"en": "Done."},
    "facts_no_show": {"en": "He didn't come."},
    "facts_extras": {"en": "Extras he asked for: {extras}."},
    "facts_none": {"en": "none"},
    "facts_extras_said": {"en": "You said yes to: {ok}; no to: {no}."},
    "facts_service": {"en": "What: {service}"},
    "facts_notes": {"en": "Notes: {notes}"},
    "facts_asked": {"en": "You asked him something; his answers come here."},
    # --- her words could not be read
    "read_failed": {"en": "I couldn't read that just now — answer with YES {n} / NO {n}, or a time like {n} 15:30."},
    "number_placeholder": {"en": "<number>"},
    "read_rest_failed": {"en": "I couldn't read the rest of your message just now: only that went through, and "
                               "nothing was passed on to him. Send the rest again, or use YES n / NO n / n 15:30."},
    "earlier_not_applied": {"en": "What you wrote before this did not fit it, so only this went through and "
                                  "nothing from before went to him. Send that part again if it still matters."},
    "read_nothing_failed": {"en": "I couldn't read that just now — nothing was saved or passed on. Please send "
                                  "it again in a few minutes."},
    "read_earlier_failed": {"en": "I couldn't read this together with your message before it just now: only "
                                  "this went through, nothing else from that message. Send that part again if "
                                  "it still matters."},
    # --- a standing instruction of hers
    "standing_saved": {"en": "Saved for the admin to add to your rules: \"{text}\". Nothing changes until they "
                             "accept it."},
    "standing_failed": {"en": "I couldn't save \"{text}\" for the admin just now — please tell them yourself."},
    # --- what became of her command
    "stays": {"en": "#{n} stays as it is: {when}."},
    "ready_at_note": {"en": "You'll be ready at {at}; nothing went to him — say \"tell him ...\" if he should "
                            "know."},
    "slot_taken": {"en": "#{n}: that time now overlaps another booking."},
    "length_taken": {"en": "#{n}: that length now overlaps another booking."},
    "changed_meanwhile": {"en": "#{n} changed meanwhile; send LIST to see where things stand."},
    "not_possible": {"en": "#{n}: {what} is not possible ({why})."},
    "length_stays": {"en": "It stays {when}."},
    "nothing_moved": {"en": "Nothing moved; YES {n} takes it as he asked: {when}."},
    "nothing_confirmed": {"en": "Nothing confirmed; YES {n} takes it as he asked: {when}."},
    "failed_n": {"en": "#{n}: something went wrong ({error}); check the Bookings page."},
    "failed_that": {"en": "That: something went wrong ({error}); check the Bookings page."},
    "no_booking": {"en": "There is no booking #{n}."},
    "not_outcall": {"en": "#{n} is not a confirmed outcall that is still to come."},
    "already_ready": {"en": "#{n}: already marked ready."},
    "already_in_taxi": {"en": "#{n}: he already knows you're in the taxi."},
    # --- a customer who needs a person (session_runtime.escalate)
    "escalation": {"en": "⚠️ {name} needs a person: their message contains “{keyword}”. The bot has stopped "
                         "answering them until the chat is switched back on in the panel.\n\n“{excerpt}”"},
    # --- why a time can't be had (availability's reasons)
    "why_past": {"en": "past"},
    "why_too_soon": {"en": "too soon"},
    "why_too_far": {"en": "too far"},
    "why_closed_day": {"en": "closed day"},
    "why_outside_hours": {"en": "outside hours"},
    "why_conflict": {"en": "conflict"},
    "why_invalid": {"en": "invalid"},
}

# TEXTS in her other languages, one language per block so each reads as a
# whole. The booker is a woman and is spoken to as "ты" / "tu" / "tú"; the
# customer is a man, named in the nominative (no case ending on a name).
# Commands shown use the words parse_owner_reply knows in that language
# (ДА/НЕТ/ГОТОВА/В ТАКСИ/СПИСОК, JĀ/NĒ/GATAVA/TAKSĪ/SARAKSTS, SÍ/NO/TAXI);
# where it knows none (Spanish READY, LIST) the English word is shown.
_TRANSLATIONS: dict[str, dict[str, str]] = {
    "ru": {
        "unknown_customer": "Неизвестный клиент",
        "the_customer": "клиент",
        "unknown": "без имени",
        "request_new_time": " (новое время)",
        "request_no_extras": "не указаны",
        "answer_help": "Ответь ДА {n} или НЕТ {n} либо напиши новое время, например: {n} 15:30 или {n} 04.10 15:30.",
        "move_commands": ("Ответь ДА {n} — перенести, НЕТ {n} — оставить старое время, или другое время, "
                          "например {n} 15:30."),
        "owner_update": "{girl_tag}#{n} {who}, {when}: {what}",
        "way_in_sent": "ему отправили, как войти.",
        "way_in_meet": "выйди его встретить.",
        "ready_reminder_commands": "напиши ГОТОВА {n}, когда будешь готова — тогда он закажет такси.",
        "ack_change_length": "#{n} теперь {when} ({length}) ✅ — {who} узнает об этом.",
        "ack_reject_proposal": "#{n} остаётся на {when} — {who} узнает об этом.",
        "ack_mark_completed": "#{n} — отмечено: встреча была.",
        "ack_mark_no_show": "#{n} — отмечено: он не пришёл.",
        "ack_ready": ("#{n}: ты готова ✅ — {who} получил просьбу заказать такси на твой адрес и прислать "
                      "ссылку. Она придёт сюда."),
        "ack_in_taxi": ("#{n}: ты в такси ✅ — {who} получил просьбу прислать адрес, этаж, как войти и фото "
                        "места. Всё это придёт сюда."),
        "ack_other": "#{n} {action}",
        "ack_outcall_proof": ("Это выезд: {who} получил просьбу прислать фото, что он на месте (карточка отеля "
                              "или здание). Фото придёт сюда, потом он закажет тебе такси. ГОТОВА {n} — без фото."),
        "ack_outcall_ready": ("Это выезд: напиши ГОТОВА {n}, когда будешь готова, и {who} получит просьбу "
                              "заказать такси."),
        "from_panel": "(из панели)",
        "step_in_taxi": "в такси",
        "step_taxi_link": "ссылка на такси пришла — напиши В ТАКСИ {n}, когда сядешь",
        "step_proof_came": "его фото пришло — ждём ссылку на такси",
        "step_ready": "готова — ждём ссылку на такси",
        "step_wait_proof": "ждём его фото места (ГОТОВА {n} — без фото)",
        "step_send_ready": "напиши ГОТОВА {n}, когда будешь готова",
        "list_nothing": "Сейчас ничего не ждёт твоего ответа.",
        "list_waiting": "Ждут твоего ответа:",
        "list_move": "#{n} — {who} хочет перенести {when} → {to}",
        "list_proposed": "#{n} — {who}, ты предложила {proposed} (ждём его ответа)",
        "list_request": "#{n} — {who}, {when}",
        "list_commands": "Ответь ДА <номер>, НЕТ <номер> или <номер> <новое время>.",
        "list_outcalls": "Выезды:",
        "list_outcall": "#{n} — {who}, {when}: {step}",
        "taxi_link": "🚕 #{n} {who} прислал ссылку на такси:\n{text}",
        "taxi_link_in": ("Напиши В ТАКСИ {n}, когда сядешь в такси (если не напишешь, через {minutes} мин ему "
                         "скажут, что ты уже едешь)."),
        "proof_header": ("📷 #{n} {who} прислал фото места (ниже). Его просят сейчас заказать тебе такси; "
                         "ссылка придёт сюда."),
        "passed_on": "💬 #{n} {who}:\n{text}",
        "photo_header": "📷 #{n} {who} прислал фото:",
        "photo_not_fetched": "[фото не удалось получить — возможно, оно одноразовое; {again}]",
        "photo_again_asked": "его попросили прислать ещё раз",
        "photo_again_ask": "попроси его прислать ещё раз",
        "news_accept_proposal": "клиент согласился на время, которое ты предложила, — подтверждено.",
        "news_reschedule": "клиент согласился на новое время — перенесено.",
        "news_reject_proposal_cancelled": "клиент отказался от времени, которое ты предложила. Бронь отменена.",
        "news_reject_proposal_stays": "клиент отказался от времени, которое ты предложила. Всё остаётся как было.",
        "news_lapsed": "срок вышел — вовремя не ответили.",
        "news_arrived_photo_ok": "приехал (фото совпадает со входом); ему отправили, как войти.",
        "news_arrived_photo_bad": "прислал фото по приезде, но оно не похоже на вход — проверь чат.",
        "news_told_in_taxi": "ему сказали, что ты в такси{late}. Его адрес, этаж и фото придут сюда.",
        "news_in_taxi_late": "(не было «В ТАКСИ {n}» за {minutes} мин после ссылки на такси)",
        "which": "Какая бронь? Добавь номер, например: {kind} 7. Напиши СПИСОК, чтобы их увидеть.",
        "which_about": "О какой это брони? Напиши СПИСОК, чтобы их увидеть.",
        "which_one": "Какая: {listed}? Ответь номером.",
        "or_last": "{items} или {last}",
        "question_time": "#{n} {who}: какое время ему предложить?",
        "question_yes_no": "#{n} {who}, {when}: да или нет? Или напиши другое время.",
        "ask_confirm_or_offer": ("#{n} {who} просил на {his}: подтвердить это (ДА {n}) или предложить ему "
                                 "{offered} ({offer})?"),
        "ask_offer_instead_booked": ("#{n} {who} записан на {his}: предложить ему вместо этого {offered} "
                                     "({offer})? Или скажи, что ему передать."),
        "ask_offer_or_confirm": ("#{n} {who} просил на {his}: предложить ему вместо этого {offered} ({offer}) "
                                 "или подтвердить его время (ДА {n})?"),
        "held": "Вместе с ответом: {items}.",
        "held_for": "на {length}",
        "held_tell": "передать ему: {words}",
        "held_ask": "спросить его: {words}",
        "held_lost": ("#{n}: то, что ты писала к ней раньше{what}, вместе с этим ответом не ушло — пришли ещё раз, если это "
                      "ещё важно."),
        "extra_yes": "да: {extra}",
        "extra_no": "нет: {extra}",
        "words_tell": "Передам ему: {items}.",
        "words_ask": "Спрошу его: {question}",
        "words_ack": "#{n} {who}:",
        "answer_comes": "Его ответ придёт сюда.",
        "words_wait": ("Бот сейчас не отвечает в его чате, поэтому это подождёт; если за сутки не уйдёт — "
                       "удалится."),
        "words_after_quiet_hours": ("Ответы ему ждут конца тихих часов (примерно в {until}), так что это уйдёт "
                                    "ему тогда."),
        "words_dropped": ("#{n} {who}: это не дошло до него вовремя и удалено: {said}. Скажи ему сама, если это "
                          "ещё важно."),
        "facts_head": "#{n} {who} — {when} ({length}), {where}.",
        "facts_outcall": "выезд",
        "facts_incall": "у тебя",
        "facts_you_proposed": "Ты предложила {proposed}; ждём его ответа.",
        "facts_waiting": "Ждёт твоего ответа.",
        "facts_confirmed": "Подтверждена.",
        "facts_move": "Он просит перенести на {to} — ждём твоего ответа.",
        "facts_you_proposed_move": "Ты предложила перенести на {proposed}; ждём его ответа.",
        "facts_outcall_step": "Выезд: {step}.",
        "facts_arrived": "Он пишет, что приехал.",
        "facts_cancelled": "Отменена.",
        "facts_completed": "Состоялась.",
        "facts_no_show": "Он не пришёл.",
        "facts_extras": "Доп. услуги, которые он просил: {extras}.",
        "facts_none": "нет",
        "facts_extras_said": "Ты сказала да: {ok}; нет: {no}.",
        "facts_service": "Услуга: {service}",
        "facts_notes": "Заметки: {notes}",
        "facts_asked": "Ты его кое о чём спросила; ответы придут сюда.",
        "read_failed": ("Сейчас не получилось это прочитать — ответь ДА {n} / НЕТ {n} или временем, например "
                        "{n} 15:30."),
        "number_placeholder": "<номер>",
        "read_rest_failed": ("Остальное в твоём сообщении сейчас не получилось прочитать: прошло только это, и "
                             "ему ничего не передано. Пришли остальное ещё раз или используй ДА n / НЕТ n / "
                             "n 15:30."),
        "earlier_not_applied": ("То, что ты писала до этого, к этому не подошло, поэтому прошло только это, а из "
                                "прежнего ему ничего не ушло. Пришли ту часть ещё раз, если это ещё важно."),
        "read_nothing_failed": ("Сейчас не получилось это прочитать — ничего не сохранено и не передано. "
                                "Пришли, пожалуйста, ещё раз через пару минут."),
        "read_earlier_failed": ("Сейчас не получилось прочитать это вместе с твоим предыдущим сообщением: прошло "
                                "только это, из того сообщения — ничего. Пришли ту часть ещё раз, если это ещё "
                                "важно."),
        "standing_saved": ("Сохранено для админа, чтобы добавить в твои правила: «{text}». Пока админ это не "
                           "примет, ничего не меняется."),
        "standing_failed": "Сейчас не получилось сохранить «{text}» для админа — напиши админу сама, пожалуйста.",
        "stays": "#{n} остаётся как есть: {when}.",
        "ready_at_note": ("Ты будешь готова в {at}; ему ничего не ушло — напиши «скажи ему …», если ему нужно "
                          "это знать."),
        "slot_taken": "#{n}: это время теперь пересекается с другой бронью.",
        "length_taken": "#{n}: с такой длительностью она теперь пересекается с другой бронью.",
        "changed_meanwhile": "#{n} тем временем изменилась; напиши СПИСОК, чтобы увидеть, как обстоят дела.",
        "not_possible": "#{n}: {what} — нельзя ({why}).",
        "length_stays": "Остаётся {when}.",
        "nothing_moved": "Ничего не перенесено; ДА {n} — как он просил: {when}.",
        "nothing_confirmed": "Ничего не подтверждено; ДА {n} — как он просил: {when}.",
        "failed_n": "#{n}: что-то пошло не так ({error}); проверь страницу Bookings.",
        "failed_that": "С этим что-то пошло не так ({error}); проверь страницу Bookings.",
        "no_booking": "Брони #{n} нет.",
        "not_outcall": "#{n} — это не подтверждённый предстоящий выезд.",
        "already_ready": "#{n}: уже отмечено, что ты готова.",
        "already_in_taxi": "#{n}: он уже знает, что ты в такси.",
        "escalation": ("⚠️ {name}: нужен живой человек — в сообщении есть «{keyword}». Бот не отвечает в этом "
                       "чате, пока чат снова не включат в панели.\n\n«{excerpt}»"),
        "why_past": "уже прошло",
        "why_too_soon": "слишком скоро",
        "why_too_far": "слишком далеко вперёд",
        "why_closed_day": "выходной",
        "why_outside_hours": "вне рабочих часов",
        "why_conflict": "занято",
        "why_invalid": "неверно",
    },
    "lv": {
        "unknown_customer": "Nezināms klients",
        "the_customer": "klients",
        "unknown": "bez vārda",
        "request_new_time": " (jauns laiks)",
        "request_no_extras": "nekas nav minēts",
        "answer_help": "Atbildi JĀ {n} vai NĒ {n}, vai jaunu laiku, piem., {n} 15:30 vai {n} 04.10 15:30.",
        "move_commands": ("Atbildi JĀ {n}, lai pārceltu, NĒ {n}, lai paliktu vecais laiks, vai citu laiku, "
                          "piem., {n} 15:30."),
        "owner_update": "{girl_tag}#{n} {who}, {when}: {what}",
        "way_in_sent": "viņam nosūtīts, kā iekļūt.",
        "way_in_meet": "izej viņu sagaidīt.",
        "ready_reminder_commands": "sūti GATAVA {n}, kad esi gatava — tad viņš pasūtīs taksi.",
        "ack_change_length": "#{n} tagad {when} ({length}) ✅ — {who} to uzzinās.",
        "ack_reject_proposal": "#{n} paliek {when} — {who} to uzzinās.",
        "ack_mark_completed": "#{n} atzīmēta kā notikusi.",
        "ack_mark_no_show": "#{n} atzīmēta: viņš neieradās.",
        "ack_ready": ("#{n}: tu esi gatava ✅ — {who} ir palūgts pasūtīt taksi uz tavu adresi un atsūtīt saiti. "
                      "Tā atnāks šurp."),
        "ack_in_taxi": ("#{n}: tu esi taksī ✅ — {who} ir palūgts atsūtīt adresi, stāvu, kā iekļūt un vietas "
                        "foto. Tas viss atnāks šurp."),
        "ack_other": "#{n} {action}",
        "ack_outcall_proof": ("Tas ir izbraukums: {who} ir palūgts atsūtīt foto, ka viņš ir uz vietas (viesnīcas "
                              "karte vai ēka). Tas atnāks šurp, tad viņš pasūtīs tev taksi. GATAVA {n} — bez "
                              "foto."),
        "ack_outcall_ready": ("Tas ir izbraukums: sūti GATAVA {n}, kad esi gatava, un {who} tiks palūgts pasūtīt "
                              "taksi."),
        "from_panel": "(no paneļa)",
        "step_in_taxi": "taksī",
        "step_taxi_link": "taksi saite atsūtīta — sūti TAKSĪ {n}, kad esi iekšā",
        "step_proof_came": "viņa foto ir atnācis — gaidām taksi saiti",
        "step_ready": "gatava — gaidām taksi saiti",
        "step_wait_proof": "gaidām viņa vietas foto (GATAVA {n} — bez foto)",
        "step_send_ready": "sūti GATAVA {n}, kad esi gatava",
        "list_nothing": "Pašlaik nekas negaida tavu atbildi.",
        "list_waiting": "Gaida tavu atbildi:",
        "list_move": "#{n} — {who} grib pārcelt {when} → {to}",
        "list_proposed": "#{n} — {who}, tu piedāvāji {proposed} (gaidām viņa atbildi)",
        "list_request": "#{n} — {who}, {when}",
        "list_commands": "Atbildi JĀ <numurs>, NĒ <numurs> vai <numurs> <jaunais laiks>.",
        "list_outcalls": "Izbraukumi:",
        "list_outcall": "#{n} — {who}, {when}: {step}",
        "taxi_link": "🚕 #{n} {who} atsūtīja taksi saiti:\n{text}",
        "taxi_link_in": ("Sūti TAKSĪ {n}, kad esi taksī (ja nesūtīsi, pēc {minutes} min viņam pateiks, ka tu jau "
                         "brauc)."),
        "proof_header": ("📷 #{n} {who} atsūtīja vietas foto (zemāk). Viņu tagad lūdz pasūtīt tev taksi; saite "
                         "atnāks šurp."),
        "passed_on": "💬 #{n} {who}:\n{text}",
        "photo_header": "📷 #{n} {who} atsūtīja foto:",
        "photo_not_fetched": "[foto neizdevās saņemt — varbūt tas bija skatāms tikai vienreiz; {again}]",
        "photo_again_asked": "viņš ir palūgts atsūtīt vēlreiz",
        "photo_again_ask": "palūdz viņu atsūtīt vēlreiz",
        "news_accept_proposal": "klients pieņēma tevis piedāvāto laiku — apstiprināta.",
        "news_reschedule": "klients pieņēma jauno laiku — pārcelta.",
        "news_reject_proposal_cancelled": "klients noraidīja tevis piedāvāto laiku. Rezervācija atcelta.",
        "news_reject_proposal_stays": "klients noraidīja tevis piedāvāto laiku. Viss paliek kā bija.",
        "news_lapsed": "termiņš beidzās — laikā netika atbildēts.",
        "news_arrived_photo_ok": "ir ieradies (foto sakrīt ar ieeju); viņam nosūtīts, kā iekļūt.",
        "news_arrived_photo_bad": "atsūtīja ierašanās foto, kas neizskatās pēc ieejas — pārbaudi čatu.",
        "news_told_in_taxi": "viņam pateikts, ka tu esi taksī{late}. Viņa adrese, stāvs un foto atnāks šurp.",
        "news_in_taxi_late": "(„TAKSĪ {n}” netika atsūtīts {minutes} min laikā pēc taksi saites)",
        "which": "Kura rezervācija? Pievieno numuru, piem., {kind} 7. Sūti SARAKSTS, lai tās redzētu.",
        "which_about": "Par kuru rezervāciju tas ir? Sūti SARAKSTS, lai tās redzētu.",
        "which_one": "Kura: {listed}? Atbildi ar numuru.",
        "or_last": "{items} vai {last}",
        "question_time": "#{n} {who}: kādu laiku viņam piedāvāt?",
        "question_yes_no": "#{n} {who}, {when}: jā vai nē? Vai arī uzraksti citu laiku.",
        "ask_confirm_or_offer": ("#{n} {who} gribēja {his}: apstiprināt to (JĀ {n}) vai piedāvāt viņam {offered} "
                                 "({offer})?"),
        "ask_offer_instead_booked": ("#{n} {who} ir pierakstīts uz {his}: piedāvāt viņam tā vietā {offered} "
                                     "({offer})? Vai arī pasaki, ko viņam pateikt."),
        "ask_offer_or_confirm": ("#{n} {who} gribēja {his}: piedāvāt viņam tā vietā {offered} ({offer}) vai "
                                 "apstiprināt viņa laiku (JĀ {n})?"),
        "held": "Kopā ar tavu atbildi: {items}.",
        "held_for": "ilgums {length}",
        "held_tell": "pateikt viņam: {words}",
        "held_ask": "pajautāt viņam: {words}",
        "held_lost": ("#{n}: tas, ko pirms tam pie tā rakstīji{what}, ar šo netika nodots — sūti vēlreiz, ja tas "
                      "vēl ir svarīgi."),
        "extra_yes": "jā: {extra}",
        "extra_no": "nē: {extra}",
        "words_tell": "Pateikšu viņam: {items}.",
        "words_ask": "Pajautāšu viņam: {question}",
        "words_ack": "#{n} {who}:",
        "answer_comes": "Viņa atbilde atnāks šurp.",
        "words_wait": ("Bots viņa čatā pašlaik neatbild, tāpēc tas pagaidīs; ja dienas laikā to nevarēs "
                       "nosūtīt, tas tiks dzēsts."),
        "words_after_quiet_hours": ("Atbildes viņam gaida, kamēr beigsies klusās stundas (ap {until}), tāpēc tas "
                                    "aizies tad."),
        "words_dropped": ("#{n} {who}: tas nenonāca pie viņa laikā un tika dzēsts: {said}. Pasaki viņam pati, "
                          "ja tas vēl ir svarīgi."),
        "facts_head": "#{n} {who} — {when} ({length}), {where}.",
        "facts_outcall": "izbraukums",
        "facts_incall": "pie tevis",
        "facts_you_proposed": "Tu piedāvāji {proposed}; gaidām viņa atbildi.",
        "facts_waiting": "Gaida tavu atbildi.",
        "facts_confirmed": "Apstiprināta.",
        "facts_move": "Viņš lūdz pārcelt uz {to} — gaida tavu atbildi.",
        "facts_you_proposed_move": "Tu piedāvāji pārcelt uz {proposed}; gaidām viņa atbildi.",
        "facts_outcall_step": "Izbraukums: {step}.",
        "facts_arrived": "Viņš saka, ka ir ieradies.",
        "facts_cancelled": "Atcelta.",
        "facts_completed": "Notika.",
        "facts_no_show": "Viņš neieradās.",
        "facts_extras": "Papildpakalpojumi, ko viņš prasīja: {extras}.",
        "facts_none": "nav",
        "facts_extras_said": "Tu teici jā: {ok}; nē: {no}.",
        "facts_service": "Pakalpojums: {service}",
        "facts_notes": "Piezīmes: {notes}",
        "facts_asked": "Tu viņam kaut ko pajautāji; atbildes atnāks šurp.",
        "read_failed": ("Pašlaik neizdevās to izlasīt — atbildi JĀ {n} / NĒ {n} vai ar laiku, piem., "
                        "{n} 15:30."),
        "number_placeholder": "<numurs>",
        "read_rest_failed": ("Pārējo tavā ziņā pašlaik neizdevās izlasīt: izgāja tikai tas, un viņam nekas netika "
                             "nodots. Sūti pārējo vēlreiz vai izmanto JĀ n / NĒ n / n 15:30."),
        "earlier_not_applied": ("Tas, ko rakstīji pirms šī, tam neatbilda, tāpēc izgāja tikai šis, un nekas no "
                                "iepriekšējā viņam netika nosūtīts. Sūti to daļu vēlreiz, ja tas vēl ir svarīgi."),
        "read_nothing_failed": ("Pašlaik neizdevās to izlasīt — nekas netika saglabāts vai nodots. Lūdzu, sūti "
                                "vēlreiz pēc pāris minūtēm."),
        "read_earlier_failed": ("Pašlaik neizdevās to izlasīt kopā ar tavu iepriekšējo ziņu: izgāja tikai šis, "
                                "nekas cits no tās ziņas. Sūti to daļu vēlreiz, ja tas vēl ir svarīgi."),
        "standing_saved": ("Saglabāts, lai admins to pievienotu taviem noteikumiem: „{text}”. Kamēr admins to "
                           "nepieņems, nekas nemainās."),
        "standing_failed": "Pašlaik neizdevās saglabāt „{text}” adminam — lūdzu, pasaki adminam pati.",
        "stays": "#{n} paliek kā ir: {when}.",
        "ready_at_note": ("Tu būsi gatava {at}; viņam nekas netika nosūtīts — raksti „pasaki viņam …”, ja viņam "
                          "tas jāzina."),
        "slot_taken": "#{n}: šis laiks tagad pārklājas ar citu rezervāciju.",
        "length_taken": "#{n}: ar šādu ilgumu tā tagad pārklājas ar citu rezervāciju.",
        "changed_meanwhile": "#{n} pa to laiku mainījās; sūti SARAKSTS, lai redzētu, kā ir tagad.",
        "not_possible": "#{n}: {what} nav iespējams ({why}).",
        "length_stays": "Paliek {when}.",
        "nothing_moved": "Nekas netika pārcelts; JĀ {n} — kā viņš lūdza: {when}.",
        "nothing_confirmed": "Nekas netika apstiprināts; JĀ {n} — kā viņš lūdza: {when}.",
        "failed_n": "#{n}: kaut kas nogāja greizi ({error}); pārbaudi lapu Bookings.",
        "failed_that": "Ar šo kaut kas nogāja greizi ({error}); pārbaudi lapu Bookings.",
        "no_booking": "Rezervācijas #{n} nav.",
        "not_outcall": "#{n} nav apstiprināts gaidāms izbraukums.",
        "already_ready": "#{n}: jau atzīmēts, ka esi gatava.",
        "already_in_taxi": "#{n}: viņš jau zina, ka tu esi taksī.",
        "escalation": ("⚠️ {name}: vajag cilvēku — ziņā ir „{keyword}”. Bots šajā čatā neatbild, kamēr to atkal "
                       "neieslēgs panelī.\n\n„{excerpt}”"),
        "why_past": "pagātnē",
        "why_too_soon": "pārāk drīz",
        "why_too_far": "pārāk tālu uz priekšu",
        "why_closed_day": "brīvdiena",
        "why_outside_hours": "ārpus darba laika",
        "why_conflict": "aizņemts",
        "why_invalid": "nederīgs",
    },
    "es": {
        "unknown_customer": "Cliente desconocido",
        "the_customer": "el cliente",
        "unknown": "sin nombre",
        "request_new_time": " (nueva hora)",
        "request_no_extras": "ninguno",
        "answer_help": "Responde SÍ {n} o NO {n}, o envía una nueva hora, como {n} 15:30 o {n} 04.10 15:30.",
        "move_commands": ("Responde SÍ {n} para cambiarla, NO {n} para mantener la hora original, o otra hora, "
                          "como {n} 15:30."),
        "owner_update": "{girl_tag}#{n} {who}, {when}: {what}",
        "way_in_sent": "se le envió cómo entrar.",
        "way_in_meet": "sal a recibirlo.",
        "ready_reminder_commands": "envía READY {n} cuando estés lista — entonces él pide el taxi.",
        "ack_change_length": "#{n} ahora {when} ({length}) ✅ — {who} será avisado.",
        "ack_reject_proposal": "#{n} se queda en {when} — {who} será avisado.",
        "ack_mark_completed": "#{n} marcada como realizada.",
        "ack_mark_no_show": "#{n} marcada: no vino.",
        "ack_ready": ("#{n}: lista ✅ — {who} ya sabe que debe pedir el taxi a tu dirección y mandar el enlace. "
                      "Llegará aquí."),
        "ack_in_taxi": ("#{n}: en el taxi ✅ — {who} ya sabe que debe mandar la dirección, el piso, cómo entrar y "
                        "una foto del sitio. Todo llegará aquí."),
        "ack_other": "#{n} {action}",
        "ack_outcall_proof": ("Es una salida: {who} ya sabe que debe mandar una foto que muestre que está en el "
                              "sitio (tarjeta de la habitación o el edificio). Llegará aquí y luego él te pide el taxi. "
                              "READY {n} se salta la foto."),
        "ack_outcall_ready": ("Es una salida: envía READY {n} cuando estés lista y {who} recibirá el aviso de "
                              "pedir el taxi."),
        "from_panel": "(desde el panel)",
        "step_in_taxi": "en el taxi",
        "step_taxi_link": "llegó el enlace del taxi — envía TAXI {n} cuando estés dentro",
        "step_proof_came": "llegó su foto — esperando el enlace del taxi",
        "step_ready": "lista — esperando el enlace del taxi",
        "step_wait_proof": "esperando su foto del sitio (READY {n} se la salta)",
        "step_send_ready": "envía READY {n} cuando estés lista",
        "list_nothing": "Ahora mismo nada espera tu respuesta.",
        "list_waiting": "Esperan tu respuesta:",
        "list_move": "#{n} — {who} quiere cambiar {when} → {to}",
        "list_proposed": "#{n} — {who}, propusiste {proposed} (esperando su respuesta)",
        "list_request": "#{n} — {who}, {when}",
        "list_commands": "Responde SÍ <número>, NO <número> o <número> <nueva hora>.",
        "list_outcalls": "Salidas:",
        "list_outcall": "#{n} — {who}, {when}: {step}",
        "taxi_link": "🚕 #{n} {who} mandó el enlace del taxi:\n{text}",
        "taxi_link_in": ("Envía TAXI {n} cuando estés en el taxi (si no, en {minutes} min se le dice que ya vas "
                         "en camino)."),
        "proof_header": ("📷 #{n} {who} mandó su foto del sitio (abajo). Ahora le toca pedirte el taxi; el enlace "
                         "llegará aquí."),
        "passed_on": "💬 #{n} {who}:\n{text}",
        "photo_header": "📷 #{n} {who} mandó una foto:",
        "photo_not_fetched": "[una foto que no se pudo descargar — quizá era de ver una sola vez; {again}]",
        "photo_again_asked": "se le pidió que la mande otra vez",
        "photo_again_ask": "pídele que la mande otra vez",
        "news_accept_proposal": "el cliente aceptó la hora que propusiste — confirmada.",
        "news_reschedule": "el cliente aceptó la nueva hora — cambiada.",
        "news_reject_proposal_cancelled": "el cliente rechazó la hora que propusiste. La reserva queda cancelada.",
        "news_reject_proposal_stays": "el cliente rechazó la hora que propusiste. Todo sigue como estaba.",
        "news_lapsed": "caducó — no se respondió a tiempo.",
        "news_arrived_photo_ok": "ya llegó (su foto coincide con la entrada); se le envió cómo entrar.",
        "news_arrived_photo_bad": "mandó una foto de llegada que no parece la entrada — revisa el chat.",
        "news_told_in_taxi": "se le dijo que vas en el taxi{late}. Su dirección, piso y fotos llegarán aquí.",
        "news_in_taxi_late": "(sin TAXI {n} en {minutes} min desde el enlace del taxi)",
        "which": "¿Qué reserva? Añade su número, por ejemplo, {kind} 7. Envía LIST para verlas.",
        "which_about": "¿De qué reserva hablas? Envía LIST para verlas.",
        "which_one": "¿Cuál: {listed}? Responde con el número.",
        "or_last": "{items} o {last}",
        "question_time": "#{n} {who}: ¿qué hora le ofrezco?",
        "question_yes_no": "#{n} {who}, {when}: ¿sí o no? O dime otra hora.",
        "ask_confirm_or_offer": ("#{n} {who} pidió para {his}: ¿lo confirmas (SÍ {n}) o le ofreces {offered} "
                                 "({offer})?"),
        "ask_offer_instead_booked": ("#{n} {who} tiene reserva para {his}: ¿le ofrezco {offered} en su lugar "
                                     "({offer})? O dime qué le digo."),
        "ask_offer_or_confirm": ("#{n} {who} pidió para {his}: ¿le ofreces {offered} en su lugar ({offer}) o "
                                 "confirmas su hora (SÍ {n})?"),
        "held": "Con tu respuesta: {items}.",
        "held_for": "por {length}",
        "held_tell": "decirle: {words}",
        "held_ask": "preguntarle: {words}",
        "held_lost": ("#{n}: lo que escribiste antes con esto{what} no se le pasó — mándalo otra vez si aún "
                      "importa."),
        "extra_yes": "sí a {extra}",
        "extra_no": "sin {extra}",
        "words_tell": "Le diré: {items}.",
        "words_ask": "Le preguntaré: {question}",
        "words_ack": "#{n} {who}:",
        "answer_comes": "Su respuesta llegará aquí.",
        "words_wait": ("Ahora mismo su chat no se está respondiendo, así que esto espera; si no puede salir en "
                       "un día, se descarta."),
        "words_after_quiet_hours": ("Sus respuestas esperan a que acaben las horas de silencio (hacia las "
                                    "{until}), así que esto le llegará entonces."),
        "words_dropped": ("#{n} {who}: esto no le llegó a tiempo y se descartó: {said}. Díselo tú si aún "
                          "importa."),
        "facts_head": "#{n} {who} — {when} ({length}), {where}.",
        "facts_outcall": "salida",
        "facts_incall": "en tu sitio",
        "facts_you_proposed": "Propusiste {proposed}; esperando su respuesta.",
        "facts_waiting": "Espera tu respuesta.",
        "facts_confirmed": "Confirmada.",
        "facts_move": "Pide cambiarla a {to} — espera tu respuesta.",
        "facts_you_proposed_move": "Propusiste cambiarla a {proposed}; esperando su respuesta.",
        "facts_outcall_step": "Salida: {step}.",
        "facts_arrived": "Dice que ya llegó.",
        "facts_cancelled": "Cancelada.",
        "facts_completed": "Realizada.",
        "facts_no_show": "No vino.",
        "facts_extras": "Extras que pidió: {extras}.",
        "facts_none": "ninguno",
        "facts_extras_said": "Dijiste sí a: {ok}; no a: {no}.",
        "facts_service": "Servicio: {service}",
        "facts_notes": "Notas: {notes}",
        "facts_asked": "Le preguntaste algo; sus respuestas llegan aquí.",
        "read_failed": "Ahora mismo no pude leerlo — responde SÍ {n} / NO {n}, o una hora como {n} 15:30.",
        "number_placeholder": "<número>",
        "read_rest_failed": ("Ahora mismo no pude leer el resto de tu mensaje: solo pasó eso y no se le mandó "
                             "nada. Manda el resto otra vez o usa SÍ n / NO n / n 15:30."),
        "earlier_not_applied": ("Lo que escribiste antes no encajaba con esto, así que solo pasó esto y nada de lo "
                                "anterior le llegó. Manda esa parte otra vez si aún importa."),
        "read_nothing_failed": ("Ahora mismo no pude leerlo — no se guardó ni se pasó nada. Mándalo otra vez en "
                                "unos minutos, por favor."),
        "read_earlier_failed": ("Ahora mismo no pude leer esto junto con tu mensaje anterior: solo pasó esto, nada "
                                "más de aquel mensaje. Manda esa parte otra vez si aún importa."),
        "standing_saved": ("Guardado para que el admin lo añada a tus reglas: «{text}». No cambia nada hasta "
                           "que lo acepte."),
        "standing_failed": "Ahora mismo no pude guardar «{text}» para el admin — díselo tú, por favor.",
        "stays": "#{n} se queda como está: {when}.",
        "ready_at_note": ("Estarás lista a las {at}; no se le mandó nada — escribe «dile ...» si él debe "
                          "saberlo."),
        "slot_taken": "#{n}: esa hora ahora choca con otra reserva.",
        "length_taken": "#{n}: con esa duración ahora choca con otra reserva.",
        "changed_meanwhile": "#{n} cambió mientras tanto; envía LIST para ver cómo está todo.",
        "not_possible": "#{n}: {what} no es posible ({why}).",
        "length_stays": "Se queda en {when}.",
        "nothing_moved": "No se cambió nada; SÍ {n} la deja como él pidió: {when}.",
        "nothing_confirmed": "No se confirmó nada; SÍ {n} la deja como él pidió: {when}.",
        "failed_n": "#{n}: algo salió mal ({error}); revisa la página Bookings.",
        "failed_that": "Con eso algo salió mal ({error}); revisa la página Bookings.",
        "no_booking": "No hay ninguna reserva #{n}.",
        "not_outcall": "#{n} no es una salida confirmada que aún esté por venir.",
        "already_ready": "#{n}: ya está marcado que estás lista.",
        "already_in_taxi": "#{n}: ya sabe que vas en el taxi.",
        "escalation": ("⚠️ {name} necesita una persona: su mensaje contiene «{keyword}». El bot dejó de "
                       "responderle hasta que se vuelva a activar el chat en el panel.\n\n«{excerpt}»"),
        "why_past": "ya pasó",
        "why_too_soon": "demasiado pronto",
        "why_too_far": "con demasiada antelación",
        "why_closed_day": "día cerrado",
        "why_outside_hours": "fuera de horario",
        "why_conflict": "choca con otra",
        "why_invalid": "no válido",
    },
}
for _lang, _forms in _TRANSLATIONS.items():
    for _key, _text in _forms.items():
        TEXTS[_key][_lang] = _text
del _lang, _forms, _key, _text

# The rules' own messages (booking_states.IllegalTransition, and what
# parse_owner_reply could not read) put into her lines: the English message
# matched whole, its parts named, said again in her language when there is
# a text for it. English, and anything not matched, goes as it is.
RULE_ERRORS: dict[str, dict[str, str]] = {
    r"cannot (?P<action>.+) booking #(?P<number>\d+): it is (?P<state>\w+)": {
        "en": "cannot {action} booking #{number}: it is {state}",
        "ru": "бронь #{number}: нельзя {action} — она {state}",
        "lv": "rezervācija #{number}: nevar {action}, jo tā {state}",
        "es": "reserva #{number}: no se puede {action} porque {state}",
    },
    r"cannot (?P<action>.+) booking #(?P<number>\d+) before it starts": {
        "en": "cannot {action} booking #{number} before it starts",
        "ru": "бронь #{number}: нельзя {action} до её начала",
        "lv": "rezervācija #{number}: nevar {action}, pirms tā sākas",
        "es": "reserva #{number}: no se puede {action} antes de que empiece",
    },
    r"(?P<actor>\w+) cannot (?P<action>.+) a booking": {
        "en": "{actor} cannot {action} a booking",
        "ru": "{actor} {action}",
        "lv": "{actor} {action}",
        "es": "{actor} {action}",
    },
    r"(?P<what>.+) is in the past": {
        "en": "{what} is in the past",
        "ru": "{what} — уже в прошлом",
        "lv": "{what} jau ir pagātnē",
        "es": "{what} ya pasó",
    },
    r"the new end of booking #(?P<number>\d+) is not after its start": {
        "en": "the new end of booking #{number} is not after its start",
        "ru": "бронь #{number}: новое окончание должно быть позже начала",
        "lv": "rezervācija #{number}: jaunās beigas nav pēc sākuma",
        "es": "reserva #{number}: el nuevo final no es posterior al inicio",
    },
    r"the proposed end is before its start": {
        "en": "the proposed end is before its start",
        "ru": "предложенное окончание раньше начала",
        "lv": "piedāvātās beigas ir pirms sākuma",
        "es": "el final propuesto es antes del inicio",
    },
    r"booking #(?P<number>\d+) has no proposed time": {
        "en": "booking #{number} has no proposed time",
        "ru": "у брони #{number} нет предложенного времени",
        "lv": "rezervācijai #{number} nav piedāvāta laika",
        "es": "la reserva #{number} no tiene hora propuesta",
    },
    r"booking #(?P<number>\d+) has not started yet": {
        "en": "booking #{number} has not started yet",
        "ru": "бронь #{number} ещё не началась",
        "lv": "rezervācija #{number} vēl nav sākusies",
        "es": "la reserva #{number} aún no empezó",
    },
    r"(?P<kind>\w+) is not something the owner can do to a booking": {
        "en": "{kind} is not something the owner can do to a booking",
        "ru": "«{kind}» с бронью сделать нельзя",
        "lv": "„{kind}” ar rezervāciju izdarīt nevar",
        "es": "\"{kind}\" no se puede hacer con una reserva",
    },
    r"I did not understand (?P<what>.+)": {
        "en": "I did not understand {what}",
        "ru": "непонятно: {what}",
        "lv": "nesapratu {what}",
        "es": "no entendí {what}",
    },
    r"no time given \(use HH:MM\)": {
        "en": "no time given (use HH:MM)",
        "ru": "не указано время (пиши ЧЧ:ММ)",
        "lv": "nav norādīts laiks (raksti SS:MM)",
        "es": "falta la hora (usa HH:MM)",
    },
    r"(?P<what>\S+) is not a time": {
        "en": "{what} is not a time",
        "ru": "{what} — это не время",
        "lv": "{what} nav laiks",
        "es": "{what} no es una hora",
    },
    r"(?P<what>\S+) is not a date": {
        "en": "{what} is not a date",
        "ru": "{what} — это не дата",
        "lv": "{what} nav datums",
        "es": "{what} no es una fecha",
    },
}

# The English words the rules put into those messages (booking_states: the
# action, the state, who tried it, what is in the past), in her language,
# so the whole line reads in it. A word not here goes as it is.
_RULE_WORDS: dict[str, dict[str, dict[str, str]]] = {
    "ru": {
        "action": {
            "submit": "отправить", "confirm": "подтвердить", "decline": "отклонить", "change": "изменить",
            "propose a new time for": "предложить новое время", "accept the proposed time of":
            "принять предложенное время", "turn down the proposed time of": "отклонить предложенное время",
            "reschedule": "перенести", "change the length of": "изменить длительность", "cancel": "отменить",
            "expire": "закрыть по сроку", "mark completed": "отметить как состоявшуюся",
            "complete": "отметить как состоявшуюся",
            "mark as missed": "отметить неявку", "confirm attendance for": "подтвердить приход",
        },
        "state": {
            "requested": "ещё не отправлена", "pending": "ждёт ответа", "confirmed": "подтверждена",
            "cancelled": "отменена", "no_show": "закрыта как неявка", "completed": "уже состоялась",
        },
        "actor": {"owner": "ты не можешь", "customer": "клиент не может", "admin": "админ не может",
                  "system": "система не может"},
        "what": {"the booking time": "время брони", "the new time": "новое время",
                 "the proposed time": "предложенное время", "the new end": "новое окончание",
                 "the booking": "бронь"},
    },
    "lv": {
        "action": {
            "submit": "iesniegt", "confirm": "apstiprināt", "decline": "atteikt", "change": "mainīt",
            "propose a new time for": "piedāvāt citu laiku", "accept the proposed time of":
            "pieņemt piedāvāto laiku", "turn down the proposed time of": "noraidīt piedāvāto laiku",
            "reschedule": "pārcelt", "change the length of": "mainīt ilgumu", "cancel": "atcelt",
            "expire": "slēgt pēc termiņa", "mark completed": "atzīmēt kā notikušu", "complete": "atzīmēt kā notikušu",
            "mark as missed": "atzīmēt neierašanos", "confirm attendance for": "apstiprināt ierašanos",
        },
        "state": {
            "requested": "vēl nav nosūtīta", "pending": "gaida atbildi", "confirmed": "ir apstiprināta",
            "cancelled": "ir atcelta", "no_show": "ir atzīmēta kā neierašanās", "completed": "jau ir notikusi",
        },
        "actor": {"owner": "tu nevari", "customer": "klients nevar", "admin": "admins nevar",
                  "system": "sistēma nevar"},
        "what": {"the booking time": "rezervācijas laiks", "the new time": "jaunais laiks",
                 "the proposed time": "piedāvātais laiks", "the new end": "jaunās beigas",
                 "the booking": "rezervācija"},
    },
    "es": {
        "action": {
            "submit": "enviar", "confirm": "confirmar", "decline": "rechazar", "change": "cambiar",
            "propose a new time for": "proponer otra hora", "accept the proposed time of":
            "aceptar la hora propuesta", "turn down the proposed time of": "rechazar la hora propuesta",
            "reschedule": "cambiar de hora", "change the length of": "cambiar la duración", "cancel": "cancelar",
            "expire": "cerrar por tiempo", "mark completed": "marcar como realizada", "complete": "marcar como realizada",
            "mark as missed": "marcar que no vino", "confirm attendance for": "confirmar la llegada",
        },
        "state": {
            "requested": "aún no se envió", "pending": "está pendiente", "confirmed": "está confirmada",
            "cancelled": "está cancelada", "no_show": "está marcada como «no se presentó»", "completed": "ya se realizó",
        },
        "actor": {"owner": "no puedes", "customer": "el cliente no puede", "admin": "el admin no puede",
                  "system": "el sistema no puede"},
        "what": {"the booking time": "la hora de la reserva", "the new time": "la nueva hora",
                 "the proposed time": "la hora propuesta", "the new end": "el nuevo final",
                 "the booking": "la reserva"},
    },
}


# The command word shown to her for a kind of answer ("Add its number, e.g.
# ДА 7"), in her language: only words parse_owner_reply reads, English
# where her language has none (or one too close to another: ГОТОВО, done,
# next to ГОТОВА, ready).
COMMANDS: dict[str, dict[str, str]] = {
    "ru": {"yes": "ДА", "no": "НЕТ", "cancel": "ОТМЕНА", "noshow": "НЕЯВКА", "ready": "ГОТОВА", "in": "В ТАКСИ"},
    "lv": {"yes": "JĀ", "no": "NĒ", "cancel": "ATCELT", "done": "NOTIKA", "noshow": "NEIERADĀS", "ready": "GATAVA",
           "in": "TAKSĪ"},
    "es": {"yes": "SÍ", "no": "NO", "in": "TAXI"},
}


# ------------------------------------------------------------------ dates

_DAYS = {
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    "ru": ("пн", "вт", "ср", "чт", "пт", "сб", "вс"),
    "lv": ("pirmd.", "otrd.", "trešd.", "ceturtd.", "piektd.", "sestd.", "svētd."),
    "es": ("lun", "mar", "mié", "jue", "vie", "sáb", "dom"),
}
_MONTHS = {
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
    # After a day number, so in the genitive: "20 окт", "5 мая", "1 июня".
    "ru": ("янв", "фев", "мар", "апр", "мая", "июня", "июля", "авг", "сен", "окт", "ноя", "дек"),
    "lv": ("janv.", "febr.", "marts", "apr.", "maijs", "jūn.", "jūl.", "aug.", "sept.", "okt.", "nov.", "dec."),
    "es": ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"),
}


def _plural_ru(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


# ------------------------------------------------------------- checking

_FORMATTER = string.Formatter()


def _fields(text: str) -> list[tuple[str, Optional[str], str]]:
    """(name, conversion, spec) of every blank in `text`; ValueError when a
    brace is unpaired."""
    return [(name, conversion, spec or "") for _, name, spec, conversion in _FORMATTER.parse(text)
            if name is not None]


def check(key: str, text: str) -> list[str]:
    """What is wrong with the account's text for template `key` ([] when
    it can be used). Empty text means the default, which is fine."""
    template = TEMPLATES.get(key)
    if template is None:
        return [f"there is no message called {key!r}"]
    if not isinstance(text, str):
        return ["must be text"]
    if len(text) > MAX_TEMPLATE:
        return [f"too long ({len(text)} characters; at most {MAX_TEMPLATE})"]
    try:
        fields = _fields(text)
    except ValueError:
        return ["a { or } without its pair — write {{ or }} for a brace itself"]
    problems = []
    for name, conversion, spec in fields:
        if not name:
            problems.append("{} needs a name inside, e.g. {number}")
        elif name not in template.blanks:
            problems.append(f"{{{name}}} is not a blank of this message; it has: "
                            + ", ".join(f"{{{b}}}" for b in template.blanks))
        elif conversion or spec:
            problems.append(f"write the blank as {{{name}}}, with nothing after its name")
    return sorted(set(problems), key=problems.index)


def check_all(messages: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(template key, problem) for the account's whole set."""
    return [(key, problem) for key, text in messages.items() for problem in check(key, text)]


def _render(text: str, values: Mapping[str, str]) -> str:
    """`text` with its blanks filled; a line whose blanks are all empty is
    left out."""
    out = []
    for line in text.split("\n"):
        names = [name for name, _, _ in _fields(line)]
        if names and not any(values.get(name) for name in names):
            continue
        out.append(line.format_map({name: values.get(name, "") for name in names}))
    return "\n".join(out)


# ---------------------------------------------------------------- texts


@dataclass(frozen=True)
class Texts:
    """What the bot says to the booker of one account: her language and
    the account's own versions of the main messages."""
    lang: str = DEFAULT_LANGUAGE
    custom: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def of(cls, settings: Mapping[str, Any]) -> "Texts":
        """From the account's booking settings."""
        lang = settings.get("booker_language") or DEFAULT_LANGUAGE
        custom = settings.get("booker_messages") or {}
        return cls(lang if lang in LANGUAGES else DEFAULT_LANGUAGE,
                   {k: v for k, v in custom.items() if isinstance(v, str) and v.strip()})

    # -------------------------------------------------------- the main ones

    def default(self, key: str) -> str:
        return DEFAULTS.get(self.lang, {}).get(key) or DEFAULTS[DEFAULT_LANGUAGE][key]

    def template(self, key: str, **values: str) -> str:
        """Template `key` filled with `values`: the account's text when it
        has one that works, else the default. The number, and a commands
        line, are never left out."""
        own = self.custom.get(key)
        if own:
            try:
                if check(key, own):
                    raise ValueError("; ".join(check(key, own)))
                text = _render(own, values)
                names = {name for name, _, _ in _fields(own)}
                if "number" not in names and values.get("number"):
                    text = f"#{values['number']} {text}"
                if TEMPLATES[key].commands and "commands" not in names and values.get("commands"):
                    text = f"{text.rstrip()}\n\n{values['commands']}"
                if text.strip():
                    return text
            except Exception as exc:
                log.warning("Booker message %r of the account can't be used (%s); the default goes out.", key, exc)
        return _render(self.default(key), values)

    # --------------------------------------------------------- the others

    def say(self, key: str, **values: Any) -> str:
        """Fixed line `key` in her language (English when it has none)."""
        forms = TEXTS[key]
        text = forms.get(self.lang) or forms[DEFAULT_LANGUAGE]
        try:
            return text.format(**values)
        except (KeyError, IndexError, ValueError):
            log.warning("Booker text %r in %s can't be filled; said in English.", key, self.lang)
            return forms[DEFAULT_LANGUAGE].format(**values)

    def command(self, kind: str) -> str:
        """The word she types for a kind of answer (yes, cancel, ...)."""
        return COMMANDS.get(self.lang, {}).get(kind) or kind.upper()

    def has(self, key: str) -> bool:
        return key in TEXTS

    def rule_error(self, text: str) -> str:
        """A rule's own English message (RULE_ERRORS) in her language; as it
        is in English, or when there is no text for it."""
        if self.lang == DEFAULT_LANGUAGE:
            return text
        for pattern, forms in RULE_ERRORS.items():
            match = re.fullmatch(pattern, text)
            if match is None:
                continue
            form = forms.get(self.lang)
            if form:
                words = _RULE_WORDS.get(self.lang, {})
                parts = {name: words.get(name, {}).get(value, value) for name, value in match.groupdict().items()}
                try:
                    return form.format(**parts)
                except (KeyError, IndexError, ValueError):
                    log.warning("Rule message %r in %s can't be filled; said in English.", pattern, self.lang)
            return text
        return text

    # ----------------------------------------------------- times, lengths

    def day(self, when: datetime, *, year: bool = True) -> str:
        """'Tue 20 Oct 2026' in her language (`when` already local)."""
        wd, mon = _DAYS[self.lang][when.weekday()], _MONTHS[self.lang][when.month - 1]
        if self.lang == "en":
            return f"{wd} {when.day:02d} {mon}" + (f" {when.year}" if year else "")
        if self.lang == "lv":
            return f"{wd}, {when.day}. {mon}" + (f" {when.year}" if year else "")
        if self.lang == "ru":
            return f"{wd}, {when.day} {mon}" + (f" {when.year}" if year else "")
        return f"{wd} {when.day} {mon}" + (f" {when.year}" if year else "")

    def weekday(self, when: datetime) -> str:
        return _DAYS[self.lang][when.weekday()]

    def span(self, start: datetime, end: datetime) -> str:
        """'Tue 20 Oct 2026, 21:00–22:00' (both already local)."""
        if end.date() == start.date():
            return f"{self.day(start)}, {start:%H:%M}–{end:%H:%M}"
        return f"{self.day(start)}, {start:%H:%M} – {self.day(end, year=False)} {end:%H:%M}"

    def minutes(self, minutes: int) -> str:
        """'1 hour', '2 hours', '1 h 30 min', '45 min' in her language."""
        hours, rest = divmod(minutes, 60)
        if minutes >= 60 and rest == 0:
            if self.lang == "ru":
                return f"{hours} {_plural_ru(hours, 'час', 'часа', 'часов')}"
            if self.lang == "lv":
                return f"{hours} {'stunda' if hours % 10 == 1 and hours % 100 != 11 else 'stundas'}"
            if self.lang == "es":
                return f"{hours} hora{'s' if hours != 1 else ''}"
            return f"{hours} hour{'s' if hours != 1 else ''}"
        h, m = {"ru": ("ч", "мин"), "lv": ("st.", "min")}.get(self.lang, ("h", "min"))
        if minutes > 60:
            return f"{hours} {h} {rest} {m}"
        return f"{minutes} {m}"


ENGLISH = Texts()

