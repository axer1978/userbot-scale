"""The embedder: short texts in, vectors out, so the bot can find past chat
moments like the one it is answering (embeddings.py is the client).

DeepSeek has no embeddings endpoint, and sending client messages to a second
company for them is not wanted, so a small multilingual model runs here on
the server's CPU through fastembed (ONNX Runtime, no PyTorch). One copy of
the model serves the whole stack over the compose network. Nothing is
published to the host and nothing is kept: texts come in, vectors go out,
and no text is logged.

    POST /embed   {"texts": ["...", ...]}  ->  {"model": ..., "dim": 384, "vectors": [[...], ...]}
    GET  /health  ->  {"model": ..., "dim": 384}

The vectors are unit length: the dot product of two is their cosine
similarity.

The model, paraphrase-multilingual-MiniLM-L12-v2, is made for "do these two
sentences mean the same", which is the question here: a client's message
against messages from earlier chats. It knows about 50 languages, Latvian and
Russian among them. On made-up client questions in English, Latvian and
Russian, the closest match of 9 in 10 meant the same thing; one message takes
about 15 ms on one CPU thread.

The model files are downloaded when the image is built (`python app.py
--download`), so the container starts without the internet and always runs
the same files. Another model means changing MODEL and rebuilding the image,
and the vectors stored with the old one no longer compare.
"""

from __future__ import annotations

import math
import os
import sys
import threading
from contextlib import asynccontextmanager
from typing import Any, Iterable, Optional, Protocol

from fastapi import FastAPI
from pydantic import BaseModel, Field, field_validator

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
CACHE_DIR = os.environ.get("EMBED_CACHE_DIR") or "/models"
# ONNX Runtime threads for one batch. The server has two CPUs and the bots
# come first, so one by default (compose also caps the container at one CPU).
THREADS = int(os.environ.get("EMBED_THREADS") or 1)
# Per request. embeddings.py splits longer lists to match.
MAX_TEXTS = 64
# Texts through the model at once. ONNX Runtime keeps the memory of its
# largest run: 64 long messages at once peaked at ~1 GB (the compose limit),
# 8 at a time at ~0.65 GB, and no slower.
MODEL_BATCH = 8
# A chat message longer than this is cut: the model reads only its first
# 128 tokens anyway, and the cut keeps tokenizing cheap.
MAX_CHARS = 2000
# The e5 models want "query: " before a text compared with other texts; the
# paraphrase models want nothing.
PREFIX = "query: " if "e5" in MODEL.lower() else ""


class Model(Protocol):
    def embed(self, documents: list[str], batch_size: int = ...) -> Iterable[Any]: ...


def load_model() -> Model:
    """The real model. fastembed is imported here, not at the top, so the
    tests can run this app with a stand-in and without fastembed."""
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=MODEL, cache_dir=CACHE_DIR, threads=THREADS)


class EmbedBody(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=MAX_TEXTS)

    @field_validator("texts")
    @classmethod
    def _not_blank(cls, texts: list[str]) -> list[str]:
        if any(not text.strip() for text in texts):
            raise ValueError("texts must not be blank")
        return texts


def _unit(vector: Iterable[Any]) -> list[float]:
    values = [float(x) for x in vector]
    norm = math.sqrt(sum(x * x for x in values))
    return [x / norm for x in values] if norm else values


def create_app(model: Optional[Model] = None) -> FastAPI:
    state: dict[str, Any] = {"model": model, "dim": 0}
    # One batch at a time: the thread count above is the CPU this may use,
    # and parallel batches would only take more of it.
    lock = threading.Lock()

    def run(texts: list[str]) -> list[list[float]]:
        documents = [PREFIX + text[:MAX_CHARS] for text in texts]
        with lock:
            return [_unit(v) for v in state["model"].embed(documents, batch_size=MODEL_BATCH)]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if state["model"] is None:
            state["model"] = load_model()
        # Loaded and answering before the first request (and the healthcheck).
        state["dim"] = len(run(["warm-up"])[0])
        yield

    app = FastAPI(title="userbot embedder", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"model": MODEL, "dim": state["dim"]}

    # A plain def: FastAPI runs it on a worker thread, so a batch never
    # blocks the event loop (or the healthcheck) while it is embedded.
    @app.post("/embed")
    def embed(body: EmbedBody) -> dict[str, Any]:
        vectors = run(body.texts)
        return {"model": MODEL, "dim": len(vectors[0]), "vectors": vectors}

    return app


# What uvicorn serves. The model loads when it starts serving, not here.
app = create_app()


if __name__ == "__main__" and sys.argv[1:] == ["--download"]:
    # Build time: fetch the model files into CACHE_DIR and check they load.
    vector = next(iter(load_model().embed([PREFIX + "warm-up"], batch_size=MODEL_BATCH)))
    print(f"{MODEL}: {len(list(vector))} dimensions, cached in {CACHE_DIR}")
