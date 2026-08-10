"""System instruction, tool schemas, and tool dispatch.

Two decisions carry over from the restaurant build.

The site brief goes in the system instruction rather than behind a tool. That
is the same reasoning as the compact menu render: a tool call is a second full
model round trip, and the measured round trip is the dominant term in every
turn. Most callers ask what the business does, where it is, when it is open,
and what it costs. Those belong in the prompt.

The prompt is written to be spoken. The model is on a phone at 8 kHz, so the
rules are mostly brevity, confirmation, and never guessing.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

TOOL_SCHEMAS = [
    {
        "name": "lookup_site",
        "description": (
            "Search the company website for anything not covered in the summary "
            "you were given. Use it for specific questions about products, "
            "policies, people, documentation, or details. Use it rather than "
            "guessing, always."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "The caller's question as a self-contained sentence. "
                        "Resolve pronouns first: if they said 'how much is it', "
                        "send 'how much does the pro plan cost'."
                    ),
                }
            },
            "required": ["question"],
        },
    },
    {
        "name": "take_message",
        "description": (
            "Record a message for the team. Use when the website does not "
            "answer the question, when the caller asks for a person, or when "
            "they want a callback."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "caller_name": {"type": "string"},
                "contact": {
                    "type": "string",
                    "description": "Phone number or email, read back before sending.",
                },
                "message": {"type": "string"},
            },
            "required": ["message"],
        },
    },
]

BASE = """You answer the phone for {name}. Everything you know about this business came from its website, crawled on {crawled_at}, plus a short set of facts the business gave directly. It is now {now} ({tz}).

How to speak:
- Two sentences maximum. This is a phone call, not an email.
- Plain spoken language. No lists, no markdown, no headings.
- Say numbers as words. Spell out email addresses and phone numbers slowly and read them back.
- Match the caller's pace. If they are brisk, be brisk.
- Never say you are an AI unless asked directly. If asked, say so plainly and carry on.

What you must never do:
- Never state a fact about this business that did not come from the summary below or from a lookup_site result. Not prices, not hours, not policies, not names, not availability. A wrong answer about someone's own business is worse than no answer.
- Some facts below are marked as provided by the business rather than taken from the website. Use them freely, but never say they came from a web page.
- You are not staff. You cannot book, take payment, or promise anything on their behalf. You answer questions and take messages.

Answering a question:
- If the summary below answers it, answer straight away.
- Otherwise call lookup_site. Do not announce it and do not narrate what you are doing.
- A lookup returns the closest text on the site, which is not the same as an answer. Read what comes back before using it. If it does not actually answer what was asked, say the site does not cover it and offer to take a message. Never estimate a price, a time, or a policy from a related passage.
- If lookup_site returns found false, say plainly that it is not on the site, then offer to take a message. Do not rephrase and try the same question twice.
- When an answer came from a lookup you may say where in a short clause, for example "that's on their pricing page". Never read a URL aloud unless asked.
- While a tool is running you may be asked to stall. Say three or four words, then stop and wait. Do not fill the gap with chatter.

Closing:
- Before ending, ask if there is anything else. If they are done, thank them and stop.

WHAT THIS BUSINESS IS:
{brief}{facts}
"""


def build(
    *, name: str, brief: str, crawled_at: str, tz: str = "America/Chicago",
    facts: str = "",
) -> str:
    """Assemble the system instruction.

    The current date and time are injected because without them the model
    answers "are you open tomorrow" with a confident guess about what day it
    is. Same reason the restaurant agent gets the restaurant's local time.
    """
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone, tz = ZoneInfo("UTC"), "UTC"
    now = datetime.now(zone).strftime("%A %d %B %Y, %I:%M %p").replace(" 0", " ")
    from .facts import for_prompt

    return BASE.format(
        name=name or "this business",
        brief=brief.strip() or "No summary was generated. Use lookup_site for everything.",
        facts=for_prompt(facts),
        crawled_at=crawled_at,
        now=now,
        tz=tz,
    )


def greeting(name: str) -> str:
    """The nudge the bridge sends on `start`.

    On an inbound call the agent speaks first. Phrased as a stage direction,
    not as dialogue, because the model will read dialogue out verbatim.
    """
    return (
        f"(The call has just connected. Greet the caller now, in one short "
        f"sentence, as {name or 'this business'}.)"
    )


class ToolDispatcher:
    """Executes tool calls. Returns a plain dict for the provider to send back."""

    def __init__(self, *, index, settings, on_sources=None):
        self.index = index
        self.settings = settings
        self.on_sources = on_sources
        self.messages: list[dict] = []
        self.lookups: list[dict] = []

    async def dispatch(self, name: str, args: dict) -> dict:
        if name == "lookup_site":
            return await self._lookup(args.get("question", ""))
        if name == "take_message":
            return self._message(args)
        return {"error": f"unknown tool {name}"}

    async def _lookup(self, question: str) -> dict:
        from .embed import embed_query
        from .retrieval import render

        question = (question or "").strip()
        if not question:
            return render([])
        started = time.monotonic()
        try:
            vec = await embed_query(
                question,
                api_key=self.settings.gemini_api_key,
                model=self.settings.embedding_model,
                dims=self.settings.embedding_dims,
            )
        except Exception:
            # A failed lookup must never become an invented answer.
            return {
                "found": False,
                "passages": [],
                "hint": "The search failed. Apologise briefly and offer to take a message.",
            }
        hits = self.index.search(
            vec,
            top_k=self.settings.top_k,
            min_score=self.settings.min_score,
            min_z=self.settings.min_z,
        )
        stats = self.index.last_stats
        log.info(
            "lookup %r -> %d hits | top %.3f | mean %.3f | min_score %.2f "
            "min_z %.1f | index %d chunks, %d dims | %.0fms",
            question,
            len(hits),
            stats.get("top", 0.0),
            stats.get("mean", 0.0),
            self.settings.min_score,
            self.settings.min_z,
            self.index.size,
            self.index.dims,
            (time.monotonic() - started) * 1000,
        )
        self.lookups.append(
            {"question": question, "hits": len(hits),
             "top": round(hits[0].score, 3) if hits else None}
        )
        if hits and self.on_sources:
            self.on_sources([h.url for h in hits])
        return render(hits)

    def _message(self, args: dict) -> dict:
        self.messages.append(
            {
                "caller_name": args.get("caller_name", ""),
                "contact": args.get("contact", ""),
                "message": args.get("message", ""),
            }
        )
        return {"saved": True, "hint": "Read the message back in one sentence."}
