"""Embedding calls, batched and retried.

Kept behind one function so swapping the model, or the whole provider, is a
one-file change. Document and query embeddings use different task types, which
is worth roughly a point of recall and costs nothing.
"""
from __future__ import annotations

import asyncio

BATCH = 32

# One client per key, reused. Building a genai.Client does a TLS handshake,
# and doing that inside a live call spent most of the tool budget before the
# request was even sent.
_CLIENTS: dict[str, object] = {}


def _client(api_key: str):
    from google import genai

    if api_key not in _CLIENTS:
        _CLIENTS[api_key] = genai.Client(api_key=api_key)
    return _CLIENTS[api_key]


async def embed_texts(
    texts: list[str],
    *,
    api_key: str,
    model: str,
    dims: int,
    task: str,
    attempts: int = 4,
    backoff_base: float = 2.0,
) -> list[list[float]]:
    from google.genai import types

    if not api_key:
        # The SDK raises a ValueError from three frames deep about "key inputs
        # arguments", which does not tell anyone which file to edit.
        raise RuntimeError(
            "GEMINI_API_KEY is empty. Set it in .env at the project root. "
            "If there is no .env, run: cp .env.example .env"
        )
    client = _client(api_key)
    out: list[list[float]] = []
    for start in range(0, len(texts), BATCH):
        batch = texts[start : start + BATCH]
        for attempt in range(attempts):
            try:
                resp = await client.aio.models.embed_content(
                    model=model,
                    contents=batch,
                    config=types.EmbedContentConfig(
                        task_type=task, output_dimensionality=dims
                    ),
                )
                out.extend([list(e.values) for e in resp.embeddings])
                break
            except Exception:  # noqa: BLE001
                if attempt == attempts - 1:
                    raise
                await asyncio.sleep(backoff_base**attempt)
    return out


async def embed_documents(texts, *, api_key, model, dims):
    return await embed_texts(
        texts, api_key=api_key, model=model, dims=dims, task="RETRIEVAL_DOCUMENT"
    )


async def embed_query(text, *, api_key, model, dims):
    """Embed one query during a live call.

    Ingest can afford four attempts with exponential backoff. A caller cannot:
    the first retry alone would sleep a second, which is longer than the whole
    tool budget. Two quick attempts, then fail and let the agent say so.
    """
    vecs = await embed_texts(
        [text],
        api_key=api_key,
        model=model,
        dims=dims,
        task="RETRIEVAL_QUERY",
        attempts=2,
        backoff_base=0.25,
    )
    return vecs[0]
