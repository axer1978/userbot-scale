"""mailer.py: settings, the message with its .ics, and the SMTP conversation
(against a fake SMTP class that records every call)."""

from __future__ import annotations

import smtplib

import pytest

import mailer

PASSWORD = "hunter2-very-secret"

ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\nMETHOD:PUBLISH\r\n"
    "BEGIN:VEVENT\r\nUID:1@test\r\nDTSTART:20260928T100000Z\r\nSUMMARY:Booking\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


def _settings(**overrides) -> mailer.SmtpSettings:
    values = dict(
        host="smtp.example.com", port=587, user="bot@example.com", password=PASSWORD,
        sender="bot@example.com", security="starttls",
    )
    values.update(overrides)
    return mailer.SmtpSettings(**values)


class FakeSMTP:
    """Records the SMTP conversation; `fail_on` names a method that raises."""

    instances: list["FakeSMTP"] = []
    fail_on: str = ""
    error: Exception = smtplib.SMTPException("boom")

    def __init__(self, host, port, **kwargs):
        self.host, self.port, self.kwargs = host, port, kwargs
        self.calls: list[str] = []
        self.sent = []
        self.login_args = None
        FakeSMTP.instances.append(self)
        if FakeSMTP.fail_on == "connect":
            raise FakeSMTP.error

    def _call(self, name):
        self.calls.append(name)
        if FakeSMTP.fail_on == name:
            raise FakeSMTP.error

    def ehlo(self):
        self._call("ehlo")

    def starttls(self, context=None):
        self.starttls_context = context
        self._call("starttls")

    def login(self, user, password):
        self.login_args = (user, password)
        self._call("login")

    def send_message(self, message):
        self.sent.append(message)
        self._call("send_message")

    def quit(self):
        self._call("quit")

    def close(self):
        self.calls.append("close")


@pytest.fixture(autouse=True)
def fresh_fake():
    FakeSMTP.instances = []
    FakeSMTP.fail_on = ""
    FakeSMTP.error = smtplib.SMTPException("boom")
    yield


async def _send(settings, **kwargs):
    args = dict(to="client@example.org", subject="Your booking", body="See you soon.")
    args.update(kwargs)
    await mailer.send(settings, smtp_factory=FakeSMTP, **args)
    return FakeSMTP.instances[-1]


# ------------------------------------------------------------ settings


def test_settings_disabled_without_host():
    assert mailer.settings_from_env({}) is None
    assert mailer.settings_from_env({"SMTP_HOST": "  ", "SMTP_USER": "a@b.co"}) is None


def test_settings_defaults():
    settings = mailer.settings_from_env(
        {"SMTP_HOST": "smtp.example.com", "SMTP_USER": "bot@example.com", "SMTP_PASSWORD": PASSWORD}
    )
    assert settings == mailer.SmtpSettings(
        host="smtp.example.com", port=587, user="bot@example.com", password=PASSWORD,
        sender="bot@example.com", security="starttls",
    )


def test_settings_explicit_values():
    settings = mailer.settings_from_env({
        "SMTP_HOST": "mail.example.com", "SMTP_PORT": "2525", "SMTP_USER": "login",
        "SMTP_PASSWORD": PASSWORD, "SMTP_FROM": "Bookings <bookings@example.com>",
        "SMTP_SECURITY": "NONE",
    })
    assert settings.port == 2525
    assert settings.sender == "Bookings <bookings@example.com>"
    assert settings.security == "none"


def test_settings_ssl_defaults_to_port_465():
    settings = mailer.settings_from_env(
        {"SMTP_HOST": "h.example.com", "SMTP_FROM": "a@example.com", "SMTP_SECURITY": "ssl"}
    )
    assert settings.port == 465
    assert settings.user == ""


@pytest.mark.parametrize(
    "env, needle",
    [
        ({"SMTP_SECURITY": "tls"}, "SMTP_SECURITY"),
        ({"SMTP_PORT": "abc"}, "SMTP_PORT"),
        ({"SMTP_PORT": "0"}, "SMTP_PORT"),
        ({"SMTP_PORT": "70000"}, "SMTP_PORT"),
        ({"SMTP_USER": "", "SMTP_FROM": ""}, "SMTP_FROM"),
        ({"SMTP_FROM": "not an address"}, "SMTP_FROM"),
    ],
)
def test_settings_bad_values(env, needle):
    base = {"SMTP_HOST": "h.example.com", "SMTP_USER": "a@example.com"}
    with pytest.raises(mailer.MailError, match=needle):
        mailer.settings_from_env({**base, **env})


def test_settings_repr_hides_password():
    assert PASSWORD not in repr(_settings())


# ------------------------------------------------------------ message


def test_message_headers_and_body():
    message = mailer.build_message(_settings(), to="client@example.org", subject="Booked", body="Hi")
    assert message["From"] == "bot@example.com"
    assert message["To"] == "client@example.org"
    assert message["Subject"] == "Booked"
    assert message["Message-ID"]
    assert message.get_content().strip() == "Hi"
    assert not message.is_multipart()


def test_message_has_ics_attachment():
    message = mailer.build_message(
        _settings(), to="client@example.org", subject="Booked", body="Hi", ics=ICS
    )
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    ics = attachments[0]
    assert ics.get_content_type() == "text/calendar"
    assert ics.get_param("method") == "PUBLISH"
    assert ics.get_filename() == "booking.ics"
    assert "BEGIN:VEVENT" in ics.get_content()
    assert message.get_body(("plain",)).get_content().strip() == "Hi"


@pytest.mark.parametrize("to", ["", "nobody", "a@b", "a b@example.com", "a@example.com, b@example.com",
                                "Name <a@example.com>"])
def test_bad_recipient_rejected(to):
    with pytest.raises(mailer.MailError, match="recipient"):
        mailer.build_message(_settings(), to=to, subject="s", body="b")


@pytest.mark.parametrize(
    "field, value",
    [("subject", "Booked\r\nBcc: everyone@example.com"), ("subject", "Booked\nX: y"),
     ("to", "client@example.org\r\nBcc: everyone@example.com")],
)
@pytest.mark.asyncio
async def test_header_injection_rejected(field, value):
    kwargs = {"to": "client@example.org", "subject": "ok", "body": "b", field: value}
    with pytest.raises(mailer.MailError, match="line breaks"):
        await mailer.send(_settings(), smtp_factory=FakeSMTP, **kwargs)
    assert FakeSMTP.instances == []  # never connected


# ------------------------------------------------------------ SMTP conversation


@pytest.mark.asyncio
async def test_starttls_path():
    smtp = await _send(_settings(), ics=ICS)
    assert (smtp.host, smtp.port) == ("smtp.example.com", 587)
    assert smtp.kwargs == {"timeout": 20.0}
    assert smtp.calls == ["ehlo", "starttls", "ehlo", "login", "send_message", "quit"]
    assert smtp.starttls_context is not None
    assert smtp.login_args == ("bot@example.com", PASSWORD)
    assert smtp.sent[0]["To"] == "client@example.org"


@pytest.mark.asyncio
async def test_ssl_path_passes_a_tls_context():
    smtp = await _send(_settings(security="ssl", port=465))
    assert smtp.port == 465
    assert "context" in smtp.kwargs and smtp.kwargs["timeout"] == 20.0
    assert smtp.calls == ["login", "send_message", "quit"]


@pytest.mark.asyncio
async def test_none_path_has_no_tls():
    smtp = await _send(_settings(security="none", port=25))
    assert smtp.calls == ["login", "send_message", "quit"]
    assert smtp.kwargs == {"timeout": 20.0}


@pytest.mark.asyncio
async def test_login_skipped_without_user():
    smtp = await _send(_settings(user="", password=""))
    assert "login" not in smtp.calls
    assert smtp.calls == ["ehlo", "starttls", "ehlo", "send_message", "quit"]


@pytest.mark.asyncio
async def test_timeout_is_passed_through():
    await mailer.send(
        _settings(), to="c@example.org", subject="s", body="b", smtp_factory=FakeSMTP, timeout=5
    )
    assert FakeSMTP.instances[-1].kwargs["timeout"] == 5


@pytest.mark.asyncio
async def test_failed_quit_after_send_is_not_an_error():
    FakeSMTP.fail_on = "quit"
    smtp = await _send(_settings())
    assert smtp.calls[-2:] == ["quit", "close"]
    assert len(smtp.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fail_on, error",
    [
        ("login", smtplib.SMTPAuthenticationError(535, f"bad credentials {PASSWORD}".encode())),
        ("send_message", smtplib.SMTPRecipientsRefused({"client@example.org": (550, b"no such user")})),
        ("starttls", OSError(f"tls broke {PASSWORD}")),
        ("connect", ConnectionRefusedError(f"refused {PASSWORD}")),
    ],
)
async def test_failures_become_mail_error_without_password(fail_on, error):
    FakeSMTP.fail_on = fail_on
    FakeSMTP.error = error
    with pytest.raises(mailer.MailError) as info:
        await _send(_settings())
    text = str(info.value)
    assert PASSWORD not in text
    assert type(error).__name__ in text
    assert "smtp.example.com:587" in text
    if fail_on != "connect":
        assert FakeSMTP.instances[-1].calls[-1] == "close"
