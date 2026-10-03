"""Finetune a business's prompt layers from screenshots of its real chats.

The admin writes one finetune template per industry (finetune_api.py keeps
it in platform_settings). A run fills the template's placeholders, sends it
with the screenshots to the vision model (vision.py's endpoint) and reads
back three blocks:

    === BUSINESS LAYER ===          {"overrides": {...}, "addendum": "..."}
    === INDUSTRY STANDARD (...) === {"sections": {...}}
    === NOTES FOR OPERATOR ===      free text

Without a vision key the chats can be transcribed instead: the same
template goes to the text model (DeepSeek) with the transcripts and a note
that says how to read them in place of screenshots.

Both JSON blocks are checked exactly as a manual save would check them
(prompt_layers.validate_client / validate_industry), plus one rule of this
module: an industry standard written by a run must have a boundaries
section, since it replaces the whole template when applied.

Pure functions and one model call; storage and applying live in
finetune_api.py.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional, Sequence

import httpx

import ai_responder
import prompt_layers
import vision
from ai_responder import UsageSink

# What a template may contain; each is replaced as plain text (the template
# is full of JSON braces, so str.format is out).
BUSINESS_NAME = "{BUSINESS_NAME}"
BUSINESSES_SO_FAR = "{N}"
CURRENT_STANDARD = "{CURRENT_INDUSTRY_SECTIONS_JSON}"
PLACEHOLDERS = (BUSINESS_NAME, BUSINESSES_SO_FAR, CURRENT_STANDARD)

# Starting points the admin can load into the template editor: one file per
# industry, named after it ("escort.txt" for an industry called "Escort"),
# and generic.txt for any other, with {INDUSTRY} filled in on loading.
DEFAULTS_DIR = Path(__file__).resolve().parent / "finetune_templates"
GENERIC_DEFAULT = "generic"
INDUSTRY_NAME = "{INDUSTRY}"

MAX_TEMPLATE_CHARS = 50_000
MAX_IMAGES = 40
MAX_TOTAL_IMAGE_BYTES = 60 * 1024 * 1024
# Two full prompt layers in JSON: far longer than any chat reply.
DEFAULT_MAX_TOKENS = 16_000
# Reading 40 screenshots and writing that much takes minutes, not seconds.
REQUEST_TIMEOUT_SECONDS = 600.0

_HEADER = re.compile(r"^[ \t]*=+[ \t]*([A-Za-z][^=\n]*?)[ \t]*=+[ \t]*$", re.MULTILINE)
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


class FinetuneError(ValueError):
    pass


# ------------------------------------------------------------------ input


def check_template(template: str) -> str:
    template = (template or "").strip()
    if not template:
        raise FinetuneError("The finetune template is empty.")
    if len(template) > MAX_TEMPLATE_CHARS:
        raise FinetuneError(f"The template is {len(template)} characters; the limit is {MAX_TEMPLATE_CHARS}.")
    return template


def _default_key(industry_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", industry_name.strip().lower()).strip("_")


def default_template(industry_name: str) -> str:
    """The shipped starting template for this industry: its own file if
    there is one, else the generic one. Empty if neither exists."""
    key = _default_key(industry_name)
    path = DEFAULTS_DIR / f"{key}.txt"
    if not key or key == GENERIC_DEFAULT or not path.is_file():
        path = DEFAULTS_DIR / f"{GENERIC_DEFAULT}.txt"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return text.replace(INDUSTRY_NAME, industry_name.strip() or "this").strip()


def fill_template(template: str, *, business_name: str, businesses_so_far: int,
                  industry_sections: dict[str, str]) -> str:
    """The template with its placeholders filled. An industry with no
    sections yet is "none", which the template tells the model to treat as
    the first business."""
    current = (json.dumps({"sections": industry_sections}, ensure_ascii=False, indent=2)
               if industry_sections else "none")
    return (check_template(template)
            .replace(BUSINESS_NAME, business_name.strip() or "this business")
            .replace(BUSINESSES_SO_FAR, str(businesses_so_far))
            .replace(CURRENT_STANDARD, current))


def screenshot_order(name: str) -> list[Any]:
    """Natural order, so 2 comes before 10 and 03a, 03b, 03c stay together."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def check_images(images: Sequence[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    """Sorted by file name, each a readable image of a size the provider
    takes. Raises FinetuneError naming the file that is wrong."""
    if not images:
        raise FinetuneError("Add at least one screenshot.")
    if len(images) > MAX_IMAGES:
        raise FinetuneError(f"{len(images)} screenshots; at most {MAX_IMAGES} per run.")
    total = sum(len(data) for _, data in images)
    if total > MAX_TOTAL_IMAGE_BYTES:
        raise FinetuneError(f"The screenshots add up to {total // (1024 * 1024)} MB; "
                            f"at most {MAX_TOTAL_IMAGE_BYTES // (1024 * 1024)} MB per run.")
    for name, data in images:
        if len(data) > vision.MAX_IMAGE_BYTES:
            raise FinetuneError(f"{name} is larger than {vision.MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
        if vision.sniff_mime(data) is None:
            raise FinetuneError(f"{name} is not a JPEG, PNG, WebP or GIF image.")
    return sorted(images, key=lambda item: screenshot_order(item[0]))


def build_messages(prompt: str, images: Sequence[tuple[str, bytes]]) -> list[dict[str, Any]]:
    """One user message: the filled template, then each screenshot after a
    line naming its file, since the names carry the conversation order and
    the operator's good/bad marks."""
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for name, data in images:
        content.append({"type": "text", "text": f"Screenshot {name}:"})
        content.append(vision._image_part(data, name))
    return [{"role": "user", "content": content}]


# ------------------------------------------------------------- transcripts

MAX_CHATS = 100
# The template plus this must fit the text model's context with room for a
# long answer.
MAX_TRANSCRIPT_CHARS = 100_000
DEFAULT_TEXT_MODEL = "deepseek-chat"
# DeepSeek's chat model writes at most 8K tokens per answer.
DEFAULT_TEXT_MAX_TOKENS = 8_000

# Comes between the template and the transcripts, so a template written for
# screenshots is read correctly without being edited.
TRANSCRIPT_NOTE = (
    "THE CHATS BELOW ARE TEXT, NOT SCREENSHOTS. The operator transcribed them by hand. Wherever the "
    "instructions above talk about screenshots, read them as these transcripts:\n"
    "- Each conversation starts with a line \"### <name>\". The name works like a screenshot's file name: "
    "the same number is one conversation, in order, and a name ending in \"-good\" or \"-bad\" carries "
    "the operator's mark.\n"
    "- Lines starting \"Client:\" are the client, lines starting \"Business:\" are the business. Bubble "
    "colours and sides do not apply.\n"
    "- Timestamps, voice notes and photos appear only where the operator wrote them, e.g. [10:42] or "
    "[voice note].\n"
    "- If a line does not say who wrote it, or a conversation is unclear, list it in the operator notes. "
    "Do not guess."
)


def check_chats(chats: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """Non-empty conversations in natural order, within the size limits."""
    chats = [(name.strip(), text.strip()) for name, text in chats if text.strip()]
    if not chats:
        raise FinetuneError("Add at least one conversation.")
    if len(chats) > MAX_CHATS:
        raise FinetuneError(f"{len(chats)} conversations; at most {MAX_CHATS} per run.")
    names = [name for name, _ in chats]
    if any(not name for name in names):
        raise FinetuneError("Every conversation needs a name, e.g. 03 or 03-good.")
    if len(set(names)) != len(names):
        raise FinetuneError("Two conversations have the same name.")
    total = sum(len(text) for _, text in chats)
    if total > MAX_TRANSCRIPT_CHARS:
        raise FinetuneError(f"The transcripts are {total:,} characters; at most {MAX_TRANSCRIPT_CHARS:,} per run.")
    return sorted(chats, key=lambda item: screenshot_order(item[0]))


def build_text_messages(prompt: str, chats: Sequence[tuple[str, str]]) -> list[dict[str, Any]]:
    transcripts = "\n\n".join(f"### {name}\n{text}" for name, text in chats)
    return [{"role": "user", "content": f"{prompt}\n\n{TRANSCRIPT_NOTE}\n\n{transcripts}"}]


def text_model() -> str:
    return (os.getenv("FINETUNE_TEXT_MODEL") or "").strip() or DEFAULT_TEXT_MODEL


def text_max_tokens() -> int:
    try:
        return max(1000, int(os.getenv("FINETUNE_TEXT_MAX_TOKENS") or DEFAULT_TEXT_MAX_TOKENS))
    except ValueError:
        return DEFAULT_TEXT_MAX_TOKENS


# ----------------------------------------------------------------- output


def split_blocks(text: str) -> dict[str, str]:
    """The model's answer by block: "business", "industry", "notes"."""
    found: dict[str, str] = {}
    headers = list(_HEADER.finditer(text or ""))
    for i, header in enumerate(headers):
        title = header.group(1).upper()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[header.end():end].strip()
        if title.startswith("BUSINESS LAYER"):
            found.setdefault("business", body)
        elif title.startswith("INDUSTRY STANDARD"):
            found.setdefault("industry", body)
        elif title.startswith("NOTES"):
            found.setdefault("notes", body)
    return found


def _json_object(body: str, what: str) -> dict[str, Any]:
    body = _FENCE.sub("", body.strip()).strip()
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end < start:
        raise FinetuneError(f"The {what} block has no JSON object.")
    try:
        value = json.loads(body[start:end + 1])
    except ValueError as exc:
        raise FinetuneError(f"The {what} block is not valid JSON: {exc}.") from None
    if not isinstance(value, dict):
        raise FinetuneError(f"The {what} block is not a JSON object.")
    return value


def check_business_layer(layer: Any) -> dict[str, Any]:
    try:
        return prompt_layers.validate_client(layer)
    except prompt_layers.PromptError as exc:
        raise FinetuneError(f"Business layer: {exc}") from None


def check_industry_sections(sections: Any) -> dict[str, str]:
    try:
        clean = prompt_layers.validate_industry({"sections": sections})["sections"]
    except prompt_layers.PromptError as exc:
        raise FinetuneError(f"Industry standard: {exc}") from None
    if not clean.get("boundaries"):
        raise FinetuneError("Industry standard: it has no boundaries section. Applying it would replace "
                            "the industry's boundaries with nothing.")
    return clean


def parse_output(text: str) -> dict[str, Any]:
    """What the run proposes, plus every problem found. A block that parses
    but does not validate is still returned, so the admin can fix it by
    hand instead of paying for another run."""
    blocks = split_blocks(text)
    errors: list[str] = []
    business: Optional[dict[str, Any]] = None
    industry: Optional[dict[str, Any]] = None

    if "business" not in blocks:
        errors.append("The answer has no === BUSINESS LAYER === block.")
    else:
        try:
            business = _json_object(blocks["business"], "business layer")
            business = check_business_layer(business)
        except FinetuneError as exc:
            errors.append(str(exc))

    if "industry" not in blocks:
        errors.append("The answer has no === INDUSTRY STANDARD === block.")
    else:
        try:
            parsed = _json_object(blocks["industry"], "industry standard")
            industry = parsed.get("sections", parsed)
            industry = check_industry_sections(industry)
        except FinetuneError as exc:
            errors.append(str(exc))

    return {"business_layer": business, "industry_sections": industry,
            "notes": blocks.get("notes", ""), "errors": errors}


# ------------------------------------------------------------------ model


def max_tokens() -> int:
    try:
        return max(1000, int(os.getenv("FINETUNE_MAX_TOKENS") or DEFAULT_MAX_TOKENS))
    except ValueError:
        return DEFAULT_MAX_TOKENS


async def complete(
    prompt: str,
    images: Sequence[tuple[str, bytes]],
    *,
    api_url: str,
    api_key: str,
    model: str,
    usage_sink: Optional[UsageSink] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """The model's raw answer. Raises vision.VisionError with a safe message."""
    vision._check_endpoint(api_url, api_key, model)
    owns = client is None
    http = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        text = await vision._complete(
            api_url=api_url, api_key=api_key, client=http, usage_sink=usage_sink,
            payload={"model": model, "messages": build_messages(prompt, images),
                     "max_tokens": max_tokens(), "temperature": 0.2},
        )
    finally:
        if owns:
            await http.aclose()
    if not text:
        raise vision.VisionError("The model returned an empty answer.")
    return text


async def complete_text(
    prompt: str,
    chats: Sequence[tuple[str, str]],
    *,
    api_key: str,
    model: str,
    usage_sink: Optional[UsageSink] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """The text model's raw answer for transcribed chats. DeepSeek speaks the
    same chat-completions format, so this reuses vision.py's call, which
    allows a long answer the time it takes (ai_responder's cap is for
    replies to customers). Raises vision.VisionError with a safe message."""
    if not api_key:
        raise vision.VisionError("DEEPSEEK_PLATFORM_KEY is not set, so transcripts cannot be read.")
    owns = client is None
    http = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        text = await vision._complete(
            api_url=ai_responder.API_URL, api_key=api_key, client=http, usage_sink=usage_sink,
            service="DeepSeek API", key_name="DEEPSEEK_PLATFORM_KEY",
            payload={"model": model, "messages": build_text_messages(prompt, chats),
                     "max_tokens": text_max_tokens(), "temperature": 0.2},
        )
    finally:
        if owns:
            await http.aclose()
    if not text:
        raise vision.VisionError("The model returned an empty answer.")
    return text
