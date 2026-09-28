"""The booking e-mail record, sent over plain SMTP.

A booking made in a Telegram chat also goes out by e-mail, with an .ics
attached so it lands in a calendar. Plain SMTP (stdlib smtplib) rather than
a provider SDK: every mail service speaks it, so the platform is not tied
to one vendor and needs no extra dependency.

Settings are platform-wide and come from the environment:

  SMTP_HOST      empty or unset = e-mail is switched off (settings_from_env
                 returns None and callers skip sending)
  SMTP_PORT      default 587 (465 when SMTP_SECURITY=ssl)
  SMTP_USER      login name; no login at all when empty
  SMTP_PASSWORD
  SMTP_FROM      the From address; defaults to SMTP_USER
  SMTP_SECURITY  "starttls" (default), "ssl" (implicit TLS) or "none"

smtplib is blocking, so send() runs it in a worker thread. The SMTP class is
injectable so tests can record the conversation without a server. The
password never appears in an error: it is redacted from anything smtplib
or the server says before it becomes a MailError.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Callable, Mapping, Optional

from ai_responder import _clip, _redact

log = logging.getLogger(__name__)

SECURITY_MODES = ("starttls", "ssl", "none")
DEFAULT_PORT = 587
DEFAULT_SSL_PORT = 465
ICS_FILENAME = "booking.ics"

# Deliberately loose: one @, something on both sides, a dot in the domain,
# and none of the characters that would let one field carry several
# addresses or a display name. Deliverability is the server's business.
_ADDRESS = re.compile(r"^[^@\s<>,;:\"()\[\]\\]+@[^@\s<>,;:\"()\[\]\\]+\.[^@\s<>,;:\"()\[\]\\]+$")

SmtpFactory = Callable[..., Any]


class MailError(Exception):
    """Raised for any failure that should surface in the admin panel."""


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    user: str
    password: str
    sender: str
    security: str

    def __repr__(self) -> str:
        # Settings end up in log lines and tracebacks; the password must not.
        return (
            f"SmtpSettings(host={self.host!r}, port={self.port}, user={self.user!r}, "
            f"password='***', sender={self.sender!r}, security={self.security!r})"
        )


def settings_from_env(env: Mapping[str, str] = os.environ) -> Optional[SmtpSettings]:
    """The SMTP settings, None when SMTP_HOST is empty (e-mail off), or
    MailError when they are set but unusable."""
    host = (env.get("SMTP_HOST") or "").strip()
    if not host:
        return None

    security = (env.get("SMTP_SECURITY") or "starttls").strip().lower()
    if security not in SECURITY_MODES:
        raise MailError(
            f"SMTP_SECURITY must be one of {', '.join(SECURITY_MODES)}, not {security!r}."
        )

    raw_port = (env.get("SMTP_PORT") or "").strip()
    if raw_port:
        try:
            port = int(raw_port)
        except ValueError:
            raise MailError(f"SMTP_PORT must be a number, not {raw_port!r}.") from None
        if not 1 <= port <= 65535:
            raise MailError(f"SMTP_PORT must be between 1 and 65535, not {port}.")
    else:
        # 587 is the submission port for STARTTLS; implicit TLS lives on 465,
        # and pointing SMTP_SSL at 587 just hangs until the timeout.
        port = DEFAULT_SSL_PORT if security == "ssl" else DEFAULT_PORT

    user = (env.get("SMTP_USER") or "").strip()
    # Passwords are taken as-is: leading or trailing spaces may be real.
    password = env.get("SMTP_PASSWORD") or ""
    sender = (env.get("SMTP_FROM") or "").strip() or user
    if not sender:
        raise MailError("SMTP_FROM (or SMTP_USER) must be set to send e-mail.")
    if not _ADDRESS.match(email.utils.parseaddr(sender)[1] or ""):
        raise MailError(f"SMTP_FROM does not look like an e-mail address: {sender!r}.")

    return SmtpSettings(
        host=host, port=port, user=user, password=password, sender=sender, security=security
    )


def _check_header(name: str, value: str) -> None:
    # A CR or LF would let a customer-supplied value start a new header
    # (Bcc: everyone@...) — reject rather than strip, so it gets noticed.
    if "\r" in value or "\n" in value:
        raise MailError(f"The {name} must not contain line breaks.")


def build_message(
    settings: SmtpSettings,
    *,
    to: str,
    subject: str,
    body: str,
    ics: Optional[str] = None,
) -> EmailMessage:
    """The message, with the .ics (when given) attached as text/calendar."""
    to = (to or "").strip()
    _check_header("recipient address", to)
    _check_header("subject", subject or "")
    if not _ADDRESS.match(to):
        raise MailError(f"The recipient does not look like an e-mail address: {to!r}.")

    message = EmailMessage()
    message["From"] = settings.sender
    message["To"] = to
    message["Subject"] = subject or ""
    message["Date"] = email.utils.formatdate(localtime=True)
    domain = email.utils.parseaddr(settings.sender)[1].rpartition("@")[2] or None
    message["Message-ID"] = email.utils.make_msgid(domain=domain)
    message.set_content(body or "")
    if ics is not None:
        # method=PUBLISH: this is a record of the booking, not a meeting
        # request, so calendars add it without offering accept/decline.
        message.add_attachment(
            ics,
            subtype="calendar",
            filename=ICS_FILENAME,
            params={"method": "PUBLISH"},
        )
    return message


async def send(
    settings: SmtpSettings,
    *,
    to: str,
    subject: str,
    body: str,
    ics: Optional[str] = None,
    smtp_factory: Optional[SmtpFactory] = None,
    timeout: float = 20.0,
) -> None:
    """Send one message, or raise MailError with a message safe to show."""
    # Built (and validated) here, so a bad address fails without a thread
    # or a connection.
    message = build_message(settings, to=to, subject=subject, body=body, ics=ics)
    await asyncio.to_thread(_send_blocking, settings, message, smtp_factory, timeout)


def _send_blocking(
    settings: SmtpSettings,
    message: EmailMessage,
    smtp_factory: Optional[SmtpFactory],
    timeout: float,
) -> None:
    try:
        if settings.security == "ssl":
            factory = smtp_factory or smtplib.SMTP_SSL
            smtp = factory(
                settings.host, settings.port, timeout=timeout,
                context=ssl.create_default_context(),
            )
        else:
            factory = smtp_factory or smtplib.SMTP
            smtp = factory(settings.host, settings.port, timeout=timeout)
    except (smtplib.SMTPException, OSError) as exc:
        raise _mail_error(settings, "Could not connect to the SMTP server", exc) from None

    try:
        if settings.security == "starttls":
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            # The server's capabilities (AUTH among them) change once the
            # connection is encrypted, so they have to be asked for again.
            smtp.ehlo()
        if settings.user:
            if settings.security == "none":
                log.warning("Logging in to %s without TLS; the password travels in clear.", settings.host)
            smtp.login(settings.user, settings.password)
        smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        _close_quietly(smtp)
        raise _mail_error(settings, "Sending the e-mail failed", exc) from None
    try:
        smtp.quit()
    except (smtplib.SMTPException, OSError):
        # The message was accepted; a rude goodbye does not un-send it.
        _close_quietly(smtp)


def _close_quietly(smtp: Any) -> None:
    try:
        smtp.close()
    except Exception:
        pass


def _mail_error(settings: SmtpSettings, what: str, exc: BaseException) -> MailError:
    detail = _clip(_redact(str(exc), settings.password))
    text = f"{what} ({settings.host}:{settings.port}): {type(exc).__name__}"
    if detail:
        text += f": {detail}"
    return MailError(_redact(text, settings.password))
