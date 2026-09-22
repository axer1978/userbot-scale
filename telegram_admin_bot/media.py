"""Photos and videos the assistant may send when a contact asks for them.

The library is a folder (`media/` under the data directory) plus a small
JSON index next to the files that gives each one a stable id and a
description. The description is what the model sees, so it is what lets it
pick the right file when someone asks for "the one from the beach".

The model attaches a file by writing a tag such as `[send 3]` in its reply.
split_attachments() takes the tags back out of the text and returns the ids;
the tag itself never reaches Telegram, exactly like the burst separator.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

PHOTO = "photo"
VIDEO = "video"

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".3gp"}

INDEX_NAME = "library.json"

# `[send 3]`, `[send: 3]`, `[SEND #3]`, `[send photo 3]`, `[[send 3]]` — the
# model is asked for the first form and produces all of them.
# `[sent photo #3: the beach]` is the history placeholder for a file already
# sent — the model sometimes echoes it instead of writing the tag. Meant as a
# send all the same, and it must never go out to the person as text.
_TAG = re.compile(
    r"\[+\s*(?:send\b[^\]\d]*|sent\s+(?:photo|video)\s*#?\s*)(\d+)[^\]]*\]+",
    re.IGNORECASE,
)
# The two forms the tag can take when the model glues it to a sentence.
_TAG_GAP = re.compile(r"[ \t]{2,}")

MAX_ATTACHMENTS_PER_REPLY = 3


def kind_for(filename: str) -> Optional[str]:
    ext = Path(filename).suffix.lower()
    if ext in PHOTO_EXTENSIONS:
        return PHOTO
    if ext in VIDEO_EXTENSIONS:
        return VIDEO
    return None


def safe_filename(name: str) -> str:
    """A bare file name, with anything that could escape the folder removed."""
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    name = re.sub(r"[^\w.\- ()\[\]]+", "_", name, flags=re.UNICODE)
    name = name.strip(". ")
    return name or "file"


class MediaLibrary:
    """The folder of files and their descriptions, kept in sync with disk.

    Files dropped straight into the folder are picked up on the next
    refresh() with a blank description; files removed from the folder drop
    out of the index. Ids are never reused, so a chat's history can keep
    pointing at "photo #3" after it is gone.
    """

    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)
        self._items: dict[int, dict[str, Any]] = {}
        self._next_id = 1
        self._load()
        self.refresh()

    # ------------------------------------------------------------- disk

    def _load(self) -> None:
        try:
            raw = json.loads((self.dir / INDEX_NAME).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        items = raw.get("items", []) if isinstance(raw, dict) else []
        for item in items:
            try:
                item_id = int(item["id"])
                file = safe_filename(str(item["file"]))
            except (KeyError, TypeError, ValueError):
                continue
            kind = kind_for(file)
            if kind is None:
                continue
            self._items[item_id] = {
                "id": item_id,
                "file": file,
                "kind": kind,
                "description": str(item.get("description") or "").strip(),
            }
        next_id = raw.get("next_id") if isinstance(raw, dict) else None
        if self._items:
            self._next_id = max(self._items) + 1
        if isinstance(next_id, int) and next_id > self._next_id:
            self._next_id = next_id

    def save(self) -> None:
        payload = {"next_id": self._next_id, "items": self.all()}
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), prefix=".library-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, self.dir / INDEX_NAME)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def refresh(self) -> bool:
        """Reconcile the index with the folder. Returns True if anything changed."""
        self.dir.mkdir(parents=True, exist_ok=True)
        on_disk = {
            entry.name
            for entry in self.dir.iterdir()
            if entry.is_file() and kind_for(entry.name) and not entry.name.startswith(".")
        }
        changed = False
        for item_id in [i for i, item in self._items.items() if item["file"] not in on_disk]:
            del self._items[item_id]
            changed = True
        known = {item["file"] for item in self._items.values()}
        for name in sorted(on_disk - known):
            self._items[self._next_id] = {
                "id": self._next_id,
                "file": name,
                "kind": kind_for(name),
                "description": "",
            }
            self._next_id += 1
            changed = True
        if changed:
            self.save()
        return changed

    # ---------------------------------------------------------- queries

    def all(self) -> list[dict[str, Any]]:
        return [dict(self._items[k]) for k in sorted(self._items)]

    def get(self, item_id: int) -> Optional[dict[str, Any]]:
        item = self._items.get(item_id)
        return dict(item) if item else None

    def path(self, item_id: int) -> Optional[Path]:
        item = self._items.get(item_id)
        if item is None:
            return None
        path = self.dir / item["file"]
        return path if path.is_file() else None

    def __len__(self) -> int:
        return len(self._items)

    # ---------------------------------------------------------- changes

    def unique_name(self, filename: str) -> str:
        """The name a new file will get: the cleaned one, or a numbered copy."""
        name = safe_filename(filename)
        stem, ext = os.path.splitext(name)
        candidate = name
        counter = 2
        while (self.dir / candidate).exists():
            candidate = f"{stem} ({counter}){ext}"
            counter += 1
        return candidate

    def add_file(self, filename: str, description: str = "") -> dict[str, Any]:
        """Register a file that has just been written into the folder."""
        name = safe_filename(filename)
        kind = kind_for(name)
        if kind is None:
            raise ValueError("Not a supported photo or video file type.")
        for item in self._items.values():
            if item["file"] == name:
                if description.strip():
                    item["description"] = description.strip()
                    self.save()
                return dict(item)
        item = {
            "id": self._next_id,
            "file": name,
            "kind": kind,
            "description": (description or "").strip(),
        }
        self._items[item["id"]] = item
        self._next_id += 1
        self.save()
        return dict(item)

    def describe(self, item_id: int, description: str) -> Optional[dict[str, Any]]:
        item = self._items.get(item_id)
        if item is None:
            return None
        item["description"] = (description or "").strip()
        self.save()
        return dict(item)

    def remove(self, item_id: int) -> bool:
        item = self._items.pop(item_id, None)
        if item is None:
            return False
        path = self.dir / item["file"]
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        self.save()
        return True


# ------------------------------------------------------------- the prompt


def label(item: dict[str, Any]) -> str:
    """'photo #3: the beach shot' — how a file is named in the prompt and rows."""
    text = item.get("description") or ""
    if not text.strip():
        text = Path(item.get("file") or "").stem.replace("_", " ").replace("-", " ")
    return f"{item.get('kind', PHOTO)} #{item.get('id')}: {text.strip()}"


def sent_placeholder(item: dict[str, Any]) -> str:
    """The row text for a sent file. Goes into the AI history too, so the
    model sees what it has already sent and does not send it twice."""
    return f"[sent {label(item)}]"


ASK_FIRST_RULE = (
    "VIDEOS ARE SENT ONLY AFTER ASKING: never attach a video in the same reply "
    "in which it first comes up. When someone asks for a video, or a video "
    "would fit, first ask whether they want you to send it — and attach it only "
    "in a later reply, once they have clearly said yes."
)


def prompt_section(items: list[dict[str, Any]], ask_before_video: bool = True) -> str:
    """What the model is told about the files it may attach, or "" if none."""
    if not items:
        return ""
    lines = [
        "PHOTOS AND VIDEOS YOU CAN SEND: you have these files on your phone. "
        "Send one only when they ask for a photo or video, or it is clearly "
        "what they want — never unprompted. To attach a file, write its tag on "
        "its own line at the end of your reply, like [send 3]. At most "
        f"{MAX_ATTACHMENTS_PER_REPLY} per reply. The history shows which ones "
        "you have already sent as [sent photo #N: ...] — that is a record, "
        "never write it yourself; to send a file again use its [send N] tag. "
        "Do not send the same file again unless they ask for it once more. "
        "If nothing you have "
        "matches what they want, say so naturally instead of sending something "
        "else. Never mention tags, files, or a library — to them it is just "
        "you sending a photo."
    ]
    for item in items:
        lines.append(f"- [send {item['id']}] = {label(item)}")
    if ask_before_video and any(item.get("kind") == VIDEO for item in items):
        lines.append(ASK_FIRST_RULE)
    return "\n".join(lines)


def split_attachments(text: str) -> tuple[str, list[int]]:
    """Take the [send N] tags out of a reply.

    Returns the text with the tags removed and the ids in the order they
    appeared, without repeats, capped at MAX_ATTACHMENTS_PER_REPLY. Ids are
    not checked against the library here — the caller knows which exist.
    """
    ids: list[int] = []
    for match in _TAG.finditer(text or ""):
        item_id = int(match.group(1))
        if item_id not in ids:
            ids.append(item_id)
    cleaned = _TAG.sub("", text or "")
    # A tag torn out of the middle of a line leaves a double space behind.
    cleaned = "\n".join(_TAG_GAP.sub(" ", line).strip() for line in cleaned.splitlines())
    return cleaned.strip(), ids[:MAX_ATTACHMENTS_PER_REPLY]
