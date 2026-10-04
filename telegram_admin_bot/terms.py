"""The terms of service: versions, acceptances, and whether sign-up is open.

- A version is immutable once published (migration 0007 refuses UPDATE and
  DELETE). The newest version is the one shown; publishing again is the
  only way to change the text.
- `requires_acceptance` marks a version every client has to accept before
  their dashboard opens again (owner_auth.current_owner). A version
  published without it (a typo fix) is shown but asks nobody to accept
  again: the required version stays the newest one that did require it.
- Every acceptance is kept with its time, address and browser, and stays
  after the login is deleted.
- Text format, deliberately tiny so the pages can render it with
  textContent only (no HTML, nothing to inject): a line starting "## " is a
  heading, "- " a bullet, a blank line ends a paragraph.
- A body still containing PLACEHOLDER can't be published: the starter text
  marks the parts only the platform's owner can write that way.

Sign-up (platform_settings 'signup') is closed by default and can only be
opened once a version is published.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import asyncpg

import audit

MAX_TITLE = 200
MAX_BODY = 100_000
MAX_NOTE = 1000
PLACEHOLDER = "[[FILL IN"

EVENT_PUBLISHED = "terms_published"
EVENT_ACCEPTED = "terms_accepted"
EVENT_SIGNUP_SETTINGS = "signup_settings_changed"

DEFAULT_SIGNUP = {"enabled": False}


def _iso(value: Any) -> Optional[str]:
    return value.isoformat(timespec="seconds") if value else None


def _version(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "version": row["version"], "title": row["title"], "body": row["body"],
        "change_note": row["change_note"], "requires_acceptance": row["requires_acceptance"],
        "published_by": row["published_by"], "published_at": _iso(row["published_at"]),
    }


# ------------------------------------------------------------------ reading


async def latest(executor: Any) -> Optional[dict[str, Any]]:
    """The version shown to everyone, or None before the first is published."""
    row = await executor.fetchrow("SELECT * FROM terms_versions ORDER BY version DESC LIMIT 1")
    return _version(row) if row else None


async def required_version(executor: Any) -> Optional[int]:
    """The newest version everyone must have accepted; None = none yet."""
    return await executor.fetchval("SELECT max(version) FROM terms_versions WHERE requires_acceptance")


async def accepted_version(executor: Any, owner_id: int) -> Optional[int]:
    return await executor.fetchval("SELECT max(version) FROM terms_acceptances WHERE owner_id = $1", owner_id)


def is_current(required: Optional[int], accepted: Optional[int]) -> bool:
    return required is None or (accepted is not None and accepted >= required)


async def history(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    """Every version, newest first, with how many logins accepted it."""
    rows = await pool.fetch(
        """
        SELECT v.*, (SELECT count(DISTINCT owner_id) FROM terms_acceptances a WHERE a.version = v.version)
               AS accepted_by
          FROM terms_versions v ORDER BY v.version DESC
        """
    )
    return [{**_version(r), "accepted_by": r["accepted_by"]} for r in rows]


async def outstanding(pool: asyncpg.Pool) -> int:
    """Active client logins that have not accepted the required version."""
    required = await required_version(pool)
    if required is None:
        return 0
    return await pool.fetchval(
        """
        SELECT count(*) FROM owners o
         WHERE o.status = 'active' AND NOT o.disabled
           AND coalesce((SELECT max(version) FROM terms_acceptances a WHERE a.owner_id = o.id), 0) < $1
        """,
        required,
    )


async def acceptances(pool: asyncpg.Pool, owner_id: int) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        "SELECT version, accepted_at, ip FROM terms_acceptances WHERE owner_id = $1 ORDER BY id DESC", owner_id,
    )
    return [{"version": r["version"], "accepted_at": _iso(r["accepted_at"]), "ip": r["ip"]} for r in rows]


# ------------------------------------------------------------------ writing


def check(title: str, body: str, change_note: str = "") -> tuple[str, str, str]:
    """The cleaned title, body and note, or ValueError for the admin."""
    title, body, change_note = title.strip(), body.strip().replace("\r\n", "\n"), change_note.strip()
    if not title:
        raise ValueError("The terms need a title.")
    if len(title) > MAX_TITLE:
        raise ValueError(f"The title is limited to {MAX_TITLE} characters.")
    if not body:
        raise ValueError("The terms can't be empty.")
    if len(body) > MAX_BODY:
        raise ValueError(f"The terms are limited to {MAX_BODY} characters.")
    if len(change_note) > MAX_NOTE:
        raise ValueError(f"The note on what changed is limited to {MAX_NOTE} characters.")
    unfilled = [line.strip() for line in body.splitlines() if PLACEHOLDER in line]
    if unfilled:
        raise ValueError(f"{len(unfilled)} part(s) still say {PLACEHOLDER} …]]. Write them first; "
                         f"the first one: {unfilled[0][:160]}")
    return title, body, change_note


async def publish(pool: asyncpg.Pool, *, title: str, body: str, change_note: str = "",
                  requires_acceptance: bool = True, actor: str) -> dict[str, Any]:
    title, body, change_note = check(title, body, change_note)
    async with pool.acquire() as con, con.transaction():
        first = await latest(con) is None
        row = await con.fetchrow(
            "INSERT INTO terms_versions (title, body, change_note, requires_acceptance, published_by) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING *",
            title, body, change_note, requires_acceptance or first, actor,
        )
        await audit.record(con, tenant_id=None, actor=actor, event=EVENT_PUBLISHED,
                           reason=change_note or "terms published",
                           payload={"version": row["version"], "requires_acceptance": row["requires_acceptance"],
                                    "chars": len(body)})
    return _version(row)


async def accept(executor: Any, *, owner_id: int, username: str, version: int, ip: str,
                 user_agent: str) -> None:
    await executor.execute(
        "INSERT INTO terms_acceptances (owner_id, username, version, ip, user_agent) VALUES ($1, $2, $3, $4, $5)",
        owner_id, username, version, ip[:100], user_agent[:300],
    )


# ------------------------------------------------------------------ sign-up


async def signup_settings(executor: Any) -> dict[str, Any]:
    value = await executor.fetchval("SELECT value FROM platform_settings WHERE key = 'signup'")
    value = json.loads(value) if isinstance(value, str) else (value or {})
    return {**DEFAULT_SIGNUP, **value}


async def save_signup_settings(pool: asyncpg.Pool, *, enabled: bool, actor: str) -> dict[str, Any]:
    if enabled and await latest(pool) is None:
        raise ValueError("Publish the terms of service first: nobody can sign up without accepting them.")
    before = await signup_settings(pool)
    value = {**before, "enabled": bool(enabled)}
    async with pool.acquire() as con, con.transaction():
        await con.execute(
            "INSERT INTO platform_settings (key, value, updated_by) VALUES ('signup', $1::jsonb, $2) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, "
            "updated_at = now()",
            json.dumps(value), actor,
        )
        await audit.record(con, tenant_id=None, actor=actor, event=EVENT_SIGNUP_SETTINGS,
                           reason="sign-up opened" if enabled else "sign-up closed",
                           payload={"before": before, "after": value})
    return value


async def signup_open(pool: asyncpg.Pool) -> bool:
    return bool((await signup_settings(pool))["enabled"]) and await latest(pool) is not None


# ------------------------------------------------------------- the starter

STARTER_TITLE = "Terms of Service"

# The parts marked [[FILL IN: …]] are the platform owner's to write; the
# rest is a strict default for an AI reply assistant running on a client's
# own Telegram or WhatsApp account. It is a starting point, not legal advice.
STARTER_BODY = """\
These Terms of Service ("Terms") are a binding agreement between you and [[FILL IN: your company's legal name, registration number and registered address]] ("we", "us"). By ticking the acceptance box, creating an account, signing in, or using the service you confirm that you are at least 18 years old and you accept these Terms. If you are under 18 or do not accept them, do not use the service.

## 1. The service
We provide an AI assistant that reads and answers messages on a Telegram or WhatsApp account you connect, takes booking requests, and shows you a dashboard ("the service").
- The assistant writes its replies with a third-party AI model. Replies can be wrong, incomplete or inappropriate. You are responsible for checking how it answers your customers.
- We may change, add or remove features at any time.
- We do not promise that the service is available at any particular time or free of errors.

## 2. Who may use it
- You must be at least 18 years old, or the age of majority where you live if that is higher. By accepting these Terms you confirm that you are. Accepting them while under age is a breach of these Terms, and you alone are responsible for its consequences.
- Use the service only for a business you own or are authorised to act for.
- We may ask you at any time for proof of your identity and age. If you do not provide it, we may suspend your account.
- The information you give us when you sign up must be true and kept up to date.
- We approve accounts at our own discretion and may refuse one without giving a reason.
- One login is for one person. Do not share it. Keep your password secret and turn on two-step sign-in.
- You are responsible for everything done with your login. Tell us within 24 hours if you think someone else has used it.

## 3. Your messaging accounts
- You may only connect a Telegram or WhatsApp account or phone number that you own or are authorised to use.
- You must follow the terms and policies of Telegram and WhatsApp. Using an automated assistant on a personal account can break those terms, and Telegram or WhatsApp may limit, suspend or ban the account at any time.
- We are not responsible for any restriction, suspension, ban or loss of a messaging account, its contacts or its message history, whatever the cause.

## 4. Acceptable use
You must not use the service, or let it be used, to:
- send spam, bulk or unsolicited messages, or message people who have not contacted you first or agreed to hear from you;
- offer, sell or promote anything illegal where you or your customers are, or anything involving weapons, drugs, gambling or counterfeit goods;
- offer adult or escort services in any way not allowed by section 5;
- run or promote scams, pyramid schemes, investment or cryptocurrency offers, loans, or requests for payment details, passwords or codes;
- harass, threaten, deceive or discriminate against anyone, or publish hateful content;
- contact anyone under 18 for marketing, or collect sensitive personal data (health, religion, sexuality, ethnicity, political views, criminal records, financial account details) through the assistant;
- impersonate another person or business, or hide that customers are talking to an automated assistant where the law requires you to tell them;
- give medical, legal or financial advice through the assistant as if it came from a qualified professional;
- get around the limits, safety checks or pauses we put on the service, or test, probe, copy, reverse engineer or overload it;
- resell, sublicense or give access to the service to anyone else.
Breaking any of these rules allows us to stop the service immediately (see section 9).

## 5. Adult and escort services
Escort services are allowed only when all of the following are true:
- you are an independent adult offering only your own services, for yourself and on your own behalf;
- you do so voluntarily, and nobody else controls, directs or takes a share of that work or of your earnings;
- the services, and the way you advertise and arrange them, are legal where you offer them and where your customers are, and you meet every local rule that applies (registration, health, tax and advertising).
The following are forbidden, and we will close the account at once:
- pimping, procuring or managing: arranging, advertising, booking or profiting from another person's sexual services, including as an agency, manager, driver or "booker";
- one account handling bookings for more than one worker;
- anyone under 18, or anyone who appears to be under 18, in any role or any content;
- any sign of coercion, trafficking, debt bondage or exploitation;
- sending sexually explicit images or videos through the assistant. It may handle availability, prices, bookings and practical information.
We may ask for proof of age, identity and independence at any time. Where we suspect a minor, trafficking or exploitation, we suspend the account without notice, keep the relevant records, and report it to the authorities.
We provide software only. We do not offer, arrange, advertise, take part in or take any share of the services you provide or of your earnings. We are not a party to any agreement between you and your customers.

## 6. Your content and instructions
- You are responsible for the information you give the assistant (prices, services, opening hours, answers, files) and for keeping it correct.
- You allow us to store and process that information, and your conversations, only to run, secure and support the service.
- You must not give the assistant anything you do not have the right to use.
- You alone are responsible and liable for how the service is used through your account, for every message the assistant sends for you, for the services you offer, and for your compliance with every law and platform rule that applies to you. We do not check your business, your customers or your messages before they are sent.

## 7. Personal data
- For your customers' personal data you are the controller and we act as your processor. You must have a lawful basis for processing it and tell your customers, in your own privacy notice, that an automated assistant answers messages.
- We process personal data as described in [[FILL IN: link to your privacy policy and data processing agreement]].
- We use subprocessors to run the service, including hosting providers and AI model providers. Messages are sent to the AI model provider to write replies.
- We keep conversation data for [[FILL IN: how long, e.g. 12 months]] and delete it within [[FILL IN: number]] days after your account ends, unless the law requires us to keep it longer.

## 8. Safety, monitoring and moderation
- We monitor the service for misuse. Our staff (administrators and moderators) may read conversations, settings and logs to investigate problems, enforce these Terms, and give support.
- Automatic safety checks may hold back a reply, pause a conversation, or pause your assistant (for example on unusual sending volume, a new login to your account, reaching a usage limit, or suspicious content). A paused assistant does not answer messages received during the pause later.
- We may pause, limit or switch off your assistant, or disconnect your messaging account, at any time and without notice when we believe it is needed to protect you, your customers, other clients, the platform or third parties.
- We keep a record of actions taken on your account.

## 9. Suspension and termination
- We may suspend or close your account immediately, without notice, if you break these Terms, do not pay, give false information, or if we are required to by law or by Telegram or WhatsApp.
- You may close your account by giving [[FILL IN: notice period, e.g. 14 days]] written notice to [[FILL IN: contact e-mail]].
- When your account ends the assistant stops, and we delete your data as described in section 7. Fees already paid are not refunded except as stated in section 10.

## 10. Fees and payment
- Prices and billing period: [[FILL IN: prices, billing period, currency and how invoices are sent]].
- Payment method and due date: [[FILL IN: how and by when clients pay]].
- If a payment is late, the service enters a grace period; when it ends unpaid, the assistant is paused until payment is received.
- Refunds: [[FILL IN: your refund policy]].
- Fees are a fixed subscription for the software. They never depend on your bookings, customers or earnings.

## 11. No warranty
The service is provided "as is" and "as available". To the fullest extent the law allows, we give no warranty of any kind, express or implied, including that the service or the assistant's replies are accurate, suitable for your purpose, uninterrupted or secure.

## 12. Limitation of liability
- To the fullest extent the law allows, we are not liable for any indirect, incidental, special or consequential loss, including lost profits, lost bookings, lost customers, lost data, or the loss or ban of a messaging account.
- Our total liability for all claims arising from the service is limited to [[FILL IN: your liability cap, e.g. the fees you paid in the 3 months before the claim]].
- We are not liable for the services you offer, your dealings with your customers, or anything you or your customers do. That liability is yours alone.
- Nothing in these Terms limits liability that cannot be limited by law.

## 13. Indemnity
You will defend us and compensate us in full for any claim, loss, fine, penalty or cost, including reasonable legal costs, that arises from your use of the service, your content, the services you offer, your customers, your breach of these Terms or of any law, your breach of Telegram's or WhatsApp's terms, or any claim or investigation by a third party or an authority about any of these.

## 14. Changes to these Terms
We may change these Terms. When a change matters, you will be asked to accept the new version the next time you sign in, and you cannot use the dashboard until you do. If you do not accept, stop using the service and close your account as described in section 9.

## 15. Law and disputes
These Terms are governed by [[FILL IN: governing law, e.g. the laws of the Republic of Latvia]]. Disputes go to [[FILL IN: competent courts]].

## 16. Contact
[[FILL IN: support e-mail address and postal address]]
"""
