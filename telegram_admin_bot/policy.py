"""Outbound policy: hard rules checked in code before an AI-written reply
goes out.

The prompt asks the model to behave. This does not ask: it reads the text
that is about to be sent and holds it for a human if it breaks a rule.
Customers will try prompt injection, and a tenant's own prompt text may be
wrong, so the prompt is not the defence; this is.

A reply that fails any check is never sent automatically. It becomes a
draft waiting for approval, with the reasons shown next to it, and an
audit row is written (session_runtime.py). Nothing is ever silently
rewritten or dropped.

Checks, in order:
- links to domains that are not allowed (config allowed_link_domains, or
  written in the business's own prompt sections)
- crypto wallet addresses and IBANs the business has not written itself
- phone numbers / e-mail addresses not in config shareable_contacts or the
  business text
- a price below the configured floor (price_floors)
- a banned topic (banned_topics)
- a promise (discount, refund, free, guarantee) the business has not made
  in its own text

"The business text" is the rendered industry + client sections
(prompt_layers.Rendered.business_text): if the business wrote a link or an
IBAN into its own prompt, the bot repeating it is expected.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

# Bare domains only count with a TLD from this list; with a scheme or www.
# anything does. Keeps "file.txt" or "Mr.Smith" from reading as links.
COMMON_TLDS = {
    "com", "net", "org", "info", "biz", "io", "co", "me", "app", "dev", "xyz", "top", "site",
    "online", "shop", "store", "link", "click", "ly", "gl", "to", "cc", "tk", "gg", "ai",
    "lv", "lt", "ee", "ru", "ua", "by", "eu", "de", "uk", "pl", "fi", "se", "no",
}
_URL = re.compile(
    r"(?i)\b(?:https?://|www\.)([a-z0-9-]+(?:\.[a-z0-9-]+)+)"
    # A bare domain, but not the part of an e-mail address after the @.
    r"|(?<![@\w.-])([a-z0-9-]+(?:\.[a-z0-9-]+)*\.([a-z]{2,10}))(?=[/\s.,!?)]|$)"
)
_WALLETS = (
    ("a Bitcoin address", re.compile(r"\b(?:bc1[02-9ac-hj-np-z]{25,59}|[13][1-9A-HJ-NP-Za-km-z]{25,34})\b")),
    ("an Ethereum address", re.compile(r"\b0x[a-fA-F0-9]{40}\b")),
    ("a TRON address", re.compile(r"\bT[1-9A-HJ-NP-Za-km-z]{33}\b")),
)
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,3})?\b")
_EMAIL = re.compile(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
_PHONE = re.compile(r"(?<![\w+])(?:\+|00)?\d(?:[\s-]?\d){7,14}(?![\w])")
_FULL_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_DATE_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}$|^\d{2}-\d{2}-\d{4}$")
_MONEY = re.compile(
    r"(?i)(?:€|eur\b|euro\b|eiro\b|евро\b)\s*(\d+(?:[.,]\d{1,2})?)"
    r"|(\d+(?:[.,]\d{1,2})?)\s*(?:€|eur\b|euro|eiro|евро)"
)
# Stems, matched case-insensitively. Deliberately not bare "free": "a free
# slot at 3" is the most normal sentence a receptionist writes.
PROMISE_TERMS = (
    "discount", "for free", "free of charge", "refund", "money back", "guarantee", "% off", "promo code",
    "atlaid", "bezmaksas", "par brīvu", "garantij", "naudas atgriešan",
    "скидк", "бесплатн", "возврат денег", "гарантир",
)


@dataclass
class Verdict:
    reasons: list[str] = field(default_factory=list)
    # The subset that is the outbound trip-wire (links, wallets, IBANs):
    # the kind of content a hijack or a prompt injection is after. With
    # anomaly.tripwire_suspend it also switches the client off.
    tripwire: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.reasons


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _domain_allowed(domain: str, allowed: list[str], business_lower: str) -> bool:
    domain = domain.lower().strip(".")
    for entry in allowed:
        entry = entry.lower().strip().removeprefix("https://").removeprefix("http://").strip("/")
        entry = entry.removeprefix("www.")
        if entry and (domain == entry or domain.endswith("." + entry) or domain.removeprefix("www.") == entry):
            return True
    return domain in business_lower


def _links(text: str) -> list[str]:
    found = []
    for match in _URL.finditer(text):
        if match.group(1):
            found.append(match.group(1))
        elif match.group(3) and match.group(3).lower() in COMMON_TLDS:
            found.append(match.group(2))
    return found


def _amount(raw: str) -> float:
    return float(raw.replace(",", "."))


def _price_problems(text: str, floors: dict[str, float]) -> list[str]:
    if not floors:
        return []
    problems = []
    lowest = min(floors.values())
    # Sentence by sentence; within one, each price belongs to the service
    # named closest to it ("Haircut 25 EUR, colouring 70 EUR").
    for sentence in re.split(r"(?<=[.!?\n])\s+", text):
        lower = sentence.lower()
        mentions = [
            (m.start(), service) for service in floors
            for m in re.finditer(re.escape(service.lower()), lower)
        ]
        for match in _MONEY.finditer(sentence):
            amount = _amount(match.group(1) or match.group(2))
            if mentions:
                _, service = min(mentions, key=lambda m: abs(m[0] - match.start()))
                if amount < floors[service]:
                    problems.append(
                        f"quotes {amount:g} EUR near '{service}', below its floor of {floors[service]:g} EUR"
                    )
            elif amount < lowest:
                problems.append(f"quotes {amount:g} EUR, below the lowest price floor ({lowest:g} EUR)")
    return problems


def platform_domains() -> list[str]:
    """The platform's own public address (PUBLIC_BASE_URL, the booking
    pages). Reminders link there, so it is always allowed; without this the
    first reminder with a booking link would trip the trip-wire."""
    from urllib.parse import urlparse

    host = urlparse((os.getenv("PUBLIC_BASE_URL") or "").strip()).hostname
    return [host] if host else []


def check_outbound(text: str, config: dict[str, Any], business_text: str = "") -> Verdict:
    """Everything wrong with `text` as an automatic reply, as sentences an
    operator can act on. An empty list means it may go out."""
    verdict = Verdict()
    business_lower = (business_text or "").lower()
    business_digits = _digits(business_text or "")
    shareable = [c.strip().lower() for c in config.get("shareable_contacts", []) if c.strip()]
    shareable_digits = [_digits(c) for c in shareable if _digits(c)]
    allowed_domains = list(config.get("allowed_link_domains", [])) + platform_domains()

    for domain in _links(text):
        if not _domain_allowed(domain, allowed_domains, business_lower):
            verdict.tripwire.append(f"links to {domain}, which is not an allowed domain")

    for label, pattern in _WALLETS:
        for match in pattern.finditer(text):
            if match.group(0).lower() not in business_lower:
                verdict.tripwire.append(f"contains {label} the business has not written anywhere")
    for match in _IBAN.finditer(text):
        compact = match.group(0).replace(" ", "")
        if compact.lower() not in business_lower.replace(" ", ""):
            verdict.tripwire.append("contains a bank account number (IBAN) the business has not written anywhere")
    verdict.tripwire = list(dict.fromkeys(verdict.tripwire))
    verdict.reasons.extend(verdict.tripwire)

    # A link was judged above as a whole; digits or an @ inside it (an IP
    # written with dashes, a path) are not a phone number or an address.
    plain = _FULL_URL.sub(" ", text)
    for match in _EMAIL.finditer(plain):
        email = match.group(0).lower()
        if email not in shareable and email not in business_lower:
            verdict.reasons.append(f"shares the e-mail address {email}, which is not in shareable_contacts")
    for match in _PHONE.finditer(plain):
        raw = match.group(0).strip()
        if _DATE_LIKE.match(raw):
            continue
        digits = _digits(raw)
        known = any(digits.endswith(d[-8:]) or d.endswith(digits[-8:]) for d in shareable_digits)
        if not known and digits not in business_digits:
            verdict.reasons.append(f"shares the phone number {raw}, which is not in shareable_contacts")

    verdict.reasons.extend(_price_problems(text, config.get("price_floors") or {}))

    lower = text.lower()
    for topic in config.get("banned_topics", []):
        if topic.strip() and re.search(r"(?<!\w)" + re.escape(topic.strip().lower()), lower):
            verdict.reasons.append(f"mentions the banned topic '{topic.strip()}'")

    for term in PROMISE_TERMS:
        if term in lower and term not in business_lower:
            verdict.reasons.append(f"promises '{term}', which the business has not offered in its own text")

    # One line per distinct problem, in the order found.
    verdict.reasons = list(dict.fromkeys(verdict.reasons))
    return verdict


def escalation_match(text: str, keywords: list[str]) -> str:
    """The first escalation keyword in a customer's message, or "". A
    keyword matches as a word or the start of one, in any case, the same
    way banned_topics do."""
    lower = (text or "").lower()
    for keyword in keywords:
        word = keyword.strip().lower()
        if word and re.search(r"(?<!\w)" + re.escape(word), lower):
            return keyword.strip()
    return ""
