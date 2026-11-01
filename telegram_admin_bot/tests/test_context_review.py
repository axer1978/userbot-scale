"""Context review, the pure part (context_review.py): how a reply is built
(the pieces in the order the model reads them, where each comes from, the
warnings), and the review's chats (who wrote each line, masking, outcomes,
which chats fit), the check of the model's answer and the repeated lines.

All data is made up: the names, numbers and addresses below are fakes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import context_review as cr
import finetune
import prompt_layers
import tenant_config
import tenants

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
RIGA = ZoneInfo("Europe/Riga")
ADDRESS = "Fake Street 12\nRiga Centre"
WAY_IN = "door code 4321\nfloor 3"

SECTIONS = {
    "about": "Independent companion.",
    "services": "1 hour 150.",
    "hours_location": "Every day, in the centre.",
    "booking": "Ask for his time first. Prices only when he asks.",
    "faq": "Parking: in the yard.",
    "tone": "Warm, short.",
    "boundaries": "Adults only.",
    "sign_off": "kisses",
    "writing_samples": "hi love 😘 what time?",
}


def bundle(*, sections=None, client=None, config=None, rules=(), channel="telegram", pin=None, name="pam",
           base="Never say you are a bot. You write in a Telegram chat."):
    config = config or {}
    sections = SECTIONS if sections is None else sections
    industry = {"id": 1, "name": "Escort", "template_version": 12, "default_config": {}, "config_revision": 0}
    tenant = {"id": 6, "name": name, "industry_id": 1, "status": "active", "channel": channel,
              "session_id": "acct-pam", "config_json": config, "config_revision": 0, "prompt_version": 3,
              "prompt_pin_version": pin}
    resolved = tenant_config.resolve({}, config)
    layers = {"base": {"rules": base}, "industry": {"sections": dict(sections)},
              "client": client or {"overrides": {}, "addendum": ""}, "industry_version": pin or 12,
              "rules": list(rules)}
    prompt = prompt_layers.render(base=layers["base"], industry=layers["industry"], client=layers["client"],
                                  business_name=name, language_policy=resolved.config.language_policy,
                                  versions=(1, 1, pin or 12, 3), rules=list(rules))
    return tenants.Bundle(tenant=tenant, industry=industry, resolved=resolved, inherited=resolved.as_dict(),
                          prompt=prompt, layers=layers)


def piece(view, key):
    return next(p for p in view["pieces"] if p["key"] == key)


ON = {"booking": {"enabled": True, "provider": "@her_booker", "address": ADDRESS},
      "style_card": {"enabled": True}, "retrieval": {"enabled": True}}


# --------------------------------------------------------- how a reply is built


def test_the_pieces_come_in_the_order_the_model_reads_them():
    view = cr.context_view(bundle())
    keys = [p["key"] for p in view["pieces"]]
    assert keys == (["platform_rules", "business"] + [f"section:{k}" for k in prompt_layers.SECTION_KEYS]
                    + ["language", "addendum", "rules", "style_card", "per_chat", "their_style", "linked_chat",
                       "examples", "appointments", "media", "burst", "no_reply", "history", "closing"])
    assert [p["part"] for p in view["pieces"]][-2:] == ["history", "closing"]
    assert {p["part"] for p in view["pieces"][:-2]} == {"system"}
    assert view["account"] == "pam" and view["network"] == "telegram" and view["version_tag"] == "b1/i1v12/c3"
    assert view["industry"] == {"id": 1, "name": "Escort", "template_version": 12}


def test_the_layer_pieces_are_the_rendered_prompt_exactly():
    b = bundle(client={"overrides": {"services": {"mode": "override", "text": "Own rates."}},
                       "addendum": "Cash only."}, rules=["Never  send the address twice."])
    view = cr.context_view(b)
    in_layers = {"platform_rules", "business", "language", "addendum", "rules"}
    layers = [p["text"] for p in view["pieces"]
              if (p["key"] in in_layers or p["key"].startswith("section:")) and p["enabled"]]
    assert "\n\n".join(layers + [prompt_layers.PRECEDENCE_FOOTER]) == b.prompt.text
    # The system message starts with them, and goes on with every piece after it.
    assert view["system_text"].startswith(b.prompt.text + "\n\n")
    assert piece(view, "burst")["text"] in view["system_text"]
    assert "[HOW THIS PERSON WRITES: measured from their messages in this chat]" in view["system_text"]
    assert view["size"]["system_chars"] == len(view["system_text"])
    assert view["size"]["approx_tokens"] == -(-len(view["system_text"]) // 4)
    assert view["size"]["history_messages"] == 60
    # A dynamic piece has no size of its own.
    assert piece(view, "their_style")["chars"] == 0 and piece(view, "history")["chars"] == 0
    assert piece(view, "burst")["chars"] == len(piece(view, "burst")["text"])


def test_each_section_says_where_it_comes_from():
    b = bundle(sections={**SECTIONS, "tone": "", "faq": "Industry faq."}, client={"overrides": {
        "booking": {"mode": "override", "text": "Her own booking steps."},
        "faq": {"mode": "append", "text": "Her own answers."},
        "tone": {"mode": "override", "text": "Her tone."},
    }, "addendum": ""})
    view = cr.context_view(b)
    booking, faq, tone = piece(view, "section:booking"), piece(view, "section:faq"), piece(view, "section:tone")
    assert (booking["source"], booking["source_label"]) == ("client", "Own text — replaces the industry's")
    assert booking["inherited"] == SECTIONS["booking"]
    assert booking["text"] == "HOW BOOKING WORKS:\nHer own booking steps."
    assert booking["edit"]["href"] == "/clients?node=tenant-6&tab=prompt"
    assert (faq["source"], faq["source_label"]) == ("industry+client", "Industry + own addition")
    assert faq["text"].endswith("Industry faq.\nHer own answers.") and faq["inherited"] == "Industry faq."
    assert (tone["source"], tone["source_label"]) == ("client", "Own text (the industry has none)")
    assert "inherited" not in tone
    about = piece(view, "section:about")
    assert (about["source"], about["source_label"]) == ("industry", "Industry Escort v12")
    assert about["edit"] == {"label": "Clients → Escort → Template",
                             "href": "/clients?node=industry-1&tab=template"}
    empty = piece(cr.context_view(bundle(sections={**SECTIONS, "sign_off": ""})), "section:sign_off")
    assert (empty["source"], empty["enabled"], empty["chars"], empty["source_label"]) == ("empty", False, 0, "Not set")
    assert piece(view, "section:writing_samples")["note"] == "Rendered last of the boxes: the model follows it most."


def test_on_whatsapp_the_prompt_says_whatsapp():
    tg = cr.context_view(bundle())
    wa = cr.context_view(bundle(channel="whatsapp"))
    assert "Telegram chat" in piece(tg, "platform_rules")["text"]
    assert "WhatsApp chat" in piece(wa, "platform_rules")["text"] and "Telegram" not in wa["system_text"]
    assert wa["network"] == "whatsapp"


def test_the_runtime_pieces_follow_the_config_and_the_notes():
    notes = cr.Notes(style_note="HOW SHE TYPES: short", style_enabled=True, style_replies=40,
                     media_note="PHOTOS AND VIDEOS YOU CAN SEND: ...\n- [send 1] = photo #1: beach", media_files=1,
                     greeting=({"id": 2, "kind": "video", "description": "hi"},), rules_industry=2, per_chat=3)
    view = cr.context_view(bundle(config={**ON, "replies": {"no_reply_instruction": "Spam."}}, rules=["Rule one.",
                                                                                                       "Rule two."]),
                           notes)
    style = piece(view, "style_card")
    assert style["enabled"] and style["text"] == "HOW SHE TYPES: short" and "40" in style["source_label"]
    media = piece(view, "media")
    assert media["enabled"] and "[only with the first reply in a chat:]" in media["text"]
    assert "video #2: hi" in media["text"] and "[only once files went out in the chat:]" in media["text"]
    # What only some replies get is shown, never counted.
    assert media["chars"] == len(notes.media_note) and notes.media_note in view["system_text"]
    assert "video #2" not in view["system_text"] and "[only" not in view["system_text"]
    assert media["edit"]["href"] == "/?modal=media&session=acct-pam"
    assert piece(view, "per_chat")["edit"]["href"] == "/?modal=style&session=acct-pam"
    assert piece(view, "appointments")["enabled"] and "[address]" in piece(view, "appointments")["text"]
    assert piece(view, "no_reply")["text"].startswith("WHEN NOT TO REPLY (from the business): Spam.")
    assert piece(view, "rules")["source"] == "industry"
    assert piece(view, "rules")["edit"]["href"] == "/finetune?tab=rules&tenant=6"
    assert piece(view, "per_chat")["enabled"] and piece(view, "examples")["enabled"]
    off = cr.context_view(bundle())
    assert not piece(off, "style_card")["enabled"] and not piece(off, "media")["enabled"]
    assert piece(off, "media")["source"] == "empty" and not piece(off, "appointments")["enabled"]
    assert not piece(off, "no_reply")["enabled"] and "[notes for this person" not in off["system_text"]
    # The address itself is never shown, only that it is set.
    assert "Fake Street" not in json.dumps(view)


def test_the_steps_before_and_after_show_the_accounts_values():
    config = {"quiet_hours": {"enabled": True, "start": "23:00", "end": "08:00"}, "auto_send": True,
              "staging": {"enabled": True, "test_chats": ["tester_one"]}, "variation": {"enabled": False}}
    view = cr.context_view(bundle(config=config), cr.Notes(off_reason="paused by the admin"))
    before = {s["key"]: s for s in view["before"]}
    after = {s["key"]: s for s in view["after"]}
    assert before["quiet_hours"]["active"] and "23:00–08:00" in before["quiet_hours"]["detail"]
    assert before["switches"]["active"] and "paused by the admin" in before["switches"]["detail"]
    assert before["staging"]["active"] and "tester_one" not in before["staging"]["detail"]
    assert not before["skip_acknowledgements"]["active"] and not before["reply_limits"]["active"]
    assert after["auto_send"]["active"] and not after["variation"]["active"] and not after["no_reply"]["active"]
    assert [s["key"] for s in view["before"]][:3] == ["reply_delay", "quiet_hours", "switches"]
    # draft_worker's order: the split and the video hold come before the policy check.
    assert [s["key"] for s in view["after"]] == ["no_reply", "media_tags", "address", "fallback", "variation",
                                                 "burst", "videos_need_approval", "policy", "auto_send"]
    calm = cr.context_view(bundle())
    assert not {s["key"]: s for s in calm["before"]}["quiet_hours"]["active"]
    assert not {s["key"]: s for s in calm["after"]}["auto_send"]["active"]


# ------------------------------------------------------------------- warnings


def texts(view, level=None):
    return [w["text"] for w in view["warnings"] if level is None or w["level"] == level]


def test_a_clean_account_has_no_warning_only_notes():
    view = cr.context_view(bundle(config=ON))
    assert texts(view, "warn") == []
    # The writing samples are always worth knowing about.
    assert any("rendered last of the boxes" in t for t in texts(view, "info"))


def test_an_own_text_over_the_industrys_hides_its_fixes():
    over = cr.context_view(bundle(client={"overrides": {"booking": {"mode": "override", "text": "Old flow."}},
                                          "addendum": ""}))
    [warning] = [w for w in over["warnings"] if "replaces the industry's" in w["text"]]
    assert warning == {"level": "warn", "piece": "section:booking", "edit": {
        "label": "Clients → pam → Prompt", "href": "/clients?node=tenant-6&tab=prompt"},
        "text": "pam's own text replaces the industry's HOW BOOKING WORKS: changes to the industry box never reach "
                "pam."}
    # Nothing to hide when the industry has no text there, or the account only adds to it.
    alone = cr.context_view(bundle(sections={**SECTIONS, "booking": ""}, client={"overrides": {
        "booking": {"mode": "override", "text": "x"}, "faq": {"mode": "append", "text": "y"}}, "addendum": ""}))
    assert not any("replaces the industry's" in t for t in texts(alone))


def test_empty_boxes_are_warned_about_by_how_much_they_cost():
    view = cr.context_view(bundle(sections={**SECTIONS, "services": "", "hours_location": "", "booking": "",
                                            "faq": "", "tone": "", "writing_samples": ""},
                                  config={"booking": {"enabled": True, "provider": "@b", "address": "x"}}))
    warn, info = texts(view, "warn"), texts(view, "info")
    assert any(t.startswith("SERVICES AND PRICES is empty") for t in warn)
    assert any(t.startswith("OPENING HOURS AND LOCATION is empty") for t in warn)
    assert any(t.startswith("HOW BOOKING WORKS is empty while bookings are on") for t in warn)
    for heading in ("FREQUENT QUESTIONS", "TONE AND STYLE", "EXAMPLES OF HOW WE WRITE (match this voice)"):
        assert f"{heading} is empty." in info
    assert not any("rendered last" in t for t in info)
    # Bookings off: an empty booking box is no warning.
    off = cr.context_view(bundle(sections={**SECTIONS, "booking": ""}))
    assert not any("HOW BOOKING WORKS is empty" in t for t in texts(off))


@pytest.mark.parametrize("network, provider, bad", [
    ("telegram", "", "empty"), ("telegram", "@her_booker", None), ("telegram", "+371 2000 0000", None),
    ("telegram", "https://chat.whatsapp.com/AbCd1234", "WhatsApp group"), ("telegram", "group: Bookings", None),
    ("whatsapp", "@her_booker", "Telegram username"), ("whatsapp", "https://t.me/+AbC", "Telegram username"),
    ("whatsapp", "+371 2000 0000", None), ("whatsapp", "https://chat.whatsapp.com/AbCd1234", None),
    ("whatsapp", "group: Bookings", None), ("whatsapp", "anna", "Telegram username"),
])
def test_the_booker_must_be_reachable_on_the_accounts_network(network, provider, bad):
    view = cr.context_view(bundle(channel=network, config={"booking": {"enabled": True, "provider": provider,
                                                                       "address": "x"}}))
    found = [t for t in texts(view, "warn") if "booking.provider" in t]
    if bad is None:
        assert found == []
    else:
        [text] = found
        assert bad in text and (not provider or provider not in text)  # the booker's handle is never echoed


def test_the_address_must_be_set_and_never_typed_into_a_box():
    unset = cr.context_view(bundle(config={"booking": {"enabled": True, "provider": "@b"}}))
    assert any("booking.address is empty" in t for t in texts(unset, "warn"))
    typed = cr.context_view(bundle(sections={**SECTIONS, "services": "1 hour 150 at fake street 12."},
                                   client={"overrides": {}, "addendum": "We are in Riga Centre."},
                                   config={"booking": {"enabled": True, "provider": "@b", "address": ADDRESS}}))
    [text] = [t for t in texts(typed, "info") if t.startswith("The address is typed")]
    assert text == ("The address is typed in SERVICES AND PRICES, ADDITIONAL NOTES FROM THE BUSINESS; use [address] "
                    "so it follows booking.address.")
    assert not any("Fake Street" in w["text"] or "fake street" in w["text"] for w in typed["warnings"])
    clean = cr.context_view(bundle(config={"booking": {"enabled": True, "provider": "@b", "address": ADDRESS}}))
    assert not any("address" in t for t in texts(clean))


def test_a_pin_rules_near_the_cap_and_a_huge_prompt_are_flagged():
    pinned = cr.context_view(bundle(pin=9))
    assert pinned["pinned"] == 9
    assert ("pam's prompt is pinned to Escort v9: newer saves of the industry's boxes (now v12) are not used for "
            "pam.") in texts(pinned, "warn")
    assert piece(pinned, "section:about")["source_label"] == "Industry Escort v9 (pinned)"
    near = cr.context_view(bundle(), cr.Notes(rules_industry=20, rules_account=19))
    assert texts(near, "info").count("20 of 25 operator rules are active for the Escort industry: fold them into "
                                     "the prompt before the cap stops new ones.") == 1
    assert not any("19 of 25" in t for t in texts(near))
    huge = cr.context_view(bundle(sections={**SECTIONS, "faq": "x" * 19_000, "about": "y" * 19_000,
                                            "services": "z" * 5_000}))
    assert any(t.startswith("The system message is 4") for t in texts(huge, "warn"))
    assert not any("The system message is" in t for t in texts(cr.context_view(bundle())))


def test_switched_off_helpers_are_noted_once_each_and_warnings_come_first():
    off = cr.context_view(bundle(config={"human": {"adaptive_style": False}, "variation": {"enabled": False},
                                         "booking": {"enabled": True}}))
    info = texts(off, "info")
    for switch in ("style_card.enabled", "retrieval.enabled", "human.adaptive_style", "variation.enabled"):
        assert sum(switch in t for t in info) == 1
    levels = [w["level"] for w in off["warnings"]]
    assert levels == sorted(levels, key=lambda level: level != "warn") and "warn" in levels
    on = cr.context_view(bundle(config=ON))
    assert not any("style_card.enabled" in t or "retrieval.enabled" in t for t in texts(on))


# ------------------------------------------------------------------ the chats


def row(id, direction, status, text, *, at, model=None, draft=None, machine=False):
    return {"id": id, "direction": direction, "status": status, "text": text, "attachments": [], "created_at": at,
            "llm_model": model, "draft_text": draft, "machine": machine}


T0 = datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc)
CHAT = [
    row(1, "in", "received", "Hi, Janis here, free tonight? +37120001122", at=T0),
    row(2, "out", "sent", "Hi love 😘 what time?", at=T0 + timedelta(minutes=4), model="deepseek-chat"),
    row(3, "out", "rejected", "a draft thrown away", at=T0 + timedelta(minutes=5), model="deepseek-chat"),
    row(4, "out", "pending_approval", "a draft still waiting", at=T0 + timedelta(minutes=5), model="deepseek-chat"),
    row(5, "in", "received", "22:00, where are you Janis asks", at=T0 + timedelta(minutes=6)),
    row(6, "out", "sent", "22 is perfect, see you", at=T0 + timedelta(minutes=8), model="deepseek-chat",
        draft="22:00 ok Janis, I am at Fake Street 12"),
    row(7, "out", "sent", "Fake Street 12\nRiga Centre", at=T0 + timedelta(minutes=8), machine=True),
    row(8, "system", "note", "📅 Booking #3 requested for today 22:00.", at=T0 + timedelta(minutes=9)),
    row(9, "out", "sent", "hey 😘 door code 4321", at=T0 + timedelta(minutes=10)),
]
MASKS = finetune.masks_for(display_name="Janis Ozols", username="janis_lv", address=ADDRESS, way_in=WAY_IN)


def test_each_line_says_who_wrote_it():
    lines = cr.review_lines(CHAT)
    assert [(line["id"], line["kind"]) for line in lines] == [
        (1, "client"), (2, "bot"), (5, "client"), (6, "bot_edited"), (7, "system"), (8, "event"), (9, "by_hand")]
    assert cr.line_counts(lines) == {"client": 2, "bot": 1, "bot_edited": 1, "by_hand": 1, "system": 1}


def test_a_chat_reads_with_labels_and_nothing_personal():
    lines = cr.review_lines(CHAT)
    header = cr.chat_header(ref="C4", account="pam", messages=6, outcome="waiting",
                            answer=cr.first_answer(lines), taken_over=True)
    assert header == ("### C4 — pam — 6 messages — outcome: waiting (a time waits for her answer) — first "
                      "answer after 4 min — taken over by a person")
    text = cr.render_chat(lines, header=header, masks=MASKS, tz=RIGA)
    assert text.splitlines()[1:] == [
        "— Fri 30.10.2026 —",
        "[22:00] Client: Hi, [client] here, free tonight? [number]",
        "[22:04] Bot: Hi love 😘 what time?",
        "[22:06] Client: 22:00, where are you [client] asks",
        "[22:08] Bot (edited by her before sending; the bot had written: «22:00 ok [client], I am at [address]»): "
        "22 is perfect, see you",
        "[22:08] System: [address]",
        "— booking: 📅 Booking #3 requested for today 22:00. —",
        "[22:10] Her (typed by hand): hey 😘 [way in]",
    ]
    for secret in ("Janis", "Ozols", "37120001122", "Fake Street", "Riga Centre", "4321", "draft"):
        assert secret not in text


def test_a_long_chat_keeps_its_start_and_its_end():
    rows = [row(i, "in" if i % 2 else "out", "received" if i % 2 else "sent", f"message {i} " + "x" * 80,
                at=T0 + timedelta(hours=i), model=None if i % 2 else "deepseek-chat") for i in range(1, 121)]
    lines = cr.review_lines(rows)
    text = cr.render_chat(lines, header="### C1", tz=RIGA, max_chars=3_000)
    assert len(text) <= 3_000
    assert "message 1 " in text and "message 120 " in text and "message 60 " not in text
    [cut] = [line for line in text.splitlines() if line.startswith("[…")]
    left = int(cut.split()[1])
    shown = sum(1 for line in text.splitlines() if "message " in line)
    assert left + shown == 120
    # The end starts with its day, as it was cut from a different one.
    after = text.splitlines()[text.splitlines().index(cut) + 1]
    assert after.startswith("— ") and after.endswith(" —")
    assert cr.render_chat(lines[:3], header="### C1", tz=RIGA) == "\n".join(
        ["### C1", *cr._rendered(lines[:3], (), RIGA)])


def test_how_a_chat_stands():
    lines = cr.review_lines(CHAT)
    assert cr.outcome(["confirmed", "cancelled"], lines, NOW) == "booked"
    assert cr.outcome(["completed"], lines, NOW) == "booked"
    assert cr.outcome(["no_show", "cancelled"], lines, NOW) == "booked_no_show"
    assert cr.outcome(["pending", "cancelled"], lines, NOW) == "waiting"
    assert cr.outcome(["requested"], lines, NOW) == "waiting"
    assert cr.outcome(["cancelled"], lines, NOW) == "not_booked"
    # No booking: who wrote last, and how long ago.
    assert cr.outcome([], lines, NOW) == "went_quiet"
    assert cr.outcome([], lines, T0 + timedelta(hours=2)) == "open"
    theirs = cr.review_lines(CHAT[:1])
    assert cr.outcome([], theirs, T0 + timedelta(minutes=30)) == "open"
    assert cr.outcome([], theirs, T0 + timedelta(hours=2)) == "unanswered"
    assert set(cr.OUTCOME_LABELS) == set(cr.OUTCOMES) and len(cr.OUTCOMES) == 7


def test_the_first_answer_is_timed():
    assert cr.first_answer(cr.review_lines(CHAT)) == timedelta(minutes=4)
    assert cr.first_answer(cr.review_lines(CHAT[:1])) is None
    assert [cr.took(timedelta(seconds=s)) for s in (30, 600, 7_800, 3 * 86_400)] == [
        "under a minute", "10 min", "2 h 10 min", "3 days"]
    assert "no answer to his first message" in cr.chat_header(ref=None, account="pam", messages=1, outcome="open",
                                                               answer=None, taken_over=False)
    # An outcome that is its own label is not said twice.
    assert cr.chat_header(ref="C1", account="pam", messages=2, outcome="booked", answer=None, taken_over=False) == (
        "### C1 — pam — 2 messages — outcome: booked — no answer to his first message")


def test_chats_are_picked_in_turns_with_both_sides_of_the_comparison():
    def chat(tenant, outcome, day, chars=100):
        return {"tenant_id": tenant, "outcome": outcome, "last_at": f"2026-10-{day:02d}", "chars": chars,
                "key": f"{tenant}:{outcome}{day}"}

    chats = [chat(6, "went_quiet", 30), chat(6, "open", 29), chat(6, "booked", 20), chat(6, "waiting", 10),
             chat(5, "unanswered", 28), chat(5, "booked", 27), chat(8, "not_booked", 25)]
    planned = cr.plan(chats, [(6, "pam"), (5, "leaui"), (8, "eva")], max_chats=5)
    # Within an account the booked-ish and the others alternate, newest first; the accounts take turns.
    assert [c["key"] for c in planned] == ["6:booked20", "5:booked27", "8:not_booked25", "6:went_quiet30",
                                           "5:unanswered28", "6:waiting10", "6:open29"]
    assert [c["ref"] for c in planned] == ["C1", "C2", "C3", "C4", "C5", None, None]
    assert [c["picked"] for c in planned] == [True] * 5 + [False] * 2
    # The characters count too: one that does not fit is skipped, a smaller one after it can be picked.
    sized = [chat(6, "booked", 30, chars=60), chat(6, "open", 29, chars=50), chat(6, "waiting", 28, chars=30)]
    out = cr.plan(sized, [(6, "pam")], max_chars=100)
    assert [(c["picked"], c["ref"]) for c in out] == [(True, "C1"), (False, None), (True, "C2")]
    assert all("ref" not in c for c in sized)  # the input is not changed


def test_the_models_input_holds_the_prompt_the_boxes_and_the_chats():
    b = bundle(sections={**SECTIONS, "hours_location": "Centre, Fake Street 12."},
               client={"overrides": {"services": {"mode": "override", "text": "Own rates."}}, "addendum": "Cash."},
               config={"booking": {"enabled": True, "address": ADDRESS, "arrival_instructions": WAY_IN}},
               rules=["Ask for his time first."])
    boxes = cr.review_boxes(b)
    assert [box["key"] for box in boxes] == list(cr.BOX_KEYS)
    by_key = {box["key"]: box for box in boxes}
    assert by_key["hours_location"]["text"] == "Centre, [address]."
    assert by_key["services"]["source"] == "own text, replaces the industry's"
    assert by_key["about"]["source"] == "industry text" and by_key["rules"]["text"] == "- Ask for his time first."
    system, user = cr.review_messages(account="pam", boxes=boxes, chats=["### C1 — pam", "### C2 — leaui"],
                                      others=["leaui"])
    assert system == {"role": "system", "content": cr.REVIEW_PROMPT}
    assert "## services — SERVICES AND PRICES (own text, replaces the industry's)\nOwn rates." in user["content"]
    assert "## addendum — ADDITIONAL NOTES FROM THE BUSINESS (own text)\nCash." in user["content"]
    assert user["content"].endswith("THE CHATS (2, from pam, and from leaui):\n\n### C1 — pam\n\n### C2 — leaui")
    assert "Fake Street" not in user["content"]
    for must in ("one JSON object", "\"covered_by\"", "Never write a client's name", "writing_samples, addendum, rules",
                 "Her (typed by hand)", "first answer after"):
        assert must in cr.REVIEW_PROMPT


# --------------------------------------------------------- checking the answer

ANSWER = {
    "summary": "Clients ask for prices and the address first. Answer faster.",
    "questions": [
        {"question": "Where are you?", "chats": ["C2"], "wordings": ["where?"], "her_answer": "", "bot_answer": "x",
         "covered_by": "hours_location", "problem": "", "suggested": None},
        {"question": "How much?", "chats": ["C1", "c3", "C1", "C99", "Janis"], "wordings": ["how much", "price?",
                                                                                         "cost", "rates?"],
         "her_answer": "150 love", "bot_answer": "the rates", "covered_by": "pricing", "problem": "too long",
         "suggested": {"box": "services", "line": "Give the 1 hour rate first."}, "extra": "dropped"},
    ],
    "booking": {"works": [{"point": "Asked for the time at once.", "chats": ["C1"]}],
                "loses": [{"point": "Sent the rates before the time.", "chats": ["C2", "C3"]}],
                "speed": "Booked chats got an answer within 5 minutes."},
    "wrong": [{"what": "Gave the address unasked.", "chats": ["C3"], "why": "booking box", "fix": {"box": "booking",
                                                                                                "line": ""}}],
    "phrases": [{"text": "Hi love 😘 what time?", "note": "Vary it.", "chats": ["C1", "C2", "C3"]}],
    "boxes": [{"boxes": ["booking", "writing_samples", "bogus"], "problem": "Two flows.", "fix": "Rewrite samples."}],
}


def test_a_good_answer_is_checked_into_a_report():
    raw = "Sure! Here it is:\n```json\n" + json.dumps(ANSWER) + "\n```\nHope it helps."
    report, errors = cr.parse_report(raw, ["C1", "C2", "C3"])
    first, second = report["questions"]
    # Most chats first.
    assert first["question"] == "How much?" and first["chats"] == ["C1", "C3"] and first["chat_count"] == 2
    assert first["wordings"] == ["how much", "price?", "cost"] and first["covered_by"] == "none"
    assert first["suggested"] == {"box": "services", "line": "Give the 1 hour rate first."} and "extra" not in first
    assert second["chat_count"] == 1 and second["suggested"] is None
    assert report["booking"]["loses"][0]["chat_count"] == 2 and report["booking"]["speed"].startswith("Booked")
    assert report["wrong"][0]["fix"] is None  # a fix with no line is no fix
    assert report["phrases"][0]["chat_count"] == 3
    assert report["boxes"] == [{"boxes": ["booking", "writing_samples"], "problem": "Two flows.",
                                "fix": "Rewrite samples."}]
    assert set(report) == {"summary", "questions", "booking", "wrong", "phrases", "boxes"}
    assert "questions #2: dropped chat ref C99 (not in this review)." in errors
    assert "questions #2: dropped a chat ref that is not of the form C<n>." in errors
    assert "questions #2: unknown box 'pricing', read as none." in errors
    assert "boxes #1: unknown box 'bogus', read as none." in errors
    assert not any("Janis" in e for e in errors)  # what the model wrote there is never echoed


def test_answers_that_cannot_be_read_or_run_long_are_handled():
    assert cr.parse_report("I could not do it.", ["C1"]) == (None, ["The answer holds no JSON object."])
    assert cr.parse_report("[1, 2]", ["C1"])[0] is None
    assert cr.parse_report("{not json at all", ["C1"])[0] is None
    # Trailing junk after the object.
    report, _ = cr.parse_report('{"summary": "ok", "questions": []} and then {', ["C1"])
    assert report["summary"] == "ok"
    many = {"summary": "s", "questions": [{"question": f"q{i}", "chats": ["C1"] * (i % 3)} for i in range(30)],
            "wrong": [{"what": "w" * 2_000, "chats": []}] * 20, "phrases": "not a list"}
    report, errors = cr.parse_report(json.dumps(many), ["C1"])
    assert len(report["questions"]) == cr.MAX_QUESTIONS and len(report["wrong"]) == cr.MAX_ITEMS
    assert len(report["wrong"][0]["what"]) == cr.TEXT_CHARS and report["phrases"] == []
    assert "phrases is not a list; left out." in errors and "The answer has no boxes." in errors
    assert "The answer has no booking comparison." in errors
    assert report["booking"] == {"works": [], "loses": [], "speed": ""}


# ------------------------------------------------------- repeated bot lines


def test_lines_are_compared_without_case_emojis_punctuation_or_figures():
    assert cr.normalise("Hi LOVE 😘😘, what time?!  At 22:00") == "hi love what time at # #"
    assert cr.normalise("hi love what time at 9 30") == cr.normalise("Hi love, what time? At 22:00")


def say(tenant, chat, text):
    return {"tenant_id": tenant, "chat_id": chat, "text": text}


def test_the_bots_repeated_lines_are_counted_in_code():
    messages = [
        say(6, 1, "Hi love 😘 what time are you coming?"), say(6, 2, "hi love, what time are you coming"),
        say(6, 3, "Hi love 😍 what time are you coming?"), say(6, 3, "Hi love 😘 what time are you coming?"),
        say(5, 9, "Hi love 😘 what time are you coming?"),
        # Only two chats: not counted.
        say(6, 1, "send me a voice message"), say(6, 2, "send me a voice message"),
        # A fixed text of the prompt is meant to be the same.
        *[say(6, c, "30 min 100, 1 hour 150") for c in (1, 2, 3)],
        # Several lines (the rates block) and placeholders are not lines of her own.
        *[say(6, c, "Rates:\n1h 150") for c in (1, 2, 3)], *[say(6, c, "[sent photo #1: beach]") for c in (1, 2, 3)],
        # Too short to say anything.
        *[say(6, c, "ok love") for c in (1, 2, 3)],
        # A line with a client's name in it never reaches the report.
        say(6, 1, "see you soon Janis my dear"), say(6, 2, "see you soon Anna my dear"),
        say(6, 3, "see you soon Janis my dear"), say(6, 4, "see you soon Janis my dear"),
        # The address is shown masked.
        *[say(6, c, "I am at Fake Street 12, come up") for c in (1, 2, 4)],
    ]
    phrases = cr.repeated_phrases(
        messages, prompts={6: "SERVICES AND PRICES:\n30 min 100, 1 hour 150", 5: ""},
        account_masks={6: finetune.masks_for(address=ADDRESS), 5: ()},
        chat_masks={(6, 1): finetune.masks_for(display_name="Janis"), (6, 3): finetune.masks_for(display_name="Janis"),
                    (6, 4): finetune.masks_for(display_name="Janis Ozols")})
    assert phrases == [
        {"text": "Hi love 😘 what time are you coming?", "chats": 4, "times": 5},
        {"text": "I am at [address], come up", "chats": 3, "times": 3},
    ]
    assert "Janis" not in json.dumps(phrases) and "Fake Street" not in json.dumps(phrases)


def test_near_identical_long_lines_are_merged_into_the_commoner_one():
    line = "my love tell me the time you want to come over tonight so I can get ready for you"
    messages = [say(6, c, line) for c in (1, 2)]
    messages += [say(6, 3, line.replace("the time", "what time"))]  # one word changed
    messages += [say(6, 4, "my dear send a photo first")]
    [phrase] = cr.repeated_phrases(messages, prompts={}, account_masks={}, chat_masks={})
    assert phrase == {"text": line, "chats": 3, "times": 3}
    # Short lines are never merged: one word in four is too much.
    short = [say(6, 1, "tell me the time"), say(6, 2, "tell me the time"), say(6, 3, "tell me what time")]
    assert cr.repeated_phrases(short, prompts={}, account_masks={}, chat_masks={}) == []


# ------------------------------------------- what the reviewer's checks found


def test_a_chat_longer_than_a_finetune_run_reads_to_its_end():
    """finetune.chat_lines keeps a run's first 300 lines; the review keeps them all, and cuts the chat itself."""
    rows = [row(i, "in" if i % 2 else "out", "received" if i % 2 else "sent", f"line {i}", at=T0 + timedelta(minutes=i),
                model=None if i % 2 else "deepseek-chat") for i in range(1, 400)]
    lines = cr.review_lines(rows)
    assert len(lines) == 399 and lines[-1]["text"] == "line 399"
    assert len(finetune.chat_lines(rows)) == finetune.MAX_EXAMPLE_MESSAGES  # a finetune run is unchanged
    assert cr.outcome([], lines, T0 + timedelta(minutes=399, hours=2)) == "unanswered"
    text = cr.render_chat(lines, header="### C1", max_chars=2_000)
    assert "Client: line 399" in text and "Client: line 1" in text and "messages left out" in text


def sent(id, text, *, at, model=None, draft=None, reason=None, audit_id=None, machine=False):
    return {**row(id, "out", "sent", text, at=at, model=model, draft=draft, machine=machine),
            "sent_reason": reason, "sent_audit": audit_id}


def test_every_message_of_a_reply_she_edited_is_marked_and_files_are_the_bots():
    approved, auto = cr.APPROVED_DRAFT, cr.AUTOMATIC_REPLY
    at = T0 + timedelta(minutes=1)
    rows = [
        row(1, "in", "received", "hi, price?", at=T0),
        # An automatic reply with a photo: the photo is the bot's, not the system's.
        sent(2, "hi love", at=at, model="deepseek-chat", reason=auto, audit_id=10, machine=True),
        sent(3, "[sent photo #1: beach]", at=at, reason=auto, audit_id=11, machine=True),
        row(4, "in", "received", "and for 2 hours?", at=at),
        # A draft she changed, approved as two messages and a photo (audit rows 20-22, draft_approved 23).
        sent(5, "2 hours 250", at=at, model="deepseek-chat", draft="2h 300", reason=approved, audit_id=20),
        sent(6, "come at 22?", at=at, model="deepseek-chat", reason=approved, audit_id=21),
        sent(7, "[sent photo #2: dress]", at=at, reason=approved, audit_id=22),
        # A later draft approved as written, and a message she typed herself.
        sent(8, "ok see you", at=at, model="deepseek-chat", reason=approved, audit_id=30),
        sent(9, "kisses", at=at, reason="sent from the panel", audit_id=31),
        sent(10, "[address]", at=at, reason="the address", audit_id=32, machine=True),
    ]
    lines = cr.review_lines(rows, approvals=[(23, 5)])
    assert [(line["id"], line["kind"]) for line in lines] == [
        (1, "client"), (2, "bot"), (3, "bot"), (4, "client"), (5, "bot_edited"), (6, "bot_edited"),
        (7, "bot_edited"), (8, "bot"), (9, "by_hand"), (10, "system")]
    text = cr.render_chat(lines, header="### C1", tz=RIGA)
    assert "Bot (edited by her before sending; the bot had written: «2h 300»): 2 hours 250" in text
    assert text.count(f"{cr.EDITED_MORE}: ") == 2 and "Bot: [sent photo #1: beach]" in text
    assert cr.line_counts(lines) == {"client": 2, "bot": 3, "bot_edited": 3, "by_hand": 1, "system": 1}
    # Without its draft_approved row nothing but the draft itself is the edited reply.
    assert [line["kind"] for line in cr.review_lines(rows)][5:7] == ["bot", "bot"]


def test_answers_of_the_wrong_shape_are_read_or_dropped_never_a_crash():
    refs = ["C1", "C2"]
    shapes = [
        {"questions": [{"question": "q", "chats": ["C1"], "wordings": 2}]},
        {"questions": [{"question": "q", "wordings": True, "chats": "C1"}]},
        {"questions": [{"question": "q", "wordings": ["a", 3, None, "b"], "chats": [1, "C2"]}]},
        {"boxes": [{"boxes": 1, "problem": "p", "fix": "f"}]},
        {"boxes": [{"boxes": None}, "not an object", 7]},
        {"booking": {"works": "all good", "loses": [{"point": "p", "chats": {"C1": True}}]}},
        {"questions": {"question": "q"}, "wrong": [{"what": "w", "fix": "not an object", "chats": []}]},
        {"phrases": [{"text": 5, "note": ["x"], "chats": ["C1"]}], "summary": 3},
    ]
    for shape in shapes:
        report, errors = cr.parse_report(json.dumps(shape), refs)
        assert report is not None and isinstance(errors, list), shape
        json.dumps(report)  # what is stored
    report, errors = cr.parse_report(json.dumps(shapes[0]), refs)
    assert report["questions"][0]["wordings"] == [] and "questions #1: its wordings are not a list; left out." in errors
    report, _ = cr.parse_report(json.dumps(shapes[2]), refs)
    assert report["questions"][0]["wordings"] == ["a", "b"] and report["questions"][0]["chats"] == ["C2"]


def test_a_text_where_a_list_belongs_is_a_list_of_one():
    answer = {"summary": "s", "questions": [{"question": "How much?", "chats": "C1", "wordings": "how much for 1 hour?",
                                             "covered_by": "services"}],
              "boxes": [{"boxes": "booking", "problem": "p", "fix": "f"}]}
    report, errors = cr.parse_report(json.dumps(answer), ["C1"])
    question = report["questions"][0]
    assert question["wordings"] == ["how much for 1 hour?"] and question["chats"] == ["C1"]
    assert report["boxes"][0]["boxes"] == ["booking"]
    assert not any("unknown box" in e for e in errors)


def test_the_most_asked_questions_are_kept_when_there_are_too_many():
    refs = [f"C{i}" for i in range(1, 31)]
    questions = [{"question": f"rare {i}", "chats": ["C1"]} for i in range(cr.MAX_QUESTIONS)]
    questions.append({"question": "THE MOST ASKED", "chats": refs})
    report, errors = cr.parse_report(json.dumps({"summary": "s", "questions": questions}), refs)
    assert len(report["questions"]) == cr.MAX_QUESTIONS and report["questions"][0]["question"] == "THE MOST ASKED"
    assert "questions: 1 past the first 20 left out." in errors


def test_an_item_about_chats_not_in_the_review_is_dropped():
    answer = {"summary": "s", "questions": [
        {"question": "only bad refs", "chats": ["C9", "C10"]},
        {"question": "no refs at all", "chats": []},
        {"question": "one good one", "chats": ["C9", "C1"]}],
        "booking": {"works": [{"point": "gone", "chats": ["C7"]}, {"point": "kept"}]},
        "wrong": [{"what": "gone", "chats": "C5"}], "phrases": [{"text": "gone", "chats": ["x"]}]}
    report, errors = cr.parse_report(json.dumps(answer), ["C1"])
    assert [q["question"] for q in report["questions"]] == ["one good one", "no refs at all"]
    assert [p["point"] for p in report["booking"]["works"]] == ["kept"]
    assert report["wrong"] == [] and report["phrases"] == []
    for where in ("questions #1", "booking works #1", "wrong #1", "phrases #1"):
        assert f"{where}: left out, none of its chat refs is in this review." in errors


def test_an_answer_cut_off_by_the_token_limit_says_so():
    whole = json.dumps({"summary": "s", "questions": [{"question": "a {brace} and \\\"quote\\\"", "chats": []}]})
    for cut in (whole[:40], whole[:-1], whole[:-3], "```json\n" + whole[:25]):
        assert cr.cut_off(cut), cut
        assert cr.parse_report(cut, ["C1"]) == (None, [cr.CUT_OFF])
    assert not cr.cut_off(whole) and not cr.cut_off(whole + " and then {") and not cr.cut_off("no json here")
    assert cr.parse_report("no json here", ["C1"]) == (None, ["The answer holds no JSON object."])
    assert "At most 15 questions" in cr.REVIEW_PROMPT and "at most 3 wordings" in cr.REVIEW_PROMPT


def test_a_named_variant_does_not_hide_the_bots_commonest_line():
    line = "hey babe thanks for writing what time would you like to come over today"
    messages = [say(6, c, line) for c in range(1, 41)]
    messages.append(say(6, 99, line.replace("babe", "Mark")))  # in one chat the bot put his name in
    named = {(6, 99): finetune.masks_for(display_name="Mark"), (6, 98): finetune.masks_for(display_name="Anna")}
    [phrase] = cr.repeated_phrases(messages, prompts={}, account_masks={}, chat_masks=named)
    assert phrase == {"text": line, "chats": 41, "times": 41}
    # A line whose every wording names the client it went to is never shown.
    only_named = [say(6, c, f"see you tonight my dear {name}") for c, name in ((1, "Mark"), (2, "Anna"), (3, "Mark"))]
    masks = {(6, 1): finetune.masks_for(display_name="Mark"), (6, 2): finetune.masks_for(display_name="Anna"),
             (6, 3): finetune.masks_for(display_name="MARK")}
    assert cr.repeated_phrases(only_named, prompts={}, account_masks={}, chat_masks=masks) == []


def test_what_the_model_wrote_is_masked_before_it_is_stored():
    masks = finetune.masks_for(display_name="Janis Ozols", address=ADDRESS, way_in=WAY_IN)
    report = {"summary": "JANIS asked for Fake Street 12, call +37120001122", "questions": [
        {"question": "q", "chats": ["C1"], "chat_count": 1, "wordings": ["janis here", "door code 4321?"],
         "covered_by": "booking", "suggested": {"box": "booking", "line": "Never write riga centre."}}],
        "boxes": [{"boxes": ["booking"], "problem": "Ozols", "fix": ""}]}
    masked = cr.mask_report(report, masks)
    assert masked["summary"] == "[client] asked for [address], call [number]"
    assert masked["questions"][0]["wordings"] == ["[client] here", "[way in]?"]
    assert masked["questions"][0]["suggested"] == {"box": "booking", "line": "Never write [address]."}
    assert masked["questions"][0]["chats"] == ["C1"] and masked["boxes"][0] == {"boxes": ["booking"],
                                                                                 "problem": "[client]", "fix": ""}
    assert cr.mask_report("Janis at Fake Street 12", masks) == "[client] at [address]"
