"""vision.py: request shape, retries, safe errors, and reading the verdict."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

import vision

API_URL = "https://vision.example/v1/chat/completions"
API_KEY = "sk-vision-secret-123"
MODEL = "some-vision-model"

JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-body"
PNG = b"\x89PNG\r\n\x1a\n" + b"png-body"
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"webp-body"
GIF = b"GIF89a" + b"gif-body"


def _ok(content, usage=None) -> httpx.Response:
    body = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


class Recorder:
    """A MockTransport handler that plays back canned responses in order
    and keeps every request it saw."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    def payload(self, index: int = -1) -> dict:
        return json.loads(self.requests[index].content)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def sleeps(monkeypatch):
    """Record backoff delays instead of waiting them out."""
    delays: list[float] = []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(vision.asyncio, "sleep", fake_sleep)
    return delays


def _kw(client):
    return {"api_url": API_URL, "api_key": API_KEY, "model": MODEL, "client": client}


def _image_urls(payload: dict) -> list[str]:
    user = payload["messages"][-1]["content"]
    return [part["image_url"]["url"] for part in user if part["type"] == "image_url"]


def _data_uri(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


# ------------------------------------------------------------ env and types


def test_endpoint_from_env_reads_and_strips():
    env = {"VISION_API_URL": " https://x/v1/chat/completions ", "VISION_API_KEY": " k "}
    assert vision.endpoint_from_env(env) == ("https://x/v1/chat/completions", "k")


def test_endpoint_from_env_unset_is_empty():
    assert vision.endpoint_from_env({}) == ("", "")


@pytest.mark.parametrize(
    "data, mime",
    [(JPEG, "image/jpeg"), (PNG, "image/png"), (WEBP, "image/webp"), (GIF, "image/gif"),
     (b"GIF87a...", "image/gif"), (b"%PDF-1.7", None), (b"", None), (b"RIFF\x00\x00\x00\x00WAVE", None)],
)
def test_sniff_mime(data, mime):
    assert vision.sniff_mime(data) == mime


# ------------------------------------------------------------ describe


@pytest.mark.asyncio
async def test_describe_request_shape():
    rec = Recorder(_ok("  A cracked phone screen showing the text \"SALE\".  "))
    async with _client(rec) as client:
        text = await vision.describe_photo(PNG, **_kw(client), max_tokens=150)

    assert text == 'A cracked phone screen showing the text "SALE".'
    request = rec.requests[0]
    assert str(request.url) == API_URL
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    payload = rec.payload()
    assert payload["model"] == MODEL
    assert payload["max_tokens"] == 150
    system, user = payload["messages"]
    assert system["role"] == "system"
    assert "a person" in system["content"] and "ethnicity" in system["content"]
    assert [part["type"] for part in user["content"]] == ["text", "image_url"]
    assert _image_urls(payload) == [_data_uri("image/png", PNG)]


@pytest.mark.asyncio
async def test_describe_clips_long_answers():
    rec = Recorder(_ok("word " * 400))
    async with _client(rec) as client:
        text = await vision.describe_photo(JPEG, **_kw(client))
    assert len(text) <= vision.MAX_DESCRIPTION_CHARS


@pytest.mark.asyncio
async def test_describe_empty_answer_is_an_error():
    rec = Recorder(_ok("   "))
    async with _client(rec) as client:
        with pytest.raises(vision.VisionError, match="empty"):
            await vision.describe_photo(JPEG, **_kw(client))


@pytest.mark.asyncio
async def test_describe_accepts_content_as_parts():
    rec = Recorder(_ok([{"type": "text", "text": "A red door."}]))
    async with _client(rec) as client:
        assert await vision.describe_photo(JPEG, **_kw(client)) == "A red door."


@pytest.mark.asyncio
async def test_usage_sink_gets_model_and_counts():
    seen = []

    async def sink(model, usage):
        seen.append((model, usage))

    rec = Recorder(_ok("A box.", usage={"prompt_tokens": 900, "completion_tokens": 12}))
    async with _client(rec) as client:
        await vision.describe_photo(JPEG, **_kw(client), usage_sink=sink)

    assert seen == [(MODEL, {
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 900,
        "completion_tokens": 12,
    })]


@pytest.mark.asyncio
async def test_failing_usage_sink_does_not_cost_the_answer():
    async def sink(model, usage):
        raise RuntimeError("db down")

    rec = Recorder(_ok("A box."))
    async with _client(rec) as client:
        assert await vision.describe_photo(JPEG, **_kw(client), usage_sink=sink) == "A box."


# ------------------------------------------------------------ rejection before any request


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides, needle",
    [({"api_url": ""}, "VISION_API_URL"), ({"api_key": ""}, "VISION_API_KEY"), ({"model": ""}, "model")],
)
async def test_missing_configuration_is_named(overrides, needle):
    rec = Recorder()
    async with _client(rec) as client:
        with pytest.raises(vision.VisionError, match=needle):
            await vision.describe_photo(JPEG, **{**_kw(client), **overrides})
    assert rec.requests == []


@pytest.mark.asyncio
async def test_oversized_image_rejected_before_request():
    rec = Recorder()
    big = JPEG + b"\x00" * vision.MAX_IMAGE_BYTES
    async with _client(rec) as client:
        with pytest.raises(vision.VisionError, match="too large"):
            await vision.describe_photo(big, **_kw(client))
    assert rec.requests == []


@pytest.mark.asyncio
async def test_unknown_image_type_rejected_before_request():
    rec = Recorder()
    async with _client(rec) as client:
        with pytest.raises(vision.VisionError, match="not a supported image"):
            await vision.compare_to_reference(b"%PDF-1.7", [JPEG], **_kw(client))
        with pytest.raises(vision.VisionError, match="reference photo 2"):
            await vision.compare_to_reference(JPEG, [JPEG, b"nope"], **_kw(client))
    assert rec.requests == []


@pytest.mark.asyncio
async def test_no_references_is_an_error():
    rec = Recorder()
    async with _client(rec) as client:
        with pytest.raises(vision.VisionError, match="reference"):
            await vision.compare_to_reference(JPEG, [], **_kw(client))
    assert rec.requests == []


# ------------------------------------------------------------ transport errors


@pytest.mark.asyncio
async def test_401_is_not_retried_and_key_is_not_leaked(sleeps):
    rec = Recorder(httpx.Response(401, text=f"invalid key {API_KEY}"))
    async with _client(rec) as client:
        with pytest.raises(vision.VisionError) as info:
            await vision.describe_photo(JPEG, **_kw(client))
    assert len(rec.requests) == 1
    assert sleeps == []
    assert "401" in str(info.value) and "VISION_API_KEY" in str(info.value)
    assert API_KEY not in str(info.value)


@pytest.mark.asyncio
async def test_other_4xx_fails_at_once_with_redacted_detail(sleeps):
    rec = Recorder(httpx.Response(400, text=f"bad request for key {API_KEY}"))
    async with _client(rec) as client:
        with pytest.raises(vision.VisionError) as info:
            await vision.describe_photo(JPEG, **_kw(client))
    assert len(rec.requests) == 1
    assert "400" in str(info.value) and "***" in str(info.value)
    assert API_KEY not in str(info.value)


@pytest.mark.asyncio
async def test_429_is_retried_honouring_retry_after(sleeps):
    rec = Recorder(
        httpx.Response(429, headers={"Retry-After": "7"}, text="slow down"),
        _ok("A door."),
    )
    async with _client(rec) as client:
        assert await vision.describe_photo(JPEG, **_kw(client)) == "A door."
    assert len(rec.requests) == 2
    assert sleeps == [7.0]


@pytest.mark.asyncio
async def test_5xx_retried_then_gives_up_without_leaking_key(sleeps):
    rec = Recorder(*[httpx.Response(503, text=f"overloaded {API_KEY}")] * vision.MAX_ATTEMPTS)
    async with _client(rec) as client:
        with pytest.raises(vision.VisionError) as info:
            await vision.describe_photo(JPEG, **_kw(client))
    assert len(rec.requests) == vision.MAX_ATTEMPTS
    assert len(sleeps) == vision.MAX_ATTEMPTS - 1
    assert "Gave up" in str(info.value) and "503" in str(info.value)
    assert API_KEY not in str(info.value)


@pytest.mark.asyncio
async def test_timeouts_and_network_errors_are_retried(sleeps):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        if len(calls) == 2:
            raise httpx.ConnectError(f"refused {API_KEY}", request=request)
        return _ok("A sign.")

    async with _client(handler) as client:
        assert await vision.describe_photo(JPEG, **_kw(client)) == "A sign."
    assert len(calls) == 3
    assert sleeps == [vision.BASE_BACKOFF_SECONDS, vision.BASE_BACKOFF_SECONDS * 2]


@pytest.mark.asyncio
async def test_network_failure_text_has_no_key(sleeps):
    def handler(request):
        raise httpx.ConnectError(f"refused {API_KEY}", request=request)

    async with _client(handler) as client:
        with pytest.raises(vision.VisionError) as info:
            await vision.describe_photo(JPEG, **_kw(client))
    assert "ConnectError" in str(info.value)
    assert API_KEY not in str(info.value)


@pytest.mark.asyncio
async def test_body_that_is_not_json_is_an_error():
    rec = Recorder(httpx.Response(200, text="<html>oops</html>"))
    async with _client(rec) as client:
        with pytest.raises(vision.VisionError, match="not JSON"):
            await vision.describe_photo(JPEG, **_kw(client))


# ------------------------------------------------------------ compare


@pytest.mark.asyncio
async def test_compare_request_shape_references_first_customer_last():
    rec = Recorder(_ok('{"same_place": true, "confidence": 0.93}'))
    refs = [JPEG, PNG]
    async with _client(rec) as client:
        match = await vision.compare_to_reference(WEBP, refs, **_kw(client))

    assert match == vision.PhotoMatch(True, 0.93)
    payload = rec.payload()
    assert payload["model"] == MODEL
    assert payload["temperature"] == 0
    assert payload["messages"][0]["role"] == "system"
    assert "JSON" in payload["messages"][0]["content"]
    assert _image_urls(payload) == [
        _data_uri("image/jpeg", JPEG),
        _data_uri("image/png", PNG),
        _data_uri("image/webp", WEBP),
    ]
    # The last content part of the user turn is the customer's photo.
    last = payload["messages"][-1]["content"][-1]
    assert last["type"] == "image_url"
    assert last["image_url"]["url"] == _data_uri("image/webp", WEBP)
    assert any(part["type"] == "text" for part in payload["messages"][-1]["content"])


@pytest.mark.asyncio
async def test_compare_reports_usage():
    seen = []

    async def sink(model, usage):
        seen.append(model)

    rec = Recorder(_ok('{"same_place": false, "confidence": 0.8}', usage={"prompt_tokens": 3}))
    async with _client(rec) as client:
        match = await vision.compare_to_reference(JPEG, [JPEG], **_kw(client), usage_sink=sink)
    assert match == vision.PhotoMatch(False, 0.8)
    assert seen == [MODEL]


@pytest.mark.asyncio
async def test_compare_garbage_answer_is_no_match():
    rec = Recorder(_ok("I cannot tell, sorry."))
    async with _client(rec) as client:
        assert await vision.compare_to_reference(JPEG, [JPEG], **_kw(client)) == vision.PhotoMatch(False, 0.0)


@pytest.mark.parametrize(
    "text, expected",
    [
        ('{"same_place": true, "confidence": 0.9}', vision.PhotoMatch(True, 0.9)),
        ('```json\n{"same_place": true, "confidence": 1}\n```', vision.PhotoMatch(True, 1.0)),
        ('```\n{"same_place": false, "confidence": 0.7}\n```', vision.PhotoMatch(False, 0.7)),
        ('Sure! Here is my answer: {"same_place": true, "confidence": 0.65} Hope it helps.',
         vision.PhotoMatch(True, 0.65)),
        ('{"confidence": 0, "same_place": false}', vision.PhotoMatch(False, 0.0)),
    ],
)
def test_parse_match_variants(text, expected):
    assert vision.parse_match(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "yes",
        "{not json}",
        '{"same_place": true, "confidence": 1.5}',
        '{"same_place": true, "confidence": -0.1}',
        '{"same_place": "yes", "confidence": 0.9}',
        '{"same_place": true, "confidence": "high"}',
        '{"same_place": true, "confidence": true}',
        '{"same_place": true}',
        '{"same_place": true, "confidence": NaN}',
        '[true, 0.9]',
    ],
)
def test_parse_match_garbage_is_no_match(text):
    assert vision.parse_match(text) == vision.PhotoMatch(False, 0.0)
