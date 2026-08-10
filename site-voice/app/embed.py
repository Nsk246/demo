"""Embedding calls, batched and retried.

Kept behind one function so swapping the model, or the whole provider, is a
one-file change. Document and query embeddings use different task types, which
is worth roughly a point of recall and costs nothing.
"""
from __future__ import annotations

import asyncio

BATCH = 32


async def embed_texts(
    texts: list[str], *, api_key: str, model: str, dims: int, task: str
) -> list[list[float]]:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    out: list[list[float]] = []
    for start in range(0, len(texts), BATCH):
        batch = texts[start : start + BATCH]
        for attempt in range(4):
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
                if attempt == 3:
                    raise
                await asyncio.sleep(2**attempt)
    return out


async def embed_documents(texts, *, api_key, model, dims):
    return await embed_texts(
        texts, api_key=api_key, model=model, dims=dims, task="RETRIEVAL_DOCUMENT"
    )


async def embed_query(text, *, api_key, model, dims):
    vecs = await embed_texts(
        [text], api_key=api_key, model=model, dims=dims, task="RETRIEVAL_QUERY"
    )
    return vecs[0]
