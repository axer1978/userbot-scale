"""embeddings.py: the client for the embedder service. Fails open: any
trouble with the embedder is None, never an exception, and no text in a log."""

from __future__ import annotations

import json
import logging
import math

import httpx
import pytest

import embeddings

SECRET = "my secret client message"


def _unit(*values: float) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values]


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _answer(model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", dim: int = 3):
    """A stand-in embedder: vector i of a request is unit vector [i+1, 1, 0...]."""
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/embed"
        texts = json.loads(request.content)["texts"]
        calls.append(texts)
        vectors = [_unit(len(t), 1.0, *([0.0] * (dim - 2))) for t in texts]
        return httpx.Response(200, json={"model": model, "dim": dim, "vectors": vectors})

    return handler, calls


@pytest.mark.asyncio
async def test_one_vector_per_text_in_order(monkeypatch):
    monkeypatch.setenv("EMBEDDER_URL", "http://embedder.test:8000/")
    seen: list[str] = []
    handler, calls = _answer()

    def recording(request):
        seen.append(str(request.url))
        return handler(request)

    async with _client(recording) as client:
        got = await embeddings.embed(["a", "bbb"], client=client)
    assert seen == ["http://embedder.test:8000/embed"]
    assert calls == [["a", "bbb"]]
    assert got.model == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    assert got.vectors == [_unit(1, 1, 0), _unit(3, 1, 0)]


@pytest.mark.asyncio
async def test_default_url_is_the_compose_service(monkeypatch):
    monkeypatch.delenv("EMBEDDER_URL", raising=False)
    assert embeddings.url() == "http://embedder:8000"


@pytest.mark.asyncio
async def test_nothing_to_embed_asks_nothing():
    def handler(request):
        raise AssertionError("no request expected")

    async with _client(handler) as client:
        got = await embeddings.embed([], client=client)
    assert got == embeddings.Embedded(model="", vectors=[])


@pytest.mark.asyncio
async def test_long_lists_go_in_batches_and_keep_their_order():
    handler, calls = _answer()
    texts = ["x" * (i % 7 + 1) for i in range(embeddings.MAX_TEXTS * 2 + 5)]
    async with _client(handler) as client:
        got = await embeddings.embed(texts, client=client)
    assert [len(batch) for batch in calls] == [embeddings.MAX_TEXTS, embeddings.MAX_TEXTS, 5]
    assert [t for batch in calls for t in batch] == texts
    assert got.vectors == [_unit(len(t), 1, 0) for t in texts]


def _status(code: int, body: object = None):
    return lambda request: httpx.Response(code, json=body if body is not None else {"detail": "x"})


def _raises(exc: Exception):
    def handler(request):
        raise exc
    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [
    _raises(httpx.ConnectError("refused")),            # profile off: no embedder
    _raises(httpx.ReadTimeout("slow")),
    _status(500),
    _status(422),
    lambda request: httpx.Response(200, content=b"not json"),
    _status(200, {"vectors": [[1.0, 0.0]]}),                                   # no model
    _status(200, {"model": "m", "vectors": [[1.0, 0.0], [0.0, 1.0]]}),         # two for one text
    _status(200, {"model": "m", "vectors": [["a", "b"]]}),                     # not numbers
    _status(200, {"model": "m", "vectors": [[True, False]]}),
    _status(200, {"model": "m", "vectors": [[]]}),
    _status(200, {"model": "m", "vectors": "nope"}),
], ids=["refused", "timeout", "500", "422", "not-json", "no-model", "wrong-count", "strings", "bools",
        "empty-vector", "not-a-list"])
async def test_any_trouble_is_none_and_no_text_is_logged(handler, caplog):
    caplog.set_level(logging.DEBUG, logger="embeddings")
    async with _client(handler) as client:
        got = await embeddings.embed([SECRET], client=client)
    assert got is None
    assert caplog.records, "a failure is logged"
    assert SECRET not in caplog.text


@pytest.mark.asyncio
async def test_batches_from_two_models_or_sizes_are_refused():
    models = iter(["model-a", "model-b"])

    def two_models(request):
        texts = json.loads(request.content)["texts"]
        return httpx.Response(200, json={"model": next(models), "vectors": [[1.0, 0.0]] * len(texts)})

    async with _client(two_models) as client:
        assert await embeddings.embed(["t"] * (embeddings.MAX_TEXTS + 1), client=client) is None

    sizes = iter([2, 3])

    def two_sizes(request):
        texts = json.loads(request.content)["texts"]
        dim = next(sizes)
        return httpx.Response(200, json={"model": "m", "vectors": [_unit(1, *([0.0] * (dim - 1)))] * len(texts)})

    async with _client(two_sizes) as client:
        assert await embeddings.embed(["t"] * (embeddings.MAX_TEXTS + 1), client=client) is None


@pytest.mark.asyncio
async def test_the_timeout_reaches_the_request():
    seen: list[dict] = []
    handler, _ = _answer()

    def recording(request):
        seen.append(request.extensions["timeout"])
        return handler(request)

    async with _client(recording) as client:
        await embeddings.embed(["a"], client=client)
        await embeddings.embed(["a"], client=client, timeout=30.0)
    assert seen[0]["read"] == embeddings.TIMEOUT_SECONDS
    assert seen[1]["read"] == 30.0


def test_similarity_of_unit_vectors():
    a, b = _unit(1, 0, 0), _unit(1, 1, 0)
    assert embeddings.similarity(a, a) == pytest.approx(1.0)
    assert embeddings.similarity(a, b) == pytest.approx(math.sqrt(0.5))
    assert embeddings.similarity(a, _unit(0, 1, 0)) == pytest.approx(0.0)
    with pytest.raises(ValueError):
        embeddings.similarity(a, [1.0, 0.0])
