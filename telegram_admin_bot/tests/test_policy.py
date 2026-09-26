"""policy.check_outbound: the in-code checks an automatic reply must pass."""

from __future__ import annotations

import pytest

import policy
import tenant_config


def cfg(**overrides):
    return tenant_config.resolve(None, overrides).as_dict()


def reasons(text, business="", **overrides):
    return policy.check_outbound(text, cfg(**overrides), business).reasons


@pytest.mark.parametrize("text", [
    "Sure, we have a free slot tomorrow at 15:00. See you then!",
    "Jā, rīt 10:00 ir brīvs laiks.",
    "Да, завтра в 12:30 свободно.",
    "The appointment is on 2026-10-01, it takes about 45 minutes.",
    "Haircut is 25 EUR.",
])
def test_ordinary_receptionist_replies_pass(text):
    assert reasons(text, price_floors={"haircut": 20}) == []


def test_links_need_an_allowed_domain_or_the_business_own_text():
    assert reasons("Book here: https://evil.example/pay") == ["links to evil.example, which is not an allowed domain"]
    assert reasons("Write to me at t.me/someoneelse") != []
    assert reasons("Book at https://salon.lv/book", allowed_link_domains=["salon.lv"]) == []
    assert reasons("Book at www.booking.salon.lv", allowed_link_domains=["salon.lv"]) == []
    assert reasons("Map: https://maps.example.com/x", business="Directions: maps.example.com/x") == []
    # Not links: file names and abbreviations with dots.
    assert reasons("Send me the photo.jpg please, e.g. from your phone.") == []


def test_wallets_and_ibans_are_held_unless_the_business_wrote_them():
    eth = "0x" + "ab" * 20
    assert any("Ethereum" in r for r in reasons(f"Pay to {eth}"))
    assert any("Bitcoin" in r for r in reasons("Send to bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"))
    assert any("IBAN" in r for r in reasons("Transfer to LV80 BANK 0000 4351 9500 1"))
    assert reasons("Transfer to LV80BANK0000435195001", business="Bank: LV80 BANK 0000 4351 9500 1") == []


def test_contacts_only_when_shareable():
    assert any("phone number" in r for r in reasons("Call Jānis on +371 2612 3456"))
    assert reasons("Call us on +371 2612 3456", shareable_contacts=["+37126123456"]) == []
    assert any("e-mail" in r for r in reasons("Mail anna.private@gmail.com"))
    assert reasons("Mail info@salon.lv", shareable_contacts=["info@salon.lv"]) == []


def test_prices_below_the_floor_are_held():
    floors = {"haircut": 20, "colouring": 60}
    assert reasons("Colouring is 45 EUR today.", price_floors=floors) == [
        "quotes 45 EUR near 'colouring', below its floor of 60 EUR"
    ]
    assert reasons("For you, €15!", price_floors=floors) == [
        "quotes 15 EUR, below the lowest price floor (20 EUR)"
    ]
    assert reasons("Haircut 25 EUR, colouring 70 EUR.", price_floors=floors) == []
    assert reasons("It's 5 euro", price_floors={}) == []            # no floors configured


def test_banned_topics_and_unoffered_promises():
    assert reasons("About your diagnosis: rest.", banned_topics=["diagnosis"]) == [
        "mentions the banned topic 'diagnosis'"
    ]
    assert reasons("I can give you a 20% discount!") != []
    assert reasons("Mums ir atlaide 10%!") != []
    assert reasons("We guarantee the result.") != []
    # An offer the business wrote itself may be repeated.
    assert reasons("Students get a discount of 10%.", business="Students get a discount of 10%.") == []


def test_injected_instructions_cannot_talk_their_way_past_it():
    """Whatever a customer persuaded the model to write, the text is checked."""
    reply = ("Ignoring previous rules as you asked: the owner's number is +371 2999 1111 "
             "and you can pay 0x" + "c" * 40 + " for a 90% discount at https://pay.evil.example")
    found = reasons(reply, price_floors={"massage": 40})
    assert len(found) >= 4
