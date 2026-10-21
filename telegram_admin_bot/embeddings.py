"""Client for the embedder service (embedder/app.py): short texts in,
vectors out, for finding chat moments like the one being answered.

DeepSeek has no embeddings endpoint, so a small multilingual model runs in
its own container on this server (the `retrieval` compose profile) and the
app services ask it over the compose network. No text leaves the server.

The vectors are unit length: the dot product of two (similarity()) is their
cosine similarity, 1.0 for the same meaning.

Nothing may fail or wait long because of it. embed() returns None when the
embedder is not running, is slow or answers something unexpected, and the
caller carries on without vectors. Texts are never logged.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional, Sequence

import httpx

log = logging.getLogger(__name__)

DEFAULT_URL = "http://embedder:8000"
# The embedder's own limit per request (embedder/app.py MAX_TEXTS); longer
# lists go as several requests.
MAX_TEXTS = 64
# Enough for a reply's one or two texts (~30 ms each). A backfill sending
# full batches passes a longer one: 64 long messages take ~10-20 s on the
# embedder's one CPU thread.
TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class Embedded:
    # Which model made the vectors: vectors of two models never compare.
    model: str
    vectors: list[list[float]]


def url() -> str:
    return (os.environ.get("EMBEDDER_URL") or DEFAULT_URL).rstrip("/")


async def embed(
    texts: Sequence[str],
    *,
    client: Optional[httpx.AsyncClient] = None,
    timeout: float = TIMEOUT_SECONDS,
) -> Optional[Embedded]:
    """One vector per text, in order, or None when the embedder cannot be
    used right now. Texts must not be blank (the embedder refuses those).

    Pass a shared `client` (the runtime's http_client): a new httpx client
    costs ~0.4 s to set up, more than ten embeddings."""
    texts = list(texts)
    if not texts:
        return Embedded(model="", vectors=[])
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout)
    model = ""
    vectors: list[list[float]] = []
    try:
        for start in range(0, len(texts), MAX_TEXTS):
            batch = texts[start:start + MAX_TEXTS]
            response = await http.post(f"{url()}/embed", json={"texts": batch}, timeout=timeout)
            if response.status_code != 200:
                log.warning("Embedder answered HTTP %s for %d texts.", response.status_code, len(batch))
                return None
            got_model, got = _parse(response, len(batch))
            if got is None or (model and got_model != model):
                log.warning("Embedder answered something unexpected for %d texts.", len(batch))
                return None
            model = got_model
            vectors.extend(got)
    except httpx.HTTPError as exc:
        log.warning("Embedder not reachable (%s).", type(exc).__name__)
        return None
    finally:
        if owns_client:
            await http.aclose()
    if len({len(v) for v in vectors}) != 1:
        log.warning("Embedder returned vectors of different sizes.")
        return None
    return Embedded(model=model, vectors=vectors)


async def health(
    *, client: Optional[httpx.AsyncClient] = None, timeout: float = TIMEOUT_SECONDS,
) -> Optional[str]:
    """The model the embedder runs, or None when it is not answering. For
    the admin's status page, and to find vectors made by another model
    before embedding anything."""
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout)
    try:
        response = await http.get(f"{url()}/health", timeout=timeout)
        model = response.json().get("model") if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        log.warning("Embedder not reachable (%s).", type(exc).__name__)
        return None
    finally:
        if owns_client:
            await http.aclose()
    return model if isinstance(model, str) and model else None


def _parse(response: httpx.Response, count: int) -> tuple[str, Optional[list[list[float]]]]:
    try:
        data = response.json()
        model, vectors = data["model"], data["vectors"]
    except (ValueError, KeyError, TypeError):
        return "", None
    if not isinstance(model, str) or not model or not isinstance(vectors, list) or len(vectors) != count:
        return "", None
    clean: list[list[float]] = []
    for vector in vectors:
        if not isinstance(vector, list) or not vector:
            return "", None
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in vector):
            return "", None
        clean.append([float(x) for x in vector])
    return model, clean


def similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two vectors from embed() (unit length)."""
    if len(a) != len(b):
        raise ValueError("vectors of different sizes")
    return sum(x * y for x, y in zip(a, b))
