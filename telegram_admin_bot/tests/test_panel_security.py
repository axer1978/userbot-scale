"""The panel's own protection when it faces the internet: security headers
on every response, and refusing to start publicly without 2FA and a long
admin password."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]


async def test_every_response_carries_the_security_headers(panel_client):
    for path in ("/", "/api/login-options", "/api/sessions"):
        r = await panel_client.get(path)
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert (await panel_client.get("/api/sessions")).headers["cache-control"] == "no-store"


async def test_the_index_page_has_no_inline_script(panel_client):
    html = (await panel_client.get("/")).text
    import re

    for tag in re.findall(r"<script[^>]*>", html):
        assert "src=" in tag, tag


async def test_a_public_panel_needs_2fa_and_a_long_password(panel_client, monkeypatch):
    import panel

    monkeypatch.setattr(panel, "PANEL_DOMAIN", "")
    assert panel.check_public_setup() == []
    monkeypatch.setattr(panel, "PANEL_DOMAIN", "panel.example.com")
    monkeypatch.setattr(panel, "ADMIN_TOTP_SECRET", "")
    monkeypatch.setattr(panel, "ADMIN_PASSWORD", "short")
    problems = panel.check_public_setup()
    assert len(problems) == 2 and "ADMIN_TOTP_SECRET" in problems[0]
    monkeypatch.setattr(panel, "ADMIN_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    monkeypatch.setattr(panel, "ADMIN_PASSWORD", "a-long-enough-password")
    assert panel.check_public_setup() == []
    monkeypatch.setattr(panel, "ADMIN_PASSWORD", "short")
    with pytest.raises(RuntimeError, match="Refusing to serve a public panel"):
        await panel.on_startup()
