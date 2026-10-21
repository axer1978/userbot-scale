"""embedder/app.py, the embedder service, with a stand-in model: the real
one (fastembed) lives only in the embedder image, whose build loads it once
(`python app.py --download`)."""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

import embeddings

APP_FILE = Path(__file__).resolve().parent.parent / "embedder" / "app.py"
if not APP_FILE.exists():  # the app image leaves embedder/ out (.dockerignore)
    pytest.skip("embedder/ is not in this checkout", allow_module_level=True)


def _load():
    spec = importlib.util.spec_from_file_location("embedder_app", APP_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


embedder_app = _load()
PREFIX = embedder_app.PREFIX


class FakeModel:
    """Vector of a text: [its length, 1, 0], not unit length on purpose."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.batch_sizes: list[int] = []

    def embed(self, documents, batch_size=256):
        self.calls.append(list(documents))
        self.batch_sizes.append(batch_size)
        return ([float(len(d)), 1.0, 0.0] for d in documents)


@pytest_asyncio.fixture
async def served():
    """The app as uvicorn runs it: started (the lifespan loads and warms the
    model), then asked over HTTP."""
    model = FakeModel()
    service = embedder_app.create_app(model)
    async with service.router.lifespan_context(service):
        transport = httpx.ASGITransport(app=service)
        async with httpx.AsyncClient(transport=transport, base_url="http://embedder:8000") as client:
            yield client, model


def _unit(*values):
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values]


def test_importing_it_neither_loads_a_model_nor_needs_fastembed():
    assert "fastembed" not in sys.modules
    assert embedder_app.app is not None


def test_the_prefix_fits_the_model():
    # e5 models want "query: " before a text compared with other texts;
    # the paraphrase models want nothing.
    assert PREFIX == ("query: " if "e5" in embedder_app.MODEL.lower() else "")


@pytest.mark.asyncio
async def test_the_model_is_warmed_up_before_it_answers(served):
    client, model = served
    assert model.calls == [[PREFIX + "warm-up"]]
    assert (await client.get("/health")).json() == {"model": embedder_app.MODEL, "dim": 3}


@pytest.mark.asyncio
async def test_embed_gives_unit_vectors_in_order(served):
    client, model = served
    response = await client.post("/embed", json={"texts": ["hi", "can I come at 22?"]})
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == embedder_app.MODEL and body["dim"] == 3
    assert model.calls[-1] == [PREFIX + "hi", PREFIX + "can I come at 22?"]
    assert body["vectors"] == [_unit(len(PREFIX + "hi"), 1, 0), _unit(len(PREFIX + "can I come at 22?"), 1, 0)]
    for vector in body["vectors"]:
        assert sum(x * x for x in vector) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_the_model_takes_small_batches_to_cap_its_memory(served):
    client, model = served
    await client.post("/embed", json={"texts": ["x"] * embedder_app.MAX_TEXTS})
    assert set(model.batch_sizes) == {embedder_app.MODEL_BATCH}
    assert embedder_app.MODEL_BATCH <= 8


@pytest.mark.asyncio
async def test_a_long_text_is_cut(served):
    client, model = served
    await client.post("/embed", json={"texts": ["x" * (embedder_app.MAX_CHARS + 500)]})
    assert model.calls[-1] == [PREFIX + "x" * embedder_app.MAX_CHARS]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"texts": []},
    {"texts": ["a"] * (embedder_app.MAX_TEXTS + 1)},
    {"texts": ["ok", "   "]},
    {"texts": [123]},
    {"texts": "a"},
    {},
], ids=["empty", "too-many", "blank", "not-text", "not-a-list", "missing"])
async def test_bad_requests_are_refused_before_the_model(served, body):
    client, model = served
    before = len(model.calls)
    assert (await client.post("/embed", json=body)).status_code == 422
    assert len(model.calls) == before


@pytest.mark.asyncio
async def test_only_the_two_routes_exist(served):
    client, _ = served
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await client.get(path)).status_code == 404


def test_client_and_service_agree_on_the_batch_size():
    assert embeddings.MAX_TEXTS == embedder_app.MAX_TEXTS


@pytest.mark.asyncio
async def test_the_client_reads_what_the_service_answers(served):
    """embeddings.embed() against this app, batches and all."""
    client, model = served
    texts = [f"text {i}" for i in range(embeddings.MAX_TEXTS + 3)]
    got = await embeddings.embed(texts, client=client)
    assert got.model == embedder_app.MODEL
    assert got.vectors == [_unit(len(PREFIX + t), 1, 0) for t in texts]
    assert [len(call) for call in model.calls[1:]] == [embeddings.MAX_TEXTS, 3]
