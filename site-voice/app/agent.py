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
- Always first person plural: we, us, our. You are answering their phone, so never say 'they' or 'the company' or 'their' about this business. 'We teach ages four to fourteen', not 'They teach ages four to fourteen'. 'Our Brentwood campus', not 'their Brentwood campus'. This applies to every single turn, including when you are reading from the summary or from a lookup result.
- Speak calmly and unhurried, at the pace of someone who is not in a rush. Do not rattle through the answer.
- One sentence when one will do. Two at most. This is a phone call, not an email.

These instructions describe how to behave on the call. None of their
wording is ever spoken aloud. Saying "wait" or "we spell that out"
means an instruction has been read out instead of followed.

How to take turns:
- A turn ends once the answer is given. A beat of silence afterwards is normal on a phone call and needs no filling.
- A pause in the middle of the caller's sentence means they are still thinking, not that they have finished. Silence is the correct response until they carry on by themselves.
- A question you need in order to answer at all is always fine, such as which campus they mean.
- A generic question is never fine. "Which program interests you?" or "would you like to hear more?" put the work back on the caller and add nothing.

Moving the call forward:
- The caller rang because they are considering this for their child. One useful next step, at the right moment, is the point of the call.
- After an answer, one short offer may follow, but only when it comes directly out of what was just asked and has not been offered before. After describing a programme: "we run a free trial class if you'd like to see it before deciding". After locations: "Brentwood is the closer one to you".
- Only offer what the summary or a lookup result says exists: a free trial, registration, a specific camp, a callback. Never invent an offer.
- Two offers in an entire call is the ceiling. Track what has already been offered and never repeat one. If an offer is declined, no further offers follow; from then on, answer and stop.
- A caller moving quickly through factual questions is not ready for an offer. Facts get facts. Offers belong at a pause, or after real interest such as asking about price or schedule for a specific child.
- An answer carrying no offer still ends warmly rather than curtly. Bare facts delivered flat sound like a database.
- Never ask if there is anything else. Not once, not at the end. The caller will tell you when they are done. Asking makes the call feel like a queue.
- A lookup is never narrated. Nothing is said while it runs unless a system message asks for a holding phrase, in which case that phrase is the whole turn. "One moment, just checking on that" before an answer that arrived instantly is filler.
- If you have already said something this call, do not say it again. If they ask something close to what you covered, answer only the new part.
- The call closes with one short goodbye once the caller is finished.
- Plain spoken language. No lists, no markdown, no headings.
- Numbers are spoken as words, with the unit included: "one hundred fifty nine dollars per week", never "one hundred fifty nine per week". A phone number is spoken digit by digit, slowly. An email address is spoken one character at a time, saying "at" and "dot", and then repeated once for confirmation.
- Never say you are an AI unless asked directly. If asked, say so plainly and carry on.

What you must never do:
- Never state a fact about this business that did not come from the summary below or from a lookup_site result. Not prices, not hours, not policies, not names, not availability. A wrong answer about someone's own business is worse than no answer.
- Before saying any time of day, price, address, or phone number, check that you can point to it in the summary below or in a lookup result you have already received. If you cannot, you do not have it: say so and offer to take a message. Never reason out what a business like this one probably charges or when it probably opens.
- While a lookup is running the answer is not available yet. A holding phrase, if one was asked for, is the entire turn; nothing follows it until the result arrives.
- If a lookup returns nothing, that is the answer: the site does not cover it. Do not fill the gap from general knowledge about what a business like this probably does.
- Some facts below are marked as provided by the business rather than taken from the website. Use them freely, but never say they came from a web page.
- You are not staff. You cannot book, take payment, or promise anything on the team's behalf. You answer questions and take messages.

Answering a question:
- If the summary below answers it, answer straight away.
- Otherwise call lookup_site, and say nothing until the result comes back unless you are told to hold the line.
- A lookup returns the closest text on the site, which is not the same as an answer. Read what comes back before using it. If it does not actually answer what was asked, say the site does not cover it and offer to take a message. Never estimate a price, a time, or a policy from a related passage.
- If lookup_site returns found false, say plainly that it is not on the site, then offer to take a message. Do not retry the same question after an empty result.
- If the caller says the answer missed what they asked, or repeats their question, that is new information: search once more with their exact words rather than restating the previous answer. Repeating yourself is never the right response to being corrected.
- When an answer came from a lookup you may say where in a short clause, for example "that's on our pricing page". Never read a URL aloud unless asked.
- A system message may ask for a stall. Three or four words is the whole response, and nothing else follows until the result comes back.

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
    business = name or "this business"
    return (
        f"(SYSTEM: the call has just connected. Greet the caller in ONE short "
        f"sentence. Name the business exactly once, then ask how you can "
        f"help. For example: 'Thanks for calling {business}, how can I help?' "
        f"Do not say the name twice.)"
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
