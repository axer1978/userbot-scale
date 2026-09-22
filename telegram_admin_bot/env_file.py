"""Tolerant reader for .env files.

python-dotenv stops a value at the first newline. A Telethon session string is
~350 characters, so when it is copied out of a terminal that wrapped it, the
line breaks come along and everything after the first one is silently dropped —
which surfaces much later as an opaque "Not a valid string". This reader joins
those continuation lines back onto the value they belong to.
"""

from __future__ import annotations

import re
from pathlib import Path

_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")

# Ending a wrapped value needs a stricter test than starting one. Base64 pads
# with "=", so a continuation line such as "aGVsbG8=" matches the lenient
# pattern and would otherwise be read as a new key. Env names are conventionally
# upper snake case, and base64 chunks almost always carry lower-case letters
# before their padding, so this tells the two apart.
_STRICT_KEY = re.compile(r"^([A-Z][A-Z0-9_]{0,63})\s*=(.*)$")


def parse_env_file(path: str | Path) -> dict[str, str]:
    """Parse .env, treating a line that isn't KEY=... as a continuation."""
    path = Path(path)
    values: dict[str, str] = {}
    if not path.exists():
        return values

    current: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            current = None
            continue
        match = (_STRICT_KEY if current is not None else _KEY).match(line)
        if match:
            current = match.group(1)
            values[current] = match.group(2).strip()
        elif current is not None:
            # A wrapped value: rejoin it with no separator.
            values[current] += line

    for key, value in values.items():
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value.strip()
    return values


def recover_wrapped(name: str, env_value: str, file_values: dict[str, str]) -> str:
    """Prefer the .env value when the environment holds a truncated prefix of it.

    Only applies when the file value genuinely extends what we already have, so
    a real environment variable still wins over a stale .env entry.
    """
    file_value = file_values.get(name, "")
    if file_value and len(file_value) > len(env_value) and file_value.startswith(env_value):
        return file_value
    return env_value


def is_placeholder(value: str) -> bool:
    """True when a value is still the untouched example from .env.example."""
    return value.startswith("your_") or value == "1234567"


def write_keys(
    path: str | Path, values: dict[str, str], template: str | Path | None = None
) -> None:
    """Set keys in a .env file, replacing existing lines and keeping the rest.

    A file that does not exist yet starts from `template` (.env.example), so
    the explanatory comments come along. If an existing value had wrapped
    across several lines, the continuation lines go too — otherwise they would
    be glued onto the new value by parse_env_file.
    """
    path = Path(path)
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    elif template is not None and Path(template).exists():
        lines = Path(template).read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    for key, value in values.items():
        value = "".join(str(value).split())
        for i, line in enumerate(lines):
            if line.strip().startswith(f"{key}="):
                end = i + 1
                while end < len(lines):
                    nxt = lines[end].strip()
                    if not nxt or nxt.startswith("#") or _STRICT_KEY.match(nxt):
                        break
                    end += 1
                lines[i:end] = [f"{key}={value}"]
                break
        else:
            lines.append(f"{key}={value}")

    is_new = not path.exists()
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if is_new:
        # Credentials: owner-only where the OS supports it (no-op on Windows).
        try:
            path.chmod(0o600)
        except OSError:
            pass
