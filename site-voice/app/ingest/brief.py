"""Generate the site brief that goes into the system instruction.

This is the single highest-leverage artifact in the demo. It decides how many
questions get answered in one round trip instead of two. It is generated once
at ingest time by a text model reading the crawl, so its cost never touches a
live call.
"""
from __future__ import annotations

import json

BRIEF_PROMPT = """You are preparing a briefing for a phone agent that will answer
calls for this business. Below is text crawled from their website.

Write a factual summary the agent can rely on. Rules:

- Only include what is actually stated in the pages. Invent nothing.
- Lead with what the business does, in one sentence a caller would recognise.
- Then cover, only where the site states them: locations and addresses, opening
  hours, phone and email, what they sell or offer, price points, who they serve,
  and anything a first-time caller obviously asks.
- Use short plain lines. No marketing language. No headings beyond simple labels.
- Stay under 400 words. Leave out anything a caller would never ask.
- If the site does not state hours, prices, or a phone number, say so explicitly
  in one line so the agent knows not to guess.

Then write one opening line for the agent to say when answering the phone. It
should name the business, be under 12 words, and sound like a person.

Return JSON only, no code fence:
{{"name": "...", "brief": "...", "greeting": "..."}}

PAGES:
{pages}
"""


def _corpus(docs, limit_chars: int = 60000) -> str:
    """Front-load the pages a caller is most likely to ask about."""
    priority = ("about", "contact", "pricing", "price", "plans", "services",
                "hours", "location", "faq", "menu", "products")

    def rank(doc):
        path = doc.url.lower()
        hit = next((i for i, k in enumerate(priority) if k in path), len(priority))
        depth = path.count("/")
        return (hit, depth, -doc.words)

    parts, total = [], 0
    for doc in sorted(docs, key=rank):
        block = f"\n=== {doc.url}\n{doc.title}\n{doc.text[:4000]}\n"
        if total + len(block) > limit_chars:
            continue
        parts.append(block)
        total += len(block)
    return "".join(parts)


async def generate_brief(docs, *, api_key: str, model: str) -> dict:
    from google import genai

    client = genai.Client(api_key=api_key)
    prompt = BRIEF_PROMPT.format(pages=_corpus(docs))
    resp = await client.aio.models.generate_content(model=model, contents=prompt)
    text = (resp.text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"name": "", "brief": text[:4000], "greeting": ""}
    return {
        "name": str(data.get("name", ""))[:120],
        "brief": str(data.get("brief", ""))[:6000],
        "greeting": str(data.get("greeting", ""))[:200],
    }
