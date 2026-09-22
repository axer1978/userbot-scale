"""AES-GCM round-trip and failure-mode behaviour for secrets at rest.

No Postgres needed — crypto.py has no I/O beyond reading the key from the
environment (patched to a fixed test key by the autouse `crypto_key` fixture
in conftest.py).
"""

from __future__ import annotations

import base64
import json

import pytest

import crypto


def test_round_trip_auth_key_bytes():
    raw = bytes(range(256)) * 2  # a plausible 512-byte Telethon auth key
    aad = crypto.aad_for("acct01", "auth_key")
    blob = crypto.encrypt(raw, aad=aad)
    assert crypto.decrypt(blob, aad=aad) == raw


def test_round_trip_proxy_url_text():
    text = "socks5://user:pass@203.0.113.7:1080"
    aad = crypto.aad_for("acct01", "proxy_url")
    blob = crypto.encrypt_text(text, aad=aad)
    assert crypto.decrypt_text(blob, aad=aad) == text


def test_ciphertext_differs_every_call():
    aad = crypto.aad_for("acct01", "auth_key")
    a = crypto.encrypt(b"same plaintext", aad=aad)
    b = crypto.encrypt(b"same plaintext", aad=aad)
    assert a != b, "nonce must be fresh per call"


def test_blob_layout_is_version_keyid_nonce_body():
    aad = crypto.aad_for("acct01", "auth_key")
    plaintext = b"hello world"
    blob = crypto.encrypt(plaintext, aad=aad)
    assert blob[0] == crypto.BLOB_VERSION
    assert blob[1] == 1  # active key id under the test keyring
    nonce = blob[2 : 2 + crypto.NONCE_BYTES]
    assert len(nonce) == crypto.NONCE_BYTES
    body = blob[2 + crypto.NONCE_BYTES :]
    assert len(body) == len(plaintext) + 16  # ciphertext + 16-byte GCM tag


def test_plaintext_never_appears_in_the_blob():
    plaintext = b"THIS_MUST_NOT_LEAK_1234567890"
    aad = crypto.aad_for("acct01", "auth_key")
    blob = crypto.encrypt(plaintext, aad=aad)
    assert plaintext not in blob


def test_wrong_session_id_cannot_decrypt():
    blob = crypto.encrypt(b"secret", aad=crypto.aad_for("acct01", "auth_key"))
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt(blob, aad=crypto.aad_for("acct02", "auth_key"))


def test_wrong_field_cannot_decrypt():
    blob = crypto.encrypt(b"secret", aad=crypto.aad_for("acct01", "auth_key"))
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt(blob, aad=crypto.aad_for("acct01", "proxy_url"))


def test_tampered_tag_raises_decrypt_error():
    aad = crypto.aad_for("acct01", "auth_key")
    blob = bytearray(crypto.encrypt(b"secret", aad=aad))
    blob[-1] ^= 0xFF  # flip a bit in the GCM tag
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt(bytes(blob), aad=aad)


def test_truncated_blob_raises_decrypt_error():
    aad = crypto.aad_for("acct01", "auth_key")
    blob = crypto.encrypt(b"secret", aad=aad)
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt(blob[:-5], aad=aad)


def test_missing_key_refuses_to_start(monkeypatch):
    monkeypatch.delenv("USERBOT_MASTER_KEY", raising=False)
    monkeypatch.delenv("USERBOT_MASTER_KEY_FILE", raising=False)
    crypto.reset_cache()
    with pytest.raises(crypto.MissingKeyError):
        crypto.load_keyring()
    # No keygen path exists anywhere in the module.
    assert not hasattr(crypto, "generate_key")
    crypto.reset_cache()


def test_previous_key_still_decrypts_after_rotation(monkeypatch, tmp_path):
    key1 = base64.b64encode(b"1" * 32).decode()
    key2 = base64.b64encode(b"2" * 32).decode()
    keyfile = tmp_path / "keys.json"

    keyfile.write_text(json.dumps({"active": 1, "keys": {"1": key1}}))
    monkeypatch.setenv("USERBOT_MASTER_KEY_FILE", str(keyfile))
    monkeypatch.delenv("USERBOT_MASTER_KEY", raising=False)
    crypto.reset_cache()
    aad = crypto.aad_for("acct01", "auth_key")
    blob_under_key1 = crypto.encrypt(b"secret", aad=aad)

    keyfile.write_text(json.dumps({"active": 2, "keys": {"1": key1, "2": key2}}))
    crypto.reset_cache()
    assert crypto.decrypt(blob_under_key1, aad=aad) == b"secret"


def test_a_rotated_blob_uses_the_new_key_id(monkeypatch, tmp_path):
    key1 = base64.b64encode(b"1" * 32).decode()
    key2 = base64.b64encode(b"2" * 32).decode()
    keyfile = tmp_path / "keys.json"
    keyfile.write_text(json.dumps({"active": 2, "keys": {"1": key1, "2": key2}}))
    monkeypatch.setenv("USERBOT_MASTER_KEY_FILE", str(keyfile))
    monkeypatch.delenv("USERBOT_MASTER_KEY", raising=False)
    crypto.reset_cache()

    aad = crypto.aad_for("acct01", "auth_key")
    blob = crypto.encrypt(b"secret", aad=aad)
    assert blob[1] == 2
    assert crypto.decrypt(blob, aad=aad) == b"secret"
