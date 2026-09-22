"""AES-GCM encrypt/decrypt for secrets stored in Postgres (auth_key,
proxy_url, api_hash, per-session DeepSeek key overrides).

The key is never generated or hardcoded here — it is supplied by ops via an
environment variable or a secrets file, and the process refuses to boot
without one. Every ciphertext is bound (via AES-GCM's associated data) to
the session_id and column it belongs to, so a blob copied into the wrong
row or the wrong column fails to decrypt instead of silently succeeding.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

BLOB_VERSION = 1
NONCE_BYTES = 12
KEY_BYTES = 32

_ENV_KEY_FILE = "USERBOT_MASTER_KEY_FILE"
_ENV_KEY = "USERBOT_MASTER_KEY"


class CryptoError(RuntimeError):
    pass


class MissingKeyError(CryptoError):
    pass


class DecryptError(CryptoError):
    pass


@dataclass(frozen=True)
class Keyring:
    active_id: int
    keys: dict[int, bytes]  # key_id -> 32 raw bytes

    def active_key(self) -> bytes:
        return self.keys[self.active_id]


_cache: Keyring | None = None


def _decode_key(b64: str) -> bytes:
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception as exc:  # noqa: BLE001 - surfaced as a clear boot-time error
        raise MissingKeyError(f"master key is not valid base64: {exc}") from exc
    if len(raw) != KEY_BYTES:
        raise MissingKeyError(f"master key must decode to {KEY_BYTES} bytes, got {len(raw)}")
    return raw


def _keyring_from_file(path: str) -> Keyring:
    try:
        content = open(path, "r", encoding="utf-8").read().strip()
    except OSError as exc:
        raise MissingKeyError(f"cannot read {_ENV_KEY_FILE}={path!r}: {exc}") from exc
    if content.startswith("{"):
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise MissingKeyError(f"{path} is not valid JSON: {exc}") from exc
        try:
            active = int(data["active"])
            keys = {int(k): _decode_key(v) for k, v in data["keys"].items()}
        except (KeyError, TypeError, ValueError) as exc:
            raise MissingKeyError(
                f'{path} must be {{"active": <id>, "keys": {{"<id>": "<b64>"}}}}: {exc}'
            ) from exc
        if active not in keys:
            raise MissingKeyError(f"{path}: active key id {active} not present in keys")
        return Keyring(active_id=active, keys=keys)
    return Keyring(active_id=1, keys={1: _decode_key(content)})


def load_keyring(*, refresh: bool = False) -> Keyring:
    """Load once and cache (so 25 session runtimes in a worker don't each
    re-read the key file). `refresh=True` (or `reset_cache`) forces a reload."""
    global _cache
    if _cache is not None and not refresh:
        return _cache

    key_file = os.environ.get(_ENV_KEY_FILE)
    if key_file:
        _cache = _keyring_from_file(key_file)
        return _cache

    key_env = os.environ.get(_ENV_KEY)
    if key_env:
        _cache = Keyring(active_id=1, keys={1: _decode_key(key_env)})
        return _cache

    raise MissingKeyError(
        f"set {_ENV_KEY_FILE} (path to a key file) or {_ENV_KEY} (base64 key) "
        "before starting this process. There is no key-generation path in this "
        "codebase; ops must supply the key material."
    )


def reset_cache() -> None:
    """Tests only."""
    global _cache
    _cache = None


def aad_for(session_id: str, field: str) -> bytes:
    return f"{session_id}:{field}".encode("utf-8")


def encrypt(plaintext: bytes, *, aad: bytes) -> bytes:
    keyring = load_keyring()
    key_id = keyring.active_id
    nonce = os.urandom(NONCE_BYTES)
    aesgcm = AESGCM(keyring.keys[key_id])
    body = aesgcm.encrypt(nonce, plaintext, aad)
    if not (1 <= key_id <= 255):
        raise CryptoError(f"key id {key_id} does not fit in one byte")
    return bytes([BLOB_VERSION, key_id]) + nonce + body


def decrypt(blob: bytes, *, aad: bytes) -> bytes:
    if len(blob) < 2 + NONCE_BYTES + 16:
        raise DecryptError("ciphertext blob is too short to be valid")
    version = blob[0]
    key_id = blob[1]
    if version != BLOB_VERSION:
        raise DecryptError(f"unknown blob version {version}")
    keyring = load_keyring()
    key = keyring.keys.get(key_id)
    if key is None:
        raise DecryptError(f"unknown key id {key_id} (key not present in the keyring)")
    nonce = blob[2 : 2 + NONCE_BYTES]
    body = blob[2 + NONCE_BYTES :]
    try:
        return AESGCM(key).decrypt(nonce, body, aad)
    except InvalidTag as exc:
        raise DecryptError("authentication failed (wrong key, AAD, or tampered ciphertext)") from exc


def encrypt_text(text: str, *, aad: bytes) -> bytes:
    return encrypt(text.encode("utf-8"), aad=aad)


def decrypt_text(blob: bytes, *, aad: bytes) -> str:
    return decrypt(blob, aad=aad).decode("utf-8")
