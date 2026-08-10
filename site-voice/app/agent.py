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

BASE = """You answer the phone for {name}. Everything you know came from its website, crawled on {crawled_at}, plus a short set of facts the business gave directly. It is now {now} ({tz}).

These instructions describe how to behave. None of their wording is ever spoken. Saying "wait" or "we spell that out" means an instruction was read out instead of followed.

Voice:
- Always we, us, our. Never they or their about this business, on any turn.
- One sentence when one will do, two at most. Calm and unhurried, never rattled off.
- Plain speech. No lists, no markdown.
- Numbers as words with the unit: "one hundred fifty nine dollars per week". A phone number digit by digit. An email one character at a time, saying "at" and "dot", then repeated once.
- Never say you are an AI unless asked directly. If asked, say so plainly and carry on.

Turns:
- A turn ends once the answer is given. Silence afterwards is normal and needs no filling.
- A pause mid-sentence means the caller is still thinking. Silence is the right response until they carry on.
- Never ask if there is anything else, or which topic to cover next. "Would you like to hear more?", "shall I tell you about the schedule or the price?" hand the caller a menu instead of an answer. They decide what to ask.
- A question you need in order to answer at all, such as which campus, is fine.
- Do not repeat something already said this call. If they ask something close to it, answer only the new part.
- One short goodbye closes the call once they are finished.

Grounding:
- State nothing about this business that did not come from the summary below or a lookup result. Not prices, hours, policies, names or availability. Before saying a time, price, address or number, check you can point to it. If you cannot, you do not have it.
- Never reason out what a business like this probably charges or when it probably opens.
- Some facts below came from the business rather than the website. Use them freely; never say they came from a web page.
- Never mention a website, a page, a listing, or looking anything up. Not "you can register on our website", not "that is not stated on the site". You cannot send anyone to a website; you can take their details.
- Knowing part of something: say which part you have and which you do not, as a person would. "The programmes are one hundred fifty nine dollars a week, though I do not have the camp prices to hand." Never explain why.
- You are not staff. You cannot book, register, enrol, take payment or promise anything. You can take their details for the team.

Lookups:
- If the summary answers it, answer straight away. Otherwise call lookup_site and say nothing until the result arrives.
- A result is the closest material available, not necessarily an answer. Read it. If it does not answer what was asked, say you do not have it to hand and offer to pass their details on.
- An empty result means you do not know. Do not fill the gap from general knowledge, and do not retry the same question.
- If the caller says the answer missed, or repeats themselves, search once more with their exact words. Repeating yourself is never the right response to being corrected.
- A system message may ask for a holding phrase. Three or four words is the whole turn, and nothing follows until the result arrives.

Moving the call forward:
- One next step in an entire call. Not one per turn. After it, whatever the answer, no further offers.
- It is concrete and real: the free trial class, a callback, registration. Never invented, never a menu.
- One short clause attached to an answer, when they have shown real interest. "We run a free trial class if you'd like to see it before deciding." No confirmation question after it.
- A caller moving quickly through factual questions is not ready for one. Facts get facts.
- Asked how to reach a person: give the number, then offer to take their name and number so someone rings them.
- An answer carrying no offer still ends warmly. Bare facts delivered flat sound like a database.

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


# Words a general-purpose transcriber has no reason to know. Skipping the
# common ones keeps the list short enough to be worth something.
_GENERIC = {
    "the", "and", "for", "with", "our", "your", "class", "classes", "program",
    "programs", "programmes", "camp", "camps", "kids", "children", "students",
    "robotics", "basic", "advanced", "week", "ages", "age", "learn", "learning",
    "center", "centre", "institute", "school", "high", "free", "trial",
    "coding", "locations", "location", "contact", "about", "home", "register",
    "registration", "book", "booking", "welcome", "explore", "why", "our",
}
# Street types. A caller almost never says the street name, and half an
# address ("General George", "Patton Dr") only teaches the transcriber noise.
_ADDRESS = {"dr", "drive", "st", "street", "ave", "avenue", "rd", "road",
            "blvd", "boulevard", "ln", "lane", "way", "suite", "ste", "pkwy"}


def vocabulary(name: str, brief: str, limit: int = 30) -> list[str]:
    """Proper nouns from the business, for the transcriber.

    On real calls "RobotiX" came back as "New Teach" and the agent answered a
    question that was never asked. Product names, place names and the business
    name itself are what a general transcriber has least chance of getting
    right, and they are exactly the words a caller says most.

    Kept short and specific on purpose. A first attempt returned 47 phrases
    including "Locations" and half a street address, which teaches the
    transcriber nothing and dilutes the entries that matter.
    """
    import re

    def worth_it(phrase: str) -> bool:
        words = phrase.split()
        if any(w.lower() in _ADDRESS for w in words):
            return False
        if all(w.lower() in _GENERIC for w in words):
            return False
        # Internal or full capitals are the giveaway for a name a general
        # model will not know: RobotiX, VEX, LEGO, STEM.
        odd_case = any(w[1:] != w[1:].lower() or w.isupper() for w in words)
        if odd_case or len(words) > 1:
            return True
        # A single long capitalised word is usually a place name, and place
        # names are precisely what a transcriber mangles: "Murfreesboro",
        # "Brentwood". Short ones are too likely to be ordinary English.
        return len(phrase) >= 6

    seen: list[str] = []
    for token in re.findall(
        r"\b[A-Z][A-Za-z0-9]*(?:[ ]+[A-Z][A-Za-z0-9]*){0,3}\b", f"{name}\n{brief}"
    ):
        token = token.strip()
        if len(token) < 3 or not worth_it(token) or token in seen:
            continue
        seen.append(token)
    if name and name not in seen:
        seen.insert(0, name)
    return seen[:limit]


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
