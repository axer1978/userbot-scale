"""Saving settings from the panel."""

from __future__ import annotations

import pytest

import config_store
import device_profiles


@pytest.mark.asyncio
async def test_saving_settings_keeps_the_device_identity(panel_client, pg_pool, db):
    """The Settings form doesn't send `identity`; a save must not blank the
    device the account has been presenting to Telegram."""
    stored = await config_store.load(pg_pool, db.session_id)
    identity = device_profiles.derive(db.session_id)
    await config_store.save(pg_pool, db.session_id, {**stored, "identity": identity})

    form = {k: v for k, v in stored.items() if k != "identity"}
    form["behavior"] = {**form["behavior"], "auto_send": True}
    r = await panel_client.put(f"/api/sessions/{db.session_id}/config", json=form)
    assert r.status_code == 200, r.text

    saved = await config_store.load(pg_pool, db.session_id)
    assert saved["behavior"]["auto_send"] is True
    assert saved["identity"]["device_model"] == identity["device_model"]
