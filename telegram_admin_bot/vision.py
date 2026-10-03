"""Photo understanding through any OpenAI-compatible vision model.

The platform's main model (DeepSeek) cannot see images, so photos go to a
separately configured chat-completions endpoint: OpenAI, OpenRouter,
Gemini's OpenAI-compatible endpoint, Qwen, or anything else that speaks the
same format. The endpoint and its key are platform-wide (VISION_API_URL,
VISION_API_KEY); the model name comes from the tenant config, so a tenant
can be moved to a cheaper or better model without a deploy.

Two jobs:

* describe_photo() turns a customer's photo into one or two factual
  sentences the text model can reply to. It is told never to describe a
  person's appearance, age, gender, ethnicity, body, health or emotions —
  the text model does not need any of that to answer, and it is exactly the
  kind of inference a business must not be making about its customers.
* compare_to_reference() answers "is this customer standing at our door?"
  by showing the model the business's own reference photos of its entrance
  followed by the customer's photo. It returns a PhotoMatch and never
  raises for a confused model answer: a wrong "no" only means a human
  checks, so an unreadable answer is simply "not the same place".

Transport behaviour mirrors ai_responder._complete and reuses its helpers:
retry on timeout, network errors, 429 and 5xx (honouring Retry-After),
fail at once on 401/403, and never let the key into error text.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

import httpx

from ai_responder import (
    BASE_BACKOFF_SECONDS,
    MAX_ATTEMPTS,
    REQUEST_TIMEOUT_SECONDS,
    UsageSink,
    _clip,
    _redact,
    _report_usage,
    _retry_after,
)

log = logging.getLogger(__name__)

# Most providers cap an inline image somewhere around here, and a phone
# photo larger than this is a sign the caller forgot to use Telegram's
# downscaled copy. Rejecting it locally is cheaper than a 413 after upload.
MAX_IMAGE_BYTES = 5 * 1024 * 1024

# A description goes into the text model's prompt; anything longer than
# this is the vision model rambling, not information.
MAX_DESCRIPTION_CHARS = 500

DESCRIBE_SYSTEM_PROMPT = (
    "You describe photos that a customer sent to a business in a customer-"
    "service chat. In at most 2 short, factual sentences, say what the photo "
    "shows that matters for answering the customer: the object, product, "
    "place, document or problem in it, and any readable text (quote it "
    "exactly). Do not guess at anything you cannot see. Never describe any "
    "person's appearance, age, gender, ethnicity, body, health or emotions; "
    "if people are in the photo, just say \"a person\" (or \"people\"). Output "
    "only the description — no preamble, no labels."
)

COMPARE_SYSTEM_PROMPT = (
    "You check whether a customer has arrived at a business. The first "
    "images are reference photos of the business's entrance/door, provided by "
    "the business. The LAST image is a photo the customer just sent to show "
    "they have arrived. Decide whether the customer's photo shows the same "
    "entrance, door or place as the reference photos. A different angle, "
    "distance, lighting, weather or time of day is expected and still counts "
    "as the same place. A different door, a random street, a screenshot, or a "
    "photo of a photo or of a screen is NOT the same place. Answer with a "
    "single JSON object and nothing else, exactly in this form: "
    '{"same_place": true or false, "confidence": a number from 0 to 1}'
)

# describe gets a little room for a two-sentence answer; compare only needs
# a one-line JSON object, and a small cap stops a chatty model from billing
# a paragraph of reasoning.
COMPARE_MAX_TOKENS = 60

_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_OBJECT = re.compile(r"\{[^{}]*\}", re.DOTALL)


class VisionError(Exception):
    """Raised for any failure that should surface in the admin panel."""


@dataclass(frozen=True)
class PhotoMatch:
    same_place: bool
    confidence: float  # 0..1


NO_MATCH = PhotoMatch(False, 0.0)


def endpoint_from_env(env: Mapping[str, str] = os.environ) -> tuple[str, str]:
    """(url, key) from VISION_API_URL / VISION_API_KEY; empty strings when
    unset. Checking is left to the call, which names what is missing."""
    return (env.get("VISION_API_URL") or "").strip(), (env.get("VISION_API_KEY") or "").strip()


def sniff_mime(data: bytes) -> Optional[str]:
    """The image type from its magic bytes, or None. The file name and the
    Telegram-declared type are not trusted: the data URI has to be right or
    the provider rejects the request."""
    if not data:
        return None
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _data_uri(image: bytes, label: str) -> str:
    if len(image) > MAX_IMAGE_BYTES:
        raise VisionError(
            f"The {label} is too large ({len(image)} bytes; the limit is "
            f"{MAX_IMAGE_BYTES} bytes)."
        )
    mime = sniff_mime(image)
    if mime is None:
        raise VisionError(
            f"The {label} is not a supported image (JPEG, PNG, WebP or GIF)."
        )
    return f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"


def _image_part(image: bytes, label: str) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": _data_uri(image, label)}}


def _check_endpoint(api_url: str, api_key: str, model: str) -> None:
    if not (api_url or "").strip():
        raise VisionError("VISION_API_URL is not set, so photos cannot be read.")
    if not (api_key or "").strip():
        raise VisionError("VISION_API_KEY is not set, so photos cannot be read.")
    if not (model or "").strip():
        raise VisionError("No vision model is configured for this business.")


async def describe_photo(
    image: bytes,
    *,
    api_url: str,
    api_key: str,
    model: str,
    client: Optional[httpx.AsyncClient] = None,
    usage_sink: Optional[UsageSink] = None,
    max_tokens: int = 200,
) -> str:
    """A short, factual description of the photo for the text model's prompt,
    or VisionError with a safe message."""
    _check_endpoint(api_url, api_key, model)
    messages = [
        {"role": "system", "content": DESCRIBE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this photo as instructed."},
                _image_part(image, "photo"),
            ],
        },
    ]
    text = await _complete(
        api_url=api_url,
        api_key=api_key,
        payload={
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            # Describing, not writing: the same photo should read the same way.
            "temperature": 0.0,
        },
        client=client,
        usage_sink=usage_sink,
    )
    if not text:
        raise VisionError("The vision model returned an empty description.")
    # _clip adds an ellipsis after `limit` characters, so leave room for it.
    return _clip(text, MAX_DESCRIPTION_CHARS - 1)


async def compare_to_reference(
    photo: bytes,
    references: Sequence[bytes],
    *,
    api_url: str,
    api_key: str,
    model: str,
    client: Optional[httpx.AsyncClient] = None,
    usage_sink: Optional[UsageSink] = None,
) -> PhotoMatch:
    """Whether `photo` shows the same entrance as the business's reference
    photos. Transport and configuration problems raise VisionError; an
    unreadable model answer is PhotoMatch(False, 0.0)."""
    _check_endpoint(api_url, api_key, model)
    if not references:
        raise VisionError("No reference photos of the entrance have been provided.")

    # Labels between the images make "the last one is the customer's" hard
    # to misread; the image parts themselves stay references-first.
    content: list[dict[str, Any]] = [
        {"type": "text", "text": f"Reference photos of the entrance ({len(references)}):"}
    ]
    for number, reference in enumerate(references, start=1):
        content.append(_image_part(reference, f"reference photo {number}"))
    content.append({"type": "text", "text": "The customer's photo (the last image):"})
    content.append(_image_part(photo, "customer photo"))

    text = await _complete(
        api_url=api_url,
        api_key=api_key,
        payload={
            "model": model,
            "messages": [
                {"role": "system", "content": COMPARE_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            "max_tokens": COMPARE_MAX_TOKENS,
            "temperature": 0.0,
        },
        client=client,
        usage_sink=usage_sink,
    )
    return parse_match(text)


def parse_match(text: str) -> PhotoMatch:
    """The model's JSON verdict, found in code fences or surrounding prose.
    Anything that is not a clean {"same_place": bool, "confidence": 0..1}
    is treated as no match."""
    text = (text or "").strip()
    candidates = [text]
    candidates.extend(block.strip() for block in _FENCE.findall(text))
    candidates.extend(_OBJECT.findall(text))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return _match_from(data)
    return NO_MATCH


def _match_from(data: dict[str, Any]) -> PhotoMatch:
    same = data.get("same_place")
    confidence = data.get("confidence")
    # bool is an int in Python; a bare `true` as the confidence is not a number.
    if not isinstance(same, bool):
        return NO_MATCH
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return NO_MATCH
    confidence = float(confidence)
    if math.isnan(confidence) or not 0.0 <= confidence <= 1.0:
        return NO_MATCH
    return PhotoMatch(same, confidence)


async def _complete(
    *,
    api_url: str,
    api_key: str,
    payload: dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
    usage_sink: Optional[UsageSink] = None,
    service: str = "Vision API",
    key_name: str = "VISION_API_KEY",
) -> str:
    """One chat completion against the vision endpoint, with retry/backoff
    and safe error text. Returns the reply text stripped (possibly empty —
    the callers decide what an empty answer means). Any other
    OpenAI-compatible endpoint works too; `service` and `key_name` name it
    in the errors (finetune.py sends transcripts to DeepSeek this way)."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        last_error = f"{service} request failed."
        for attempt in range(1, MAX_ATTEMPTS + 1):
            delay = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
            try:
                response = await http.post(api_url, json=payload, headers=headers)
            except httpx.TimeoutException:
                last_error = f"{service} timed out."
            except httpx.HTTPError as exc:
                last_error = _redact(
                    f"Could not reach the {service}: {type(exc).__name__}.", api_key
                )
            else:
                if response.status_code == 200:
                    content = _parse_reply(response)
                    if usage_sink is not None:
                        await _report_usage(usage_sink, payload["model"], response)
                    return content

                detail = _clip(_redact(response.text, api_key))
                if response.status_code == 429:
                    last_error = f"{service} rate limit (429). {detail}"
                    delay = _retry_after(response, attempt)
                elif response.status_code in (401, 403):
                    # Not retryable — a bad key will not fix itself.
                    raise VisionError(
                        f"The {service} rejected the key (HTTP {response.status_code}). "
                        f"Check that {key_name} is correct."
                    )
                elif response.status_code >= 500:
                    last_error = f"{service} server error (HTTP {response.status_code}). {detail}"
                    delay = _retry_after(response, attempt)
                else:
                    raise VisionError(
                        f"{service} error (HTTP {response.status_code}). {detail}"
                    )

            if attempt < MAX_ATTEMPTS:
                log.warning(
                    "Vision attempt %s/%s failed (%s); retrying in %.1fs",
                    attempt, MAX_ATTEMPTS, last_error, delay,
                )
                await asyncio.sleep(delay)

        raise VisionError(f"{last_error} Gave up after {MAX_ATTEMPTS} attempts.")
    finally:
        if owns_client:
            await http.aclose()


def _parse_reply(response: httpx.Response) -> str:
    """choices[0].message.content as text. Some OpenAI-compatible servers
    return content as a list of parts even for text; those are joined."""
    try:
        data = response.json()
    except ValueError:
        raise VisionError("The vision API returned a response that was not JSON.") from None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise VisionError(
            "The vision API response was missing choices[0].message.content."
        ) from None
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    if content is None:
        return ""
    if not isinstance(content, str):
        raise VisionError("The vision API returned content that was not text.")
    return content.strip()
