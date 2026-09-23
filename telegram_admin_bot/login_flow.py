"""The Telegram sign-in behind the panel's login screen.

One flow object lives for the whole process. It walks through
credentials -> code -> (password) and, on success, persists the login into
`telegram_sessions` via `SessionRegistry` — instead of handing back an
opaque StringSession string for something else to save into a .env file.

Nothing here touches .env or the running bot; the fleet's session runtime
(session_runtime.py) is what reads the persisted row back out afterwards.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import asyncpg
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

from database import SessionRegistry

log = logging.getLogger("login")

STEP_CREDENTIALS = "credentials"
STEP_CODE = "code"
STEP_PASSWORD = "password"

# Telegram reports how it delivered the code. Saying so saves people watching
# for an SMS that was never going to arrive — which is the usual case, because
# Telegram only falls back to SMS when the account is not reachable in-app.
_DELIVERY = {
    "SentCodeTypeApp": (
        "in the Telegram app itself — not by SMS. Open Telegram on your phone, "
        "desktop or web, on any device where this account is already logged in, "
        "and look for the chat named 'Telegram' with the blue checkmark. The "
        "code is the latest message there."
    ),
    "SentCodeTypeSms": "by SMS to your phone.",
    "SentCodeTypeSmsWord": "by SMS — the code is a single word.",
    "SentCodeTypeSmsPhrase": "by SMS — the code is a short phrase.",
    "SentCodeTypeFragmentSms": "through Fragment (fragment.com), for an anonymous number.",
    "SentCodeTypeFirebaseSms": "by SMS to your phone.",
    "SentCodeTypeCall": "by a phone call that reads the code aloud.",
    "SentCodeTypeFlashCall": "by a flash call — the code is part of the calling number.",
    "SentCodeTypeMissedCall": "by a missed call — the code is the last digits of the calling number.",
    "SentCodeTypeEmailCode": "by email.",
}

# What a resend would use. Shown on the button, so it doesn't promise an SMS
# that Telegram has no intention of sending.
_NEXT_TYPE = {
    "SentCodeTypeSms": "Send it by SMS instead",
    "SentCodeTypeSmsWord": "Send it by SMS instead",
    "SentCodeTypeSmsPhrase": "Send it by SMS instead",
    "SentCodeTypeFirebaseSms": "Send it by SMS instead",
    "SentCodeTypeCall": "Call me and read the code out",
    "SentCodeTypeFlashCall": "Call me instead",
    "SentCodeTypeMissedCall": "Call me instead",
    "SentCodeTypeFragmentSms": "Send it through Fragment instead",
}


class LoginError(Exception):
    """A problem the person at the screen can act on. The flow stays put."""


def _flood_message(exc: errors.FloodWaitError) -> str:
    minutes = exc.seconds / 60
    return (
        f"Telegram is rate-limiting login attempts on this number. Wait "
        f"{exc.seconds} seconds (~{minutes:.0f} min) and try again — retrying "
        "sooner makes the wait longer."
    )


def mask_phone(phone: Optional[str]) -> Optional[str]:
    if not phone:
        return None
    if len(phone) <= 6:
        return phone
    return phone[:3] + "•" * (len(phone) - 6) + phone[-3:]


class LoginFlow:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._registry = SessionRegistry(pool)
        self._client: Optional[TelegramClient] = None
        self.session_id: Optional[str] = None
        self.step = STEP_CREDENTIALS
        self.api_id: Optional[int] = None
        self.api_hash: Optional[str] = None
        self.phone: Optional[str] = None
        self.phone_code_hash: Optional[str] = None
        self.delivery: Optional[str] = None
        self.code_length: Optional[int] = None
        # How a resend would arrive, and when Telegram will accept one.
        self.next_label: Optional[str] = None
        self.resend_at: float = 0.0
        # Shown at the top of the login screen, e.g. why a saved session stopped working.
        self.notice: Optional[str] = None

    # ----------------------------------------------------------------- state

    def state(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "session_id": self.session_id,
            "phone": mask_phone(self.phone),
            "delivery": self.delivery,
            "code_length": self.code_length,
            "next_label": self.next_label,
            "resend_in": max(0, round(self.resend_at - time.monotonic())),
            "notice": self.notice,
        }

    async def reset(self, notice: Optional[str] = None) -> None:
        """Back to the first screen, dropping any half-finished login."""
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        self.session_id = None
        self.step = STEP_CREDENTIALS
        self.phone = None
        self.phone_code_hash = None
        self.delivery = None
        self.code_length = None
        self.next_label = None
        self.resend_at = 0.0
        self.notice = notice

    def _remember_delivery(self, sent: Any) -> None:
        name = type(sent.type).__name__
        self.delivery = _DELIVERY.get(name, name)
        self.code_length = getattr(sent.type, "length", None)
        self.phone_code_hash = sent.phone_code_hash

        # next_type is Telegram's decision, not ours: it is the only channel a
        # resend can use, and None means it will not send another one at all.
        next_name = type(sent.next_type).__name__ if sent.next_type else None
        self.next_label = _NEXT_TYPE.get(next_name, "Send the code again" if next_name else None)
        # Telegram rejects a resend before this timeout with a flood wait.
        self.resend_at = time.monotonic() + (getattr(sent, "timeout", None) or 60)

    # ----------------------------------------------------------------- steps

    async def start(
        self,
        session_id: str,
        api_id: int,
        api_hash: str,
        phone: str,
        *,
        label: str = "",
    ) -> None:
        """Send the login code. On return the flow is at the code step.

        Creates (or overwrites the credentials on) the `telegram_sessions`
        row identified by `session_id` before talking to Telegram, so a
        completed login always has somewhere to be saved.
        """
        await self.reset()

        phone = "".join(phone.split())
        if not phone.startswith("+"):
            phone = "+" + phone

        client = TelegramClient(StringSession(), api_id, api_hash)
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
        except errors.FloodWaitError as exc:
            await client.disconnect()
            raise LoginError(_flood_message(exc))
        except errors.PhoneNumberInvalidError:
            await client.disconnect()
            raise LoginError(
                f"Telegram does not recognise {phone} as a valid number. Use "
                "international format: + then country code then the number, "
                "with no spaces or dashes."
            )
        except errors.PhoneNumberBannedError:
            await client.disconnect()
            raise LoginError(f"{phone} is banned from Telegram.")
        except errors.PhoneNumberFloodError:
            await client.disconnect()
            raise LoginError(
                "Too many codes were requested for this number recently. Wait a while before trying again."
            )
        except (errors.ApiIdInvalidError, errors.ApiIdPublishedFloodError):
            await client.disconnect()
            raise LoginError(
                "API ID and API hash are not a matching pair. Recheck both at "
                "https://my.telegram.org → API development tools."
            )
        except Exception as exc:
            await client.disconnect()
            raise LoginError(f"Could not reach Telegram: {type(exc).__name__}: {exc}")

        # The code is on its way — only now do we create/refresh the row, so
        # a rejected phone number or bad api_id/api_hash never creates one.
        try:
            await self._registry.create(session_id, label=label, api_id=api_id, api_hash=api_hash)
        except Exception as exc:
            await client.disconnect()
            raise LoginError(f"Could not save this session's credentials: {exc}")

        self._client = client
        self.session_id = session_id
        self.api_id = api_id
        self.api_hash = api_hash
        self.phone = phone
        self._remember_delivery(sent)
        self.step = STEP_CODE
        log.info("Login code sent to %s for session %r.", mask_phone(phone), session_id)

    async def resend(self) -> None:
        """Ask Telegram to send the code again.

        Which channel it uses is Telegram's call (the next_type it reported),
        not ours — force_sms is deprecated and does nothing. Because the client
        already holds a phone_code_hash for this number, this call resends
        rather than starting a fresh code request.
        """
        if self._client is None or self.step != STEP_CODE:
            raise LoginError("Enter your details first.")
        if self.next_label is None:
            raise LoginError(
                "Telegram will not send this code through another channel. "
                "Check the Telegram app on a device where this account is "
                "already logged in."
            )
        waiting = round(self.resend_at - time.monotonic())
        if waiting > 0:
            raise LoginError(f"Telegram asks you to wait {waiting} more seconds before resending.")
        try:
            sent = await self._client.send_code_request(self.phone)
        except errors.FloodWaitError as exc:
            raise LoginError(_flood_message(exc))
        except errors.PhoneCodeExpiredError:
            await self.reset()
            raise LoginError("That code expired. Start again to get a fresh one.")
        except Exception as exc:
            raise LoginError(f"Could not resend the code: {type(exc).__name__}: {exc}")
        self._remember_delivery(sent)

    async def submit_code(self, code: str) -> Optional[tuple[dict[str, Any], Any]]:
        """Try the code. Returns (session_row, me) when signed in, or None
        when Telegram wants the two-step password next."""
        if self._client is None or self.step != STEP_CODE:
            raise LoginError("Enter your details first.")
        code = "".join(ch for ch in code if ch.isdigit())
        if not code:
            raise LoginError("Enter the code Telegram sent you.")
        try:
            await self._client.sign_in(
                self.phone, code, phone_code_hash=self.phone_code_hash
            )
        except errors.SessionPasswordNeededError:
            self.step = STEP_PASSWORD
            return None
        except errors.PhoneCodeInvalidError:
            raise LoginError("That code is not right. Check it and try again.")
        except errors.PhoneCodeExpiredError:
            await self.reset()
            raise LoginError("That code has expired. Start again to get a fresh one.")
        except errors.PhoneNumberUnoccupiedError:
            await self.reset()
            raise LoginError(
                "There is no Telegram account with this number. Create one in the "
                "Telegram app first, then sign in here."
            )
        except errors.FloodWaitError as exc:
            raise LoginError(_flood_message(exc))
        except Exception as exc:
            raise LoginError(f"Sign-in failed: {type(exc).__name__}: {exc}")
        return await self._finish()

    async def submit_password(self, password: str) -> tuple[dict[str, Any], Any]:
        if self._client is None or self.step != STEP_PASSWORD:
            raise LoginError("Enter the login code first.")
        if not password:
            raise LoginError("Enter your two-step verification password.")
        try:
            await self._client.sign_in(password=password)
        except errors.PasswordHashInvalidError:
            raise LoginError("That password is not right.")
        except errors.FloodWaitError as exc:
            raise LoginError(_flood_message(exc))
        except Exception as exc:
            raise LoginError(f"Sign-in failed: {type(exc).__name__}: {exc}")
        return await self._finish()

    async def _finish(self) -> tuple[dict[str, Any], Any]:
        """Pull the completed login's session internals off the connected
        client and persist them via SessionRegistry, instead of handing back
        an opaque StringSession string.

        `dc_id`, `server_address`, `port` and `auth_key.key` are standard
        attributes on any Telethon session object (MemorySession and
        StringSession both expose them the same way — see
        telethon/sessions/memory.py) — this is exactly the shape
        session_runtime.py's `_client_from_auth()` reconstructs a client
        from on the other end.
        """
        client = self._client
        session_id = self.session_id
        assert client is not None
        assert session_id is not None

        session = client.session
        dc_id = session.dc_id
        server_address = session.server_address
        port = session.port
        auth_key = session.auth_key.key  # raw bytes

        me = await client.get_me()

        self._client = None
        await client.disconnect()

        row = await self._registry.save_login(
            session_id,
            dc_id=dc_id,
            server_address=server_address,
            port=port,
            auth_key=auth_key,
            user_id=me.id if me is not None else None,
            username=getattr(me, "username", None) if me is not None else None,
            phone_number=getattr(me, "phone", None) if me is not None else self.phone,
        )

        await self.reset()
        return row, me
