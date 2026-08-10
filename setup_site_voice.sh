#!/usr/bin/env bash
# site-voice: a phone agent that answers only from a crawled website.
# Creates ./site-voice. Run from the parent dir:  bash setup_site_voice.sh
set -euo pipefail
mkdir -p site-voice && cd site-voice
mkdir -p .devcontainer app app/ingest app/providers app/telephony scripts tests data

cat > .devcontainer/devcontainer.json << 'SVEOF0'
{
  "name": "site-voice",
  "image": "mcr.microsoft.com/devcontainers/python:3.12",
  "postCreateCommand": "pip install -r requirements.txt && cp -n .env.example .env || true",
  "forwardPorts": [8000],
  "portsAttributes": { "8000": { "label": "voice", "visibility": "public" } },
  "customizations": {
    "vscode": { "extensions": ["ms-python.python", "charliermarsh.ruff"] }
  }
}
SVEOF0

cat > .env.example << 'SVEOF1'
# ---- core ----
APP_ENV=dev
# Public host Twilio reaches, no scheme. On Railway this is filled in from
# RAILWAY_PUBLIC_DOMAIN automatically, so you can leave it blank there.
PUBLIC_BASE_URL=

# ---- telephony ----
# Same number as the restaurant build. scripts/webhook.py backs up the
# existing webhook before it swaps, and restores it afterwards.
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_NUMBER=+1
# Never turn this off in prod. The stream URL is public.
TWILIO_VALIDATE_SIGNATURE=true

# ---- realtime speech model ----
REALTIME_PROVIDER=gemini
GEMINI_API_KEY=
# Gemini 2.0 Flash was retired March 2026. Model ids churn; check
# https://ai.google.dev/gemini-api/docs/models if a session fails to open.
GEMINI_LIVE_MODEL=gemini-3.1-flash-live-preview
# minimal | low | medium | high. Lower means faster first audio.
GEMINI_THINKING_LEVEL=minimal
# Silence before the model treats the caller's turn as finished. Added to
# every turn, so usually the largest single term in perceived latency.
GEMINI_END_OF_SPEECH_MS=500
GEMINI_VOICE=Aoede
# Cheap text model, used once at ingest to write the site brief.
GEMINI_TEXT_MODEL=gemini-2.5-flash

# ---- retrieval ----
EMBEDDING_MODEL=gemini-embedding-001
EMBEDDING_DIMS=768
TOP_K=4
# The dial that matters. Too low and the agent answers off-topic questions
# with whatever chunk scored highest, confidently and wrong. Too high and it
# says "not on the site" for things that are. Tune with scripts/eval.py.
MIN_SCORE=0.55

# ---- the crawled site ----
SITE_DB=data/site.db
SITE_TIMEZONE=America/Chicago
MAX_PAGES=120
MAX_DEPTH=3
SVEOF1

cat > .gitignore << 'SVEOF2'
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
.venv/
.env
# Built by ingest, never by git. A committed db silently deploys an old crawl.
# data/pages/ is different: it is a build input, committed on purpose when the
# crawl has to happen on a machine that can reach the site.
data/*.db
data/*.db-wal
data/*.db-shm
.webhook-backup.json
SVEOF2

cat > Dockerfile << 'SVEOF3'
FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY data ./data

EXPOSE 8000
# Long-lived WebSockets: the ping settings keep Twilio's media stream from
# being reaped mid-call by an idle timeout.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--ws-ping-interval", "20", "--ws-ping-timeout", "20"]
SVEOF3

cat > Makefile << 'SVEOF4'
.PHONY: install test check run ingest eval point restore

install:
	pip install -r requirements.txt

test:
	python -m pytest tests -q

check:
	python scripts/check.py

run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload \
	  --ws-ping-interval 20 --ws-ping-timeout 20

ingest:
	python -m app.ingest $(URL)

# Crawl here, embed elsewhere. For sites that block this machine's network.
archive:
	python -m app.ingest $(URL) --crawl-only --save-html data/pages

from-archive:
	python -m app.ingest --from-dir data/pages

netcheck:
	python scripts/netcheck.py $(URL)

eval:
	python scripts/eval.py

point:
	python scripts/webhook.py point $(URL)

restore:
	python scripts/webhook.py restore
SVEOF4

cat > README.md << 'SVEOF5'
# site-voice

A phone number that answers and can only tell you what is actually on a
website. Crawl a site, call the number, ask it questions.

Standalone demo. It shares no database and no repo with the restaurant
platform. The audio codec, provider adapters, and media bridge are copied from
it, because those took real calls to get right and there is no reason to
rediscover any of it.

## What it does

1. `python -m app.ingest https://example.com` crawls the site, strips the
   boilerplate, chunks what is left, embeds it, and writes a summary of the
   business.
2. The summary goes into the system instruction. The chunks sit behind a
   `lookup_site` tool.
3. A caller dials. Gemini Live answers. Common questions come back in one round
   trip from the summary. Anything specific triggers a lookup.
4. The screen at `/` shows the transcript live, every page the agent cited, and
   the per-turn latency.

## Why the summary is in the prompt rather than behind the tool

Same reasoning as the compact menu render in the restaurant build. Measured
there: about 1.6 seconds of model round trip, effectively zero transport. A
tool call is a second full round trip. Most callers ask what the business does,
where it is, when it is open, and what it costs, so those go in the prompt and
the tool covers the rest.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # GEMINI_API_KEY and the Twilio values
make test
make check
```

## Ingest a site

```bash
python -m app.ingest https://example.com
python scripts/eval.py
```

`eval.py` runs caller-shaped questions through retrieval only: no phone, no
live model. Every MISS is either a gap in the crawl or a question the site
genuinely does not answer. Sort that out there, not on a call.

## The Twilio number

Shared with the restaurant build, so every swap is recorded before it happens.

```bash
python scripts/webhook.py status
python scripts/webhook.py point https://site-voice-production.up.railway.app
python scripts/webhook.py restore
```

`point` writes the previous webhook to `.webhook-backup.json` the first time it
runs and refuses to overwrite it, so running it twice cannot lose the
restaurant URL. Keep that file.

## Deploy

Railway, Dockerfile builder, US East, paid tier. `PUBLIC_BASE_URL` can stay
empty because `RAILWAY_PUBLIC_DOMAIN` is picked up automatically.

The SQLite file ships inside the image, so re-ingest locally and push to update
the crawl. `data/*.db` is gitignored to stop a stale one being committed by
accident, which means you have to force-add it deliberately when you do want to
ship one.

Not Codespaces: the forwarded-port relay returns 404 on the WebSocket upgrade
and Twilio reports error 31920. Not Fly's trial: machines stop after five
minutes. Both have cost a demo already.

## What was copied, and what is new

Copied unchanged from `restaurant-ai`:

- `app/audio.py`, including the 14-bit `abs(s >> 2) << 2` domain in the encode
  table. A naive `abs()` disagrees with the reference codec on exactly 381
  values, all of them negative, which is distortion on loud audio.
- `app/providers/base.py`, `gemini.py`, `mock.py`. The Gemini adapter reads
  audio off `response.data`; walking `server_content.model_turn.parts` instead
  gives a session that transcribes correctly and plays nothing.
- `app/telephony/bridge.py` and `twilio_webhook.py`. The outbound pump does not
  sleep between frames. That looks like correct real-time pacing and is not:
  `asyncio.sleep` overshoots every iteration, a long reply drifts hundreds of
  milliseconds late, and the backlog compounds across turns. Twilio buffers
  inbound media itself and `clear` is what makes barge-in work.

New here: the crawl and retrieval layer (`app/ingest/`, `store.py`, `embed.py`,
`retrieval.py`), the prompt and tools (`app/agent.py`), the screen, and
`scripts/eval.py`.

## Tuning

`MIN_SCORE` is the dial. Too low and the agent answers unrelated questions with
whatever chunk scored highest. Too high and it says "not on the site" for
things that are. Start at 0.55 and use `eval.py`.

`GEMINI_END_OF_SPEECH_MS` is the latency lever, same as the restaurant build.
Below roughly 350ms it starts cutting off people who pause mid-sentence.

`MAX_PAGES` is a time and cost limit, not a quality one. A 40-page marketing
site is fully covered at 60. Documentation sites need more and are better
served by pointing the crawler at the docs subdirectory.

## Layout

```
app/
  audio.py            mu-law codec and PSTN resampling, no audioop
  agent.py            prompt, tool schemas, tool dispatch
  retrieval.py        in-memory cosine search
  embed.py            embedding calls
  store.py            SQLite schema
  config.py           settings, .env resolved by walking ancestors
  main.py             webhook, media stream, monitor socket
  screen.html         live transcript, cited sources, latency
  providers/          base contract, Gemini Live adapter, mock
  telephony/          media bridge, Twilio webhook helpers
  ingest/             crawl, extract, chunk, brief, CLI
scripts/
  check.py            AST checks for the bugs that have cost hours
  webhook.py          reversible Twilio number swap
  eval.py             offline retrieval scoring
```

## Notes for the RobotiX Institute demo

The site is `https://www.rxiedu.com/`, a robotics school with centres in
Brentwood and Murfreesboro. Two things about it shape the setup.

**It never states class days or times.** Every programme page says "See Details
for Schedule" and the detail page gives a four-week curriculum outline, not a
timetable. There are no opening hours either. Those are the first things a
parent asks, so without help the demo's opening exchange is the agent correctly
refusing to answer. Fill in `data/facts.md` before the demo:

```bash
python -m app.ingest --write-facts-template
```

The template is pre-filled with plausible values as a placeholder. Replace them
with real ones or delete the section, because an agent confidently reading out
invented class times to the person who owns the school is the worst possible
outcome here.

**Its pages repeat a lot inside the content area.** The testimonial carousel,
the FAQ accordion, and the "similar programs" price cards appear on nearly
every page. Left in, asking the price of Python Coding retrieves the LEGO page,
because the LEGO page carries a Python card. `app/ingest/boilerplate.py` drops
any line appearing on half the pages or more, and `eval.py` warns when the top
hits cluster within 0.02 of each other, which is what surviving chrome looks
like.

`questions.txt` is written against this site. The last three entries are
deliberately unrelated to it: if any of them match, `MIN_SCORE` is too low.

## When a crawl fetches nothing

The ingest failure output is the same eight lines whether it worked or not, so
read them rather than guessing.

`ConnectTimeout` with the root never resolving to a connection has three
causes and they need different responses. Run:

```bash
python scripts/netcheck.py https://www.rxiedu.com/
```

- **IPv4 works, the default socket does not.** The container has an IPv6
  address and no route behind it. httpx has no Happy Eyeballs, so it picks the
  AAAA record and hangs, while curl in the same shell falls back in
  milliseconds and makes the site look fine. The crawler already retries on a
  forced IPv4 socket and says so in the report.
- **Both fail and the raw TCP connect to an A record times out.** The site
  drops traffic from this network. This is what rxiedu.com does to Codespaces:
  it is hosted on Hostinger, which null-routes cloud IP ranges, and Codespaces
  is Azure. Nothing HTTP-level is happening, so no user agent or proxy header
  changes it. Split the crawl from the embedding, below.
- **No A records.** The hostname is wrong.

## Crawling on one machine, embedding on another

Ingest was always an offline build step: the SQLite file ships inside the
image and Railway never crawls anything. So when the site is unreachable from
the machine you develop on, move only the crawl.

On a laptop, on a normal home connection. Copy across `scripts/offline_crawl.py`
by itself. It uses only the standard library, so there is nothing to install,
no virtualenv, no git, and no API key:

```bash
python3 offline_crawl.py https://www.rxiedu.com/
```

It writes `pages/` and zips it. Drag that zip into the Codespaces file
explorer, then:

```bash
unzip -o pages.zip -d data/
python -m app.ingest --from-dir data/pages
python scripts/eval.py
```

If the laptop does have the project checked out, `python -m app.ingest <url>
--crawl-only --save-html data/pages` does the same thing through the normal
crawler. Both write the same format; a test asserts they agree on filenames.

`data/pages/` is committed on purpose. It is a build input, unlike
`data/site.db`, which is generated and gitignored. The archive keeps a
manifest mapping each file back to its URL, so citations still point at the
real page rather than a local filename.
SVEOF5

cat > app/__init__.py << 'SVEOF6'

SVEOF6

cat > app/agent.py << 'SVEOF7'
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

from datetime import datetime
from zoneinfo import ZoneInfo

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
            vec, top_k=self.settings.top_k, min_score=self.settings.min_score
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
SVEOF7

cat > app/audio.py << 'SVEOF8'
"""G.711 mu-law codec and PSTN resampling.

Twilio Media Streams carry 8 kHz mu-law. Realtime speech models want 16 kHz
PCM16 in and emit 16 or 24 kHz PCM16 out. This module is the only place that
knows about either fact.

Why not `audioop`: it was removed from the standard library in Python 3.13.
The lookup tables here are built at import time with numpy and are verified
byte-for-byte against `audioop` in the test suite while it still exists, so
we get the same output with no deprecated dependency.
"""

from __future__ import annotations

import numpy as np
from scipy import signal

_BIAS = 0x84
_CLIP = 32635


def _build_decode_table() -> np.ndarray:
    """256 mu-law bytes -> int16 PCM."""
    u = np.arange(256, dtype=np.int32)
    v = ~u & 0xFF
    mantissa = v & 0x0F
    exponent = (v & 0x70) >> 4
    magnitude = ((mantissa << 3) + _BIAS) << exponent
    magnitude -= _BIAS
    sample = np.where(v & 0x80, -magnitude, magnitude)
    return sample.astype(np.int16)


def _build_encode_table() -> np.ndarray:
    """Full int16 range -> mu-law byte. 64K entries, built once.

    The `abs(s >> 2) << 2` is not decoration. G.711 is defined over a 14-bit
    domain, so the low two bits are discarded before the magnitude is taken.
    Because the shift is arithmetic, negative samples round away from zero,
    which is why a naive `abs(s)` disagrees with the reference codec on 381
    values, all of them negative. Verified against audioop in the tests.
    """
    s = np.arange(-32768, 32768, dtype=np.int32)
    sign = np.where(s < 0, 0x80, 0x00).astype(np.int32)
    mag = np.minimum(np.abs(s >> 2) << 2, _CLIP) + _BIAS

    # Exponent is the position of the highest set bit above bit 7.
    exponent = np.zeros_like(mag)
    for e in range(1, 8):
        exponent = np.where(mag >= (1 << (e + 7)), e, exponent)

    mantissa = (mag >> (exponent + 3)) & 0x0F
    byte = ~(sign | (exponent << 4) | mantissa) & 0xFF
    return byte.astype(np.uint8)


_DECODE = _build_decode_table()
_ENCODE = _build_encode_table()


def ulaw_to_pcm16(payload: bytes) -> np.ndarray:
    """Twilio mu-law bytes -> 8 kHz PCM16 samples."""
    return _DECODE[np.frombuffer(payload, dtype=np.uint8)]


def pcm16_to_ulaw(samples: np.ndarray) -> bytes:
    """8 kHz PCM16 samples -> Twilio mu-law bytes."""
    idx = samples.astype(np.int32) + 32768
    return _ENCODE[np.clip(idx, 0, 65535)].tobytes()


def resample(samples: np.ndarray, src_hz: int, dst_hz: int) -> np.ndarray:
    """Polyphase resample. Exact ratios only, which is all telephony needs."""
    if src_hz == dst_hz:
        return samples
    from math import gcd

    g = gcd(src_hz, dst_hz)
    out = signal.resample_poly(samples, up=dst_hz // g, down=src_hz // g)
    return np.clip(out, -32768, 32767).astype(np.int16)


def phone_to_model(payload: bytes, model_hz: int = 16000) -> bytes:
    """Inbound: Twilio mu-law 8k -> model PCM16."""
    return resample(ulaw_to_pcm16(payload), 8000, model_hz).tobytes()


def model_to_phone(pcm: bytes, model_hz: int = 24000) -> bytes:
    """Outbound: model PCM16 -> Twilio mu-law 8k."""
    samples = np.frombuffer(pcm, dtype=np.int16)
    return pcm16_to_ulaw(resample(samples, model_hz, 8000))


# Twilio expects 20 ms frames: 160 mu-law bytes at 8 kHz.
FRAME_BYTES = 160


def frames(payload: bytes, size: int = FRAME_BYTES):
    """Split into whole frames; return the frames and any trailing remainder.

    Twilio tolerates larger writes, but pacing in 20 ms frames keeps barge-in
    responsive: a `clear` lands between frames instead of after a long blob.
    """
    n = len(payload) // size
    return [payload[i * size : (i + 1) * size] for i in range(n)], payload[n * size :]
SVEOF8

cat > app/config.py << 'SVEOF9'
"""Settings, read once from the environment.

The env-file resolution and base-URL fallback are lifted from the restaurant
build. Both encode failures that were expensive to diagnose the first time.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _env_file() -> str:
    """Absolute path to the repo-root .env, if there is one.

    Not a relative ".env": that resolves against the working directory, so a
    repo-root file is silently ignored and every setting falls back to its
    default. Walk ancestors rather than indexing a fixed depth, because in the
    container the app sits at /srv/app and parents[3] raises IndexError at
    import, killing the process before it can serve anything.
    """
    here = Path(__file__).resolve()
    for base in here.parents:
        candidate = base / ".env"
        if candidate.is_file():
            return str(candidate)
    return ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_env_file(), extra="ignore")

    app_env: str = "dev"
    public_base_url: str = ""

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_validate_signature: bool = True
    twilio_number: str = ""

    realtime_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_live_model: str = "gemini-3.1-flash-live-preview"
    gemini_thinking_level: str = "minimal"
    gemini_end_of_speech_ms: int = 500
    gemini_voice: str = "Aoede"
    gemini_text_model: str = "gemini-2.5-flash"

    embedding_model: str = "gemini-embedding-001"
    embedding_dims: int = 768

    site_db: str = "data/site.db"
    # Answers the website does not contain. See app/facts.py.
    facts_file: str = "data/facts.md"
    site_timezone: str = "America/Chicago"

    # Retrieval. MIN_SCORE is the dial that decides whether the agent answers
    # off-topic questions with whatever chunk scored highest.
    top_k: int = 4
    min_score: float = 0.55

    # A lookup is an embed call plus a matrix multiply. The embed call is the
    # whole cost, and it is well under the bridge's 450ms stall threshold on a
    # good connection, so most lookups never trigger a holding phrase.
    tool_timeout_ms: int = 2500
    max_call_seconds: int = 600

    max_pages: int = 120
    max_depth: int = 3
    crawl_timeout_s: float = 15.0
    crawl_concurrency: int = 6


def resolve_base_url(configured: str, env: dict[str, str]) -> str:
    """Normalise the public hostname, falling back to the platform's own.

    Split out from get_settings so it can be tested as a pure function.
    Testing it through Settings makes the result depend on whether the
    developer running the suite happens to have a .env, which is the kind of
    environment-dependent test that passes for me and fails for you.
    """
    url = configured
    if not url:
        for var in ("RAILWAY_PUBLIC_DOMAIN", "FLY_APP_NAME"):
            value = (env.get(var) or "").strip()
            if value:
                url = value if "." in value else f"{value}.fly.dev"
                break
    return url.replace("https://", "").replace("http://", "").strip("/")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.public_base_url = resolve_base_url(
        settings.public_base_url, dict(os.environ)
    )
    return settings


def log_config_source() -> str:
    """Which .env was read, and whether it existed.

    An env file that is silently not found looks identical to one with every
    value left at its default, which is how a real call ends up answered by
    the mock provider.
    """
    path = Path(Settings.model_config["env_file"])
    return f"{path} ({'found' if path.is_file() else 'MISSING'})"
SVEOF9

cat > app/embed.py << 'SVEOF10'
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
SVEOF10

cat > app/facts.py << 'SVEOF11'
"""Facts the business gave us that are not on their website.

Every marketing site omits something a caller asks about on the first turn.
For a school it is almost always class days and times. The agent is right to
refuse to guess, but a demo whose first answer is "that isn't on the site" is
a demo that sells nothing.

So there is one plain-text file the business can fill in. Its contents go into
the brief and into the index, labelled as coming from the business rather than
the website, so the agent never claims a fact is "on their pricing page" when
it came from here.

Format is markdown. Lines starting with `## ` become section headings, which
is the same shape the chunker already understands.
"""

from __future__ import annotations

from pathlib import Path

HEADER = (
    "FACTS THE BUSINESS PROVIDED DIRECTLY. These are not on the website. They "
    "are as reliable as anything below, but never say they came from a web "
    "page."
)

SOURCE = "business:facts"


def load(path: str | Path) -> str:
    path = Path(path)
    if not path.is_file():
        return ""
    text = path.read_text().strip()
    # A file of nothing but comments is an empty file.
    body = "\n".join(
        line for line in text.split("\n") if not line.strip().startswith("<!--")
    ).strip()
    return body


def for_prompt(body: str) -> str:
    return f"\n\n{HEADER}\n{body}\n" if body else ""


TEMPLATE = """<!-- Anything a caller asks that the website does not answer.
     Delete a section rather than leaving it blank: an empty heading tells
     the agent the topic exists and gives it nothing to say. -->

## Class days and times
Brentwood: Saturdays 10:00 AM and 11:30 AM, Sundays 2:00 PM.
Murfreesboro: Saturdays 1:00 PM.

## Opening hours for phone and walk-ins
Monday to Friday, 4:00 PM to 7:00 PM. Saturday 9:00 AM to 4:00 PM. Closed Sunday.

## Enrolment
Students can join any week. Billing runs in four-week blocks from the start date.

## Makeup classes
One makeup class per four-week block, subject to space at the same centre.
"""
SVEOF11

cat > app/ingest/__init__.py << 'SVEOF12'

SVEOF12

cat > app/ingest/__main__.py << 'SVEOF13'
"""Ingest CLI.

    python -m app.ingest https://example.com

Crawls, extracts, chunks, embeds, writes the brief, and replaces the contents
of the SQLite file. Idempotent: run it again and the old site is gone.
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from datetime import datetime, timezone

from ..embed import embed_documents
from ..config import get_settings
from ..store import connect, pack, save_site
from ..facts import SOURCE as FACTS_SOURCE
from ..facts import load as load_facts
from . import archive
from .boilerplate import strip_repeats
from .brief import generate_brief
from .chunk import chunk_doc
from .crawl import crawl, normalise
from .extract import extract

WALL_HINTS = ("client challenge", "just a moment", "attention required",
              "access denied", "enable javascript", "cloudflare")


async def run(
    root: str,
    *,
    max_pages: int,
    max_depth: int,
    db: str | None,
    save_html: str | None = None,
    from_dir: str | None = None,
    crawl_only: bool = False,
) -> int:
    settings = get_settings()
    if not settings.gemini_api_key and not crawl_only:
        print("GEMINI_API_KEY is not set. Check your .env.", file=sys.stderr)
        return 1

    if from_dir:
        pages = archive.load(from_dir)
        print(f"loaded {len(pages)} archived pages from {from_dir}")
        if not pages:
            print(f"{from_dir} holds no pages.", file=sys.stderr)
            return 1
        report = None
    else:
        pages, report = await _crawl(root, max_pages, max_depth, settings)
        if pages is None:
            return 1
        if save_html:
            archive.save(pages, save_html)
            print(f"archived {len(pages)} pages to {save_html}")
            if crawl_only:
                print("crawl only, stopping here. Commit that directory, then "
                      "run: python -m app.ingest --from-dir " + save_html)
                return 0

    docs, walled, thin = [], 0, 0
    for page in pages:
        doc = extract(page.url, page.html)
        if any(h in doc.title.lower() for h in WALL_HINTS) or (
            doc.words == 0 and len(page.html) > 500
        ):
            walled += 1
            continue
        if doc.words < 25:
            thin += 1
            continue
        docs.append(doc)

    docs, dropped = strip_repeats(docs)
    print(f"\n{len(docs)} usable pages, {thin} too thin, {walled} blocked by a bot wall")
    if dropped:
        print(f"{dropped} repeated lines removed as cross-page chrome "
              f"(testimonials, shared FAQ, product cards)")
    if walled > len(pages) // 3:
        print("WARNING: most pages were blocked. The brief will be thin and the "
              "demo will sound vague. Try a different site or ask for access.")
    if not docs:
        return 1

    chunks = []
    for doc in docs:
        chunks.extend(chunk_doc(doc.url, doc.title, doc.text))

    # Facts the business supplied are indexed too, so lookup_site can find
    # them for questions the summary is too short to cover.
    facts = load_facts(settings.facts_file)
    if facts:
        fact_chunks = chunk_doc(FACTS_SOURCE, "Provided by the business", facts)
        chunks.extend(fact_chunks)
        print(f"{len(fact_chunks)} chunks from {settings.facts_file} "
              f"(facts the website does not contain)")
    else:
        print(f"no {settings.facts_file}. Anything the website omits will get "
              f"'that is not on the site', which is correct but makes for a "
              f"thin demo. Run: python -m app.ingest --write-facts-template")
    print(f"{len(chunks)} chunks, {sum(len(c.text) for c in chunks) // 1000}k chars")
    if report is not None and report.failures:
        print("fetch failures: " + ", ".join(
            f"{k}={v}" for k, v in sorted(report.failures.items())))

    print("writing brief...")
    meta = await generate_brief(
        docs, api_key=settings.gemini_api_key, model=settings.gemini_text_model
    )
    print(f"  name: {meta['name']}")
    print(f"  greeting: {meta['greeting']}")
    print(f"  brief: {len(meta['brief'].split())} words")

    print(f"embedding {len(chunks)} chunks with {settings.embedding_model}...")
    vectors = await embed_documents(
        [f"{c.title}\n{c.heading}\n{c.text}" for c in chunks],
        api_key=settings.gemini_api_key,
        model=settings.embedding_model,
        dims=settings.embedding_dims,
    )

    conn = connect(db or settings.site_db)
    conn.execute("DELETE FROM chunks")
    conn.execute("DELETE FROM pages")
    for doc in docs:
        conn.execute(
            "INSERT OR REPLACE INTO pages (url,title,description,words,text)"
            " VALUES (?,?,?,?,?)",
            (doc.url, doc.title, doc.description, doc.words, doc.text),
        )
    if facts:
        conn.execute(
            "INSERT OR REPLACE INTO pages (url,title,description,words,text)"
            " VALUES (?,?,?,?,?)",
            (FACTS_SOURCE, "Provided by the business", "", len(facts.split()), facts),
        )
    for chunk, vec in zip(chunks, vectors):
        conn.execute(
            "INSERT INTO chunks (url,title,heading,text,embedding) VALUES (?,?,?,?,?)",
            (chunk.url, chunk.title, chunk.heading, chunk.text, pack(vec)),
        )
    conn.commit()
    save_site(
        conn,
        root_url=root,
        name=meta["name"] or root,
        brief=meta["brief"],
        greeting=meta["greeting"],
        crawled_at=datetime.now(timezone.utc).strftime("%d %B %Y"),
        page_count=len(docs),
        dims=settings.embedding_dims,
        facts=facts,
    )
    conn.close()
    print(f"\ndone. {db or settings.site_db} is ready. restart the service to load it.")
    return 0


async def _crawl(root, max_pages, max_depth, settings):
    root = normalise(root)
    print(f"crawling {root} (max {max_pages} pages, depth {max_depth})")
    pages, report = await crawl(
        root,
        max_pages=max_pages,
        max_depth=max_depth,
        timeout_s=settings.crawl_timeout_s,
        concurrency=settings.crawl_concurrency,
        on_page=lambda p, n: print(f"  [{n:3d}] {p.url}"),
    )
    if not pages:
        print("\nnothing fetched. what happened:\n", file=sys.stderr)
        print(report.render(), file=sys.stderr)
        print(
            "\nIf the raw TCP connect timed out, the site is dropping traffic "
            "from this network rather than refusing it. Run the crawl on a "
            "machine with a residential connection:\n"
            "  python -m app.ingest <url> --crawl-only --save-html data/pages\n"
            "then commit data/pages and run here:\n"
            "  python -m app.ingest --from-dir data/pages",
            file=sys.stderr,
        )
        return None, report
    if report.final_root != report.root:
        print(f"note: {report.root} redirected to {report.final_root}")
    if report.ua_fallback_used:
        print("note: the site refused a declared crawler, so a browser user "
              "agent was used instead")
    if report.ipv4_forced:
        print("note: the default socket timed out, IPv4 was forced")
    return pages, report


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.ingest")
    parser.add_argument("url", nargs="?")
    parser.add_argument(
        "--write-facts-template",
        action="store_true",
        help="write a starter facts file and exit",
    )
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--max-depth", type=int, default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument(
        "--save-html",
        metavar="DIR",
        help="archive the fetched pages so ingest can run on another machine",
    )
    parser.add_argument(
        "--from-dir",
        metavar="DIR",
        help="skip the crawl and read an archive written by --save-html",
    )
    parser.add_argument(
        "--crawl-only",
        action="store_true",
        help="stop after archiving. Needs no API key.",
    )
    args = parser.parse_args()
    settings = get_settings()
    if args.write_facts_template:
        from ..facts import TEMPLATE

        target = pathlib.Path(settings.facts_file)
        if target.exists():
            raise SystemExit(f"{target} already exists, leaving it alone")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(TEMPLATE)
        print(f"wrote {target}. Edit it, then run ingest.")
        raise SystemExit(0)
    if not args.url and not args.from_dir:
        parser.error("a url is required, or --from-dir to use an archive")
    if args.crawl_only and not args.save_html:
        parser.error("--crawl-only needs --save-html DIR")
    raise SystemExit(
        asyncio.run(
            run(
                args.url or "",
                max_pages=args.max_pages or settings.max_pages,
                max_depth=args.max_depth or settings.max_depth,
                db=args.db,
                save_html=args.save_html,
                from_dir=args.from_dir,
                crawl_only=args.crawl_only,
            )
        )
    )


if __name__ == "__main__":
    main()
SVEOF13

cat > app/ingest/archive.py << 'SVEOF14'
"""Save a crawl to disk and read it back.

Some hosts null-route datacenter IP ranges, so the machine that can reach the
site and the machine that runs the demo are not always the same one. Hostinger
does this and it is why rxiedu.com is unreachable from Codespaces.

Splitting crawl from ingest fixes that without a proxy. Crawl on a laptop,
which needs no API key at all, commit the archive, then embed anywhere.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

MANIFEST = "manifest.json"
SAFE = re.compile(r"[^a-z0-9._-]+")


def filename(url: str) -> str:
    """Readable, collision-proof, and stable across runs.

    The hash suffix matters: two different URLs can slugify to the same string
    and silently overwrite each other, which shows up much later as a page
    mysteriously missing from the index.
    """
    slug = SAFE.sub("-", url.split("://", 1)[-1].lower()).strip("-")[:80] or "page"
    digest = hashlib.sha1(url.encode()).hexdigest()[:8]
    return f"{slug}-{digest}.html"


def save(pages, directory: str | Path) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.glob("*.html"):
        old.unlink()
    manifest = []
    for page in pages:
        name = filename(page.url)
        (directory / name).write_text(page.html, encoding="utf-8")
        manifest.append({"url": page.url, "file": name, "status": page.status})
    (directory / MANIFEST).write_text(json.dumps(manifest, indent=2))
    return directory


def load(directory: str | Path):
    """Read an archive back as the same Page objects crawl returns."""
    from .crawl import Page

    directory = Path(directory)
    manifest_path = directory / MANIFEST
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"{manifest_path} not found. An archive directory needs the "
            f"manifest written by --save-html, not just loose HTML files."
        )
    pages = []
    for entry in json.loads(manifest_path.read_text()):
        target = directory / entry["file"]
        if not target.is_file():
            continue
        pages.append(
            Page(
                url=entry["url"],
                status=entry.get("status", 200),
                html=target.read_text(encoding="utf-8"),
            )
        )
    return pages
SVEOF14

cat > app/ingest/boilerplate.py << 'SVEOF15'
"""Cross-page boilerplate removal.

Per-page stripping handles nav and footer. It does not handle the blocks a
marketing site repeats inside `<main>` on every page: testimonial carousels,
a shared FAQ accordion, "similar programs" cards.

Those are poison for retrieval in two ways. They multiply into near-identical
chunks that crowd out the real answer, and because a card carries a price,
asking about one product retrieves the page of a different one.

The rule is frequency. A line appearing on most pages is chrome, whatever it
looks like. Lines unique to a page are its content.
"""

from __future__ import annotations

from collections import Counter

# A line on this fraction of pages or more is treated as chrome.
REPEAT_RATIO = 0.5
# Below this many pages the ratio is meaningless: on three pages, two hits
# looks like boilerplate and is usually just a shared sentence.
MIN_PAGES = 6
# Long lines are almost never chrome, and dropping one loses real content.
MAX_CHROME_CHARS = 400


def repeated_lines(docs, *, ratio: float = REPEAT_RATIO) -> set[str]:
    if len(docs) < MIN_PAGES:
        return set()
    counts: Counter[str] = Counter()
    for doc in docs:
        for line in {l.strip() for l in doc.text.split("\n") if l.strip()}:
            counts[line] += 1
    threshold = max(2, int(len(docs) * ratio))
    return {
        line
        for line, n in counts.items()
        if n >= threshold and len(line) <= MAX_CHROME_CHARS
    }


def strip_repeats(docs, *, ratio: float = REPEAT_RATIO):
    """Remove cross-page chrome in place and report what went.

    Headings are kept even when repeated, because a chunk with no heading
    loses the label the agent uses to say where an answer came from.
    """
    chrome = repeated_lines(docs, ratio=ratio)
    if not chrome:
        return docs, 0
    removed = 0
    for doc in docs:
        kept = []
        for line in doc.text.split("\n"):
            stripped = line.strip()
            if stripped and stripped in chrome and not stripped.startswith("## "):
                removed += 1
                continue
            kept.append(line)
        doc.text = "\n".join(kept).strip()
    return docs, removed
SVEOF15

cat > app/ingest/brief.py << 'SVEOF16'
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
SVEOF16

cat > app/ingest/chunk.py << 'SVEOF17'
"""Split extracted text into retrievable chunks.

Chunks follow heading boundaries first and fall back to a sliding window with
overlap. Each carries its page URL and nearest heading so the agent can say
where an answer came from, which is the part that makes a demo believable.
"""
from __future__ import annotations

from dataclasses import dataclass

TARGET_CHARS = 900
OVERLAP_CHARS = 150
MIN_CHARS = 80


@dataclass
class Chunk:
    url: str
    title: str
    heading: str
    text: str


def _window(text: str) -> list[str]:
    if len(text) <= TARGET_CHARS:
        return [text]
    out, start = [], 0
    while start < len(text):
        end = min(start + TARGET_CHARS, len(text))
        if end < len(text):
            cut = text.rfind("\n", start + MIN_CHARS, end)
            if cut == -1:
                cut = text.rfind(". ", start + MIN_CHARS, end)
                cut = cut + 1 if cut != -1 else -1
            if cut != -1:
                end = cut
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - OVERLAP_CHARS, start + 1)
    return [c for c in out if c]


def chunk_doc(url: str, title: str, text: str) -> list[Chunk]:
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in text.split("\n"):
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        else:
            sections[-1][1].append(line)

    chunks: list[Chunk] = []
    for heading, lines in sections:
        body = "\n".join(l for l in lines if l.strip()).strip()
        if len(body) < MIN_CHARS and heading:
            body = f"{heading}\n{body}".strip()
        if len(body) < MIN_CHARS:
            continue
        for piece in _window(body):
            if len(piece) >= MIN_CHARS:
                chunks.append(Chunk(url=url, title=title, heading=heading, text=piece))
    return chunks
SVEOF17

cat > app/ingest/crawl.py << 'SVEOF18'
"""Same-origin crawler.

Order of preference: sitemap.xml, then breadth-first link following. Sitemaps
give better coverage in fewer requests and put the important pages first, which
matters because the page budget is small.

robots.txt is honoured. A demo that ignores it is a demo you cannot show to the
company whose site you crawled.

Two rules earned by failing on a real site:

The URL you fetch is not the URL you compare. Stripping `www.` is right for
deciding whether two links are the same page and wrong for deciding what to
request, because plenty of hosts serve only one of the two. Fetching uses the
host as given, then adopts whatever the root redirected to.

A crawl that fetches nothing must say why. "Wrong URL, or the site blocks
crawlers" is a guess, and a guess sends you looking in the wrong place.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

SKIP_EXT = re.compile(
    r"\.(jpg|jpeg|png|gif|svg|webp|ico|css|js|mjs|woff2?|ttf|eot|zip|gz|tar|"
    r"mp4|mp3|wav|avi|mov|pdf|doc|docx|xls|xlsx|ppt|pptx)$",
    re.I,
)
SKIP_PATH = re.compile(
    r"/(wp-json|wp-admin|cdn-cgi|feed|rss|tag|author|cart|checkout|login|"
    r"signin|signup|account)(/|$)",
    re.I,
)
# Listing pages built from query parameters. Each is a reshuffle of posts that
# are already indexed individually, so they add near-duplicate chunks and no
# new facts. `?tag=`, `?author=`, `?page=` are the usual three.
SKIP_QUERY = re.compile(r"(^|&)(tag|author|page|paged|s|q|sort|filter)=", re.I)

BOT_UA = "SiteVoiceDemoBot/0.1 (+contact via the site owner)"
# Used only after the polite identifier is refused. Many small-business sites
# sit behind a WAF that rejects anything it does not recognise, including
# well-behaved crawlers, so the choice is this or no demo.
FALLBACK_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
BLOCKED_STATUS = {401, 403, 406, 429, 503}
# Connect-level failures worth retrying on a forced IPv4 socket. httpx has no
# Happy Eyeballs: given an AAAA record and a container whose IPv6 route goes
# nowhere, it picks v6 and hangs until timeout. curl falls back in
# milliseconds, which is why the site looks reachable from the same shell.
CONNECT_FAILURES = (httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadTimeout)


@dataclass
class Page:
    url: str
    status: int
    html: str


@dataclass
class CrawlReport:
    """Everything needed to tell a person what actually happened."""

    root: str = ""
    final_root: str = ""
    robots_status: str = ""
    root_status: str = ""
    content_type: str = ""
    user_agent: str = BOT_UA
    ua_fallback_used: bool = False
    ipv4_forced: bool = False
    sitemap_urls: int = 0
    fetched: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def fail(self, reason: str) -> None:
        self.failures[reason] = self.failures.get(reason, 0) + 1

    def render(self) -> str:
        lines = [
            f"  root requested   {self.root}",
            f"  root resolved    {self.final_root or '(never resolved)'}",
            f"  root response    {self.root_status or '(no response)'}",
            f"  content type     {self.content_type or '-'}",
            f"  robots.txt       {self.robots_status or '-'}",
            f"  user agent       {'browser fallback' if self.ua_fallback_used else 'SiteVoiceDemoBot'}",
            f"  socket           {'forced IPv4' if self.ipv4_forced else 'default'}",
            f"  sitemap urls     {self.sitemap_urls}",
            f"  pages fetched    {self.fetched}",
        ]
        if self.failures:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(self.failures.items()))
            lines.append(f"  failures         {detail}")
        lines.extend(f"  note             {n}" for n in self.notes)
        return "\n".join(lines)


def strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def normalise(url: str, *, for_fetch: bool = True) -> str:
    """Canonicalise a URL.

    `for_fetch=True` keeps the host exactly as written, because that is what
    has to go on the wire. `for_fetch=False` drops `www.` and gives a key for
    comparing and de-duplicating.
    """
    url, _ = urldefrag(url)
    parsed = urlparse(url)
    scheme = (parsed.scheme or "https").lower()
    host = parsed.netloc.lower()
    if not for_fetch:
        host = strip_www(host)
    if (scheme == "https" and host.endswith(":443")) or (
        scheme == "http" and host.endswith(":80")
    ):
        host = host.rsplit(":", 1)[0]
    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query = parsed.query
    if query:
        keep = [
            p
            for p in query.split("&")
            if p and not p.split("=")[0].lower().startswith(("utm_", "fbclid", "gclid"))
        ]
        query = "&".join(sorted(keep))
    return f"{scheme}://{host}{path}" + (f"?{query}" if query else "")


def key(url: str) -> str:
    """Identity of a page, ignoring www and scheme."""
    return normalise(url, for_fetch=False).split("://", 1)[-1]


def same_site(url: str, root: str) -> bool:
    return strip_www(urlparse(normalise(url)).netloc) == strip_www(
        urlparse(normalise(root)).netloc
    )


def crawlable(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    if SKIP_EXT.search(parsed.path) or SKIP_PATH.search(parsed.path):
        return False
    if parsed.query and SKIP_QUERY.search(parsed.query):
        return False
    return True


LINK_RE = re.compile(r"""<a\s[^>]*href\s*=\s*["']([^"'>]+)["']""", re.I)
LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


def extract_links(html: str, base: str) -> list[str]:
    out = []
    for href in LINK_RE.findall(html):
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        out.append(normalise(urljoin(base, href)))
    return out


def open_client(timeout_s: float, concurrency: int, *, force_ipv4: bool = False):
    """One place that builds the HTTP client, so the IPv4 fallback is a flag."""
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(
            local_address="0.0.0.0" if force_ipv4 else None,
            limits=httpx.Limits(max_connections=concurrency),
            retries=1,
        ),
        follow_redirects=True,
        timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 8.0)),
        headers={"Accept": "text/html,application/xhtml+xml"},
    )


async def _probe_root(client: httpx.AsyncClient, root: str, report: CrawlReport):
    """Fetch the root once, learn the real host, and detect a UA block.

    Doing this before anything else means a wrong hostname, a redirect to a
    different domain, or a WAF rejection is reported as itself rather than as
    an empty crawl.
    """
    for attempt, ua in enumerate((BOT_UA, FALLBACK_UA)):
        try:
            resp = await client.get(root, headers={"User-Agent": ua})
        except CONNECT_FAILURES as exc:
            report.root_status = f"{type(exc).__name__}: {exc}"
            return None, ua
        except Exception as exc:
            report.root_status = f"{type(exc).__name__}: {exc}"
            report.note(
                "the root URL could not be reached at all. Check the spelling "
                "and that the host resolves from this machine."
            )
            return None, ua
        report.root_status = str(resp.status_code)
        report.content_type = resp.headers.get("content-type", "")
        report.final_root = normalise(str(resp.url))
        if resp.status_code in BLOCKED_STATUS and attempt == 0:
            report.note(
                f"the site answered {resp.status_code} to a declared crawler. "
                f"Retrying with a browser user agent."
            )
            continue
        if resp.status_code != 200:
            report.note(
                f"the root returned {resp.status_code}, so there is nothing to "
                f"crawl. Open the URL in a browser and confirm it loads."
            )
            return None, ua
        if "html" not in report.content_type.lower():
            report.note(
                f"the root is {report.content_type or 'an unknown type'}, not "
                f"HTML. This crawler only reads HTML pages."
            )
            return None, ua
        report.user_agent = ua
        report.ua_fallback_used = ua is FALLBACK_UA
        return Page(url=report.final_root, status=200, html=resp.text), ua
    return None, FALLBACK_UA


async def _robots(client: httpx.AsyncClient, root: str, ua: str, report: CrawlReport):
    rp = RobotFileParser()
    target = urljoin(root, "/robots.txt")
    rp.set_url(target)
    try:
        resp = await client.get(target, headers={"User-Agent": ua})
    except Exception as exc:
        report.robots_status = f"unreachable ({type(exc).__name__})"
        rp.parse([])
        return rp
    if resp.status_code == 200 and "html" not in resp.headers.get(
        "content-type", ""
    ).lower():
        report.robots_status = "200, honoured"
        rp.parse(resp.text.splitlines())
    else:
        report.robots_status = f"{resp.status_code}, no rules"
        rp.parse([])
    return rp


async def _sitemap_urls(client, root: str, ua: str, limit: int) -> list[str]:
    found: list[str] = []
    queue = [urljoin(root, "/sitemap.xml"), urljoin(root, "/sitemap_index.xml")]
    seen_maps: set[str] = set()
    while queue and len(found) < limit:
        target = queue.pop(0)
        if target in seen_maps:
            continue
        seen_maps.add(target)
        try:
            resp = await client.get(target, headers={"User-Agent": ua})
        except Exception:
            continue
        if resp.status_code != 200 or "<loc" not in resp.text.lower():
            continue
        for loc in (normalise(u) for u in LOC_RE.findall(resp.text)):
            if loc.endswith(".xml") and len(seen_maps) < 12:
                queue.append(loc)
            elif same_site(loc, root) and crawlable(loc):
                found.append(loc)
    ordered, seen = [], set()
    for url in found:
        if key(url) not in seen:
            seen.add(key(url))
            ordered.append(url)
    return ordered[:limit]


async def crawl(
    root: str,
    *,
    max_pages: int = 120,
    max_depth: int = 3,
    timeout_s: float = 15.0,
    concurrency: int = 6,
    on_page=None,
) -> tuple[list[Page], CrawlReport]:
    report = CrawlReport(root=normalise(root))

    # Try the default socket, then a forced-IPv4 one. Anything that survives
    # both is a real network problem rather than an address-family accident.
    for force_ipv4 in (False, True):
        client = open_client(timeout_s, concurrency, force_ipv4=force_ipv4)
        first, ua = await _probe_root(client, report.root, report)
        if first is not None:
            report.ipv4_forced = force_ipv4
            break
        await client.aclose()
        if force_ipv4 or "Connect" not in report.root_status:
            break
        report.note(
            "the connection timed out on the default socket. Retrying on IPv4 "
            "only, which is the usual fix inside a container with no working "
            "IPv6 route."
        )

    if first is None:
        if "Connect" in report.root_status:
            report.note(
                "IPv4 timed out too. Either this network cannot reach the site "
                "or the site drops traffic from it. Run scripts/netcheck.py to "
                "tell those apart."
            )
        return [], report

    # Not `async with`: the client is already open from the probe above, and
    # httpx refuses to re-enter one. Close it in the finally instead.
    try:

        # Follow the root's own redirect rather than arguing with it: a site
        # that sends the apex to www knows better than we do.
        root = report.final_root
        rp = await _robots(client, root, ua, report)

        def allowed(url: str) -> bool:
            try:
                return rp.can_fetch(ua, url)
            except Exception:
                return True

        seeds = await _sitemap_urls(client, root, ua, max_pages)
        report.sitemap_urls = len(seeds)
        if not seeds:
            report.note("no sitemap found, falling back to following links")

        pages: list[Page] = [first]
        seen: set[str] = {key(first.url)}
        if on_page:
            on_page(first, 1)
        frontier: list[tuple[str, int]] = [(u, 1) for u in seeds]
        frontier += [(u, 1) for u in extract_links(first.html, first.url)]
        sem = asyncio.Semaphore(concurrency)

        async def fetch(url: str) -> Page | None:
            async with sem:
                try:
                    resp = await client.get(url, headers={"User-Agent": ua})
                except Exception as exc:
                    report.fail(type(exc).__name__)
                    return None
            ctype = resp.headers.get("content-type", "")
            if resp.status_code != 200:
                report.fail(f"http {resp.status_code}")
                return None
            if "html" not in ctype.lower():
                report.fail("not html")
                return None
            final = normalise(str(resp.url))
            # A link can redirect off-site. Checking only the requested URL
            # lets a payment or booking provider's pages into the archive,
            # and the agent then answers questions about the wrong company.
            if not same_site(final, root):
                report.fail("redirected off-site")
                return None
            return Page(url=final, status=200, html=resp.text)

        while frontier and len(pages) < max_pages:
            batch: list[tuple[str, int]] = []
            while (
                frontier
                and len(batch) < concurrency
                and len(pages) + len(batch) < max_pages
            ):
                url, depth = frontier.pop(0)
                if key(url) in seen or not same_site(url, root):
                    continue
                if not crawlable(url):
                    continue
                if not allowed(url):
                    report.fail("robots disallow")
                    continue
                seen.add(key(url))
                batch.append((url, depth))
            if not batch:
                continue
            for (url, depth), page in zip(
                batch, await asyncio.gather(*(fetch(u) for u, _ in batch))
            ):
                if page is None:
                    continue
                pages.append(page)
                if on_page:
                    on_page(page, len(pages))
                if depth >= max_depth:
                    continue
                for link in extract_links(page.html, page.url):
                    if key(link) not in seen and same_site(link, root):
                        frontier.append((link, depth + 1))

        report.fetched = len(pages)
        return pages, report
    finally:
        await client.aclose()
SVEOF18

cat > app/ingest/extract.py << 'SVEOF19'
"""Boilerplate removal.

Nav, footer, and cookie banners repeat on every page. Left in, they dominate
the embedding space and every query retrieves the header. Stripped, a small
site fits comfortably in a few thousand tokens.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser

DROP = (
    "script", "style", "noscript", "svg", "iframe", "nav", "footer", "header",
    "aside", "form", "template", "picture", "video", "audio",
)
DROP_HINT = re.compile(
    r"(nav|menu|footer|header|cookie|consent|banner|sidebar|breadcrumb|social|"
    r"share|subscribe|newsletter|popup|modal|skip-link)",
    re.I,
)
WS = re.compile(r"[ \t\r\f\v]+")
BLANKS = re.compile(r"\n{3,}")


@dataclass
class Doc:
    url: str
    title: str
    description: str = ""
    text: str = ""
    headings: list[str] = field(default_factory=list)

    @property
    def words(self) -> int:
        return len(self.text.split())


def _clean(text: str) -> str:
    text = WS.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    return BLANKS.sub("\n\n", "\n".join(l for l in lines if l))


def extract(url: str, html: str) -> Doc:
    tree = HTMLParser(html)

    title = ""
    if tree.css_first("title"):
        title = tree.css_first("title").text(strip=True)
    description = ""
    meta = tree.css_first('meta[name="description"]') or tree.css_first(
        'meta[property="og:description"]'
    )
    if meta is not None:
        description = (meta.attributes.get("content") or "").strip()

    for tag in DROP:
        for node in tree.css(tag):
            node.decompose()
    for node in tree.css("[class],[id],[role]"):
        attrs = " ".join(
            filter(None, [node.attributes.get("class"), node.attributes.get("id"),
                          node.attributes.get("role")])
        )
        if attrs and DROP_HINT.search(attrs):
            node.decompose()

    main = (
        tree.css_first("main")
        or tree.css_first("article")
        or tree.css_first('[role="main"]')
        or tree.body
        or tree.root
    )
    if main is None:
        return Doc(url=url, title=title, description=description)

    headings = [h.text(strip=True) for h in main.css("h1,h2,h3") if h.text(strip=True)]

    parts: list[str] = []
    for node in main.css("h1,h2,h3,h4,p,li,td,th,dd,dt,blockquote,figcaption"):
        chunk = node.text(separator=" ", strip=True)
        if not chunk:
            continue
        tag = node.tag
        if tag in ("h1", "h2", "h3", "h4"):
            parts.append(f"\n## {chunk}\n")
        elif tag in ("li", "dd", "dt"):
            parts.append(f"- {chunk}")
        else:
            parts.append(chunk)

    text = _clean("\n".join(parts))

    # A page whose every line repeats elsewhere is navigation we failed to kill.
    deduped, seen = [], set()
    for line in text.split("\n"):
        key = line.strip().lower()
        if key and key in seen:
            continue
        seen.add(key)
        deduped.append(line)
    return Doc(url=url, title=title, description=description,
               text=_clean("\n".join(deduped)), headings=headings[:20])
SVEOF19

cat > app/main.py << 'SVEOF20'
"""FastAPI entrypoint: Twilio webhook, media stream bridge, monitor socket."""

from __future__ import annotations

import contextlib
import json
import logging
import pathlib
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from . import agent as agent_mod
from .config import get_settings, log_config_source
from .providers.gemini import GeminiLiveProvider
from .providers.mock import MockProvider
from .retrieval import Index
from .store import connect, site_row
from .telephony.bridge import MediaBridge
from .telephony.twilio_webhook import (
    connect_stream_twiml,
    public_url,
    validate_twilio_signature,
)

log = logging.getLogger(__name__)
settings = get_settings()

SITE: dict = {}
INDEX: Index | None = None
CONN = None

# Live monitor sockets, keyed by call id. The screen subscribes here.
_monitors: dict[str, set[WebSocket]] = {}
# Caller and dialled number per call, carried from the webhook to the stream
# socket. Twilio's `start` event does not include them.
_dialled: dict[str, dict] = {}
# Finished calls, newest first, so the screen survives a refresh.
_history: list[dict] = []


@asynccontextmanager
async def lifespan(_: FastAPI):
    global CONN, INDEX
    logging.basicConfig(level=logging.INFO)
    log.info("config from %s", log_config_source())
    if not settings.public_base_url:
        log.error(
            "PUBLIC_BASE_URL is empty. The stream URL handed to Twilio will be "
            "wss:///ws/twilio/... with no host, and the call will connect and "
            "then go silent."
        )
    CONN = connect(settings.site_db)
    row = site_row(CONN)
    if row:
        SITE.update(dict(row))
        INDEX = Index(CONN, row["dims"])
        log.info(
            "loaded %s: %s pages, %s chunks, crawled %s",
            row["name"], row["page_count"], INDEX.size, row["crawled_at"],
        )
    else:
        log.error(
            "no site ingested. run: python -m app.ingest <url>. Until then the "
            "agent has nothing to answer from and every call will be useless."
        )
    yield
    CONN.close()


app = FastAPI(title="site-voice", lifespan=lifespan)


def effective_provider() -> str:
    """Which provider will actually handle the next call.

    Not the same as the configured value. Asking for gemini with no API key
    silently yields a mock while /health still says gemini, which is a bad way
    to lose twenty minutes.
    """
    if settings.realtime_provider == "mock":
        return "mock"
    if settings.realtime_provider == "gemini" and not settings.gemini_api_key:
        if settings.app_env == "prod":
            raise RuntimeError(
                "REALTIME_PROVIDER=gemini but GEMINI_API_KEY is unset. Set the "
                "key or set REALTIME_PROVIDER=mock explicitly."
            )
        log.warning(
            "GEMINI_API_KEY is unset, falling back to the mock provider. Calls "
            "will not reach a real model."
        )
        return "mock"
    return settings.realtime_provider


def build_provider():
    if effective_provider() == "mock":
        return MockProvider()
    return GeminiLiveProvider(
        api_key=settings.gemini_api_key,
        model=settings.gemini_live_model,
        voice=settings.gemini_voice,
        thinking_level=settings.gemini_thinking_level,
        end_of_speech_silence_ms=settings.gemini_end_of_speech_ms,
    )


@app.get("/health")
async def health():
    actual = effective_provider()
    body = {
        "ok": True,
        "provider": actual,
        "site": SITE.get("root_url"),
        "pages": SITE.get("page_count", 0),
        "chunks": INDEX.size if INDEX else 0,
        "crawled_at": SITE.get("crawled_at"),
    }
    if actual != settings.realtime_provider:
        body["configured"] = settings.realtime_provider
        body["note"] = "falling back: GEMINI_API_KEY is unset"
    if not SITE:
        body["ok"] = False
        body["note"] = "no site ingested"
    return body


@app.post("/twilio/voice")
async def inbound_call(
    request: Request, To: str = Form(""), From: str = Form(""), CallSid: str = Form("")
):
    """Twilio hits this when someone dials."""
    form = dict(await request.form())
    if settings.twilio_validate_signature and settings.app_env != "test":
        signed_url = public_url(request)
        ok = validate_twilio_signature(
            settings.twilio_auth_token,
            signed_url,
            form,
            request.headers.get("X-Twilio-Signature", ""),
        )
        if not ok:
            log.warning(
                "twilio signature mismatch. Validated against %s. If that is not "
                "the URL configured in the Twilio console, they must match "
                "exactly, including https and any trailing slash.",
                signed_url,
            )
            return Response(status_code=403, content="invalid signature")

    call_id = CallSid or str(uuid.uuid4())
    _dialled[call_id] = {"to": To, "from": From}
    base = settings.public_base_url.replace("https://", "").replace("http://", "")
    ws_url = f"wss://{base}/ws/twilio/{call_id}"
    return Response(content=connect_stream_twiml(ws_url), media_type="application/xml")


@app.websocket("/ws/twilio/{call_id}")
async def twilio_stream(ws: WebSocket, call_id: str):
    await ws.accept()
    routing = _dialled.pop(call_id, {})
    sources: list[str] = []

    async def fan_out(payload: dict):
        payload["call_id"] = call_id
        dead = set()
        for sub in _monitors.get("*", set()):
            try:
                await sub.send_text(json.dumps(payload))
            except Exception:
                dead.add(sub)
        _monitors.get("*", set()).difference_update(dead)

    def note_sources(urls: list[str]):
        for url in urls:
            if url not in sources:
                sources.append(url)

    dispatcher = None
    tools: list[dict] = []
    instructions = "You answer the phone. Keep replies short."
    if INDEX is not None and SITE:
        instructions = agent_mod.build(
            name=SITE["name"],
            brief=SITE["brief"],
            crawled_at=SITE["crawled_at"],
            facts=SITE.get("facts", ""),
            tz=settings.site_timezone,
        )
        tools = agent_mod.TOOL_SCHEMAS
        dispatcher = agent_mod.ToolDispatcher(
            index=INDEX, settings=settings, on_sources=note_sources
        )
    else:
        log.error("call %s arrived with no site loaded", call_id)

    await fan_out({"type": "call_started", "from": routing.get("from", "")})

    bridge = MediaBridge(
        ws,
        build_provider(),
        instructions=instructions,
        tools=tools,
        on_event=fan_out,
        max_call_seconds=settings.max_call_seconds,
        dispatch_tool=dispatcher.dispatch if dispatcher else None,
        tool_timeout_ms=settings.tool_timeout_ms,
        greeting=agent_mod.greeting(SITE.get("name", "")) if SITE else None,
    )

    summary = {}
    try:
        summary = await bridge.run()
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("call %s failed", call_id)
    finally:
        record = {
            "call_id": call_id,
            "from": routing.get("from", ""),
            "transcript": bridge.transcript,
            "sources": sources,
            "messages": dispatcher.messages if dispatcher else [],
            "lookups": dispatcher.lookups if dispatcher else [],
            "stats": summary,
        }
        _history.insert(0, record)
        del _history[25:]
        with contextlib.suppress(Exception):
            await fan_out({"type": "call_finished", **record})
        log.info("call %s: %s", call_id, summary)


@app.websocket("/ws/monitor")
async def monitor(ws: WebSocket):
    await ws.accept()
    _monitors.setdefault("*", set()).add(ws)
    await ws.send_text(
        json.dumps(
            {
                "type": "hello",
                "site": SITE.get("name", ""),
                "root": SITE.get("root_url", ""),
                "chunks": INDEX.size if INDEX else 0,
            }
        )
    )
    try:
        while True:
            await ws.receive_text()
    except Exception:
        pass
    finally:
        _monitors.get("*", set()).discard(ws)


@app.get("/api/calls")
async def calls():
    return {"calls": _history}


@app.get("/", response_class=HTMLResponse)
async def screen():
    html = (pathlib.Path(__file__).with_name("screen.html")).read_text()
    return html.replace("__SITE__", SITE.get("name", "no site ingested")).replace(
        "__ROOT__", SITE.get("root_url", "")
    )
SVEOF20

cat > app/providers/__init__.py << 'SVEOF21'

SVEOF21

cat > app/providers/base.py << 'SVEOF22'
"""Realtime speech provider interface.

The bridge talks to this, never to a vendor SDK. Two reasons: we benchmark
providers against each other in M1 before committing, and the kiosk in Stage 2
reuses whichever wins without touching adapter code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol


@dataclass
class ProviderEvent:
    """One thing the model did. The bridge only understands these."""

    kind: Literal["audio", "transcript", "tool_call", "turn_end", "error"]
    audio: bytes = b""
    text: str = ""
    role: Literal["caller", "agent", ""] = ""
    tool_call_id: str = ""
    tool_name: str = ""
    tool_args: dict = field(default_factory=dict)
    detail: str = ""


class RealtimeProvider(Protocol):
    """A speech-to-speech model session.

    Contract notes learned the hard way:

    * `receive()` must keep yielding across turn boundaries. Several vendor
      SDKs end their async iterator at the end of every model turn. If the
      adapter passes that termination through, the bridge goes deaf after the
      greeting and the call dies to a keepalive timeout. Adapters wrap the
      vendor iterator in an outer loop so this generator only ends when the
      session actually closes.

    * `interrupt()` must be safe to call when the model is not speaking.
    """

    input_hz: int
    output_hz: int

    async def connect(self, *, instructions: str, tools: list[dict]) -> None: ...
    async def send_audio(self, pcm: bytes) -> None: ...
    async def send_text(self, text: str) -> None: ...
    async def send_tool_result(self, call_id: str, name: str, result: dict) -> None: ...
    async def interrupt(self) -> None: ...
    def receive(self) -> AsyncIterator[ProviderEvent]: ...
    async def close(self) -> None: ...
SVEOF22

cat > app/providers/gemini.py << 'SVEOF23'
"""Gemini Live adapter.

The one thing that matters here is the outer while-loop in `receive()`. The
SDK's async iterator terminates at every turn boundary. Passing that through
to the bridge makes it stop listening after the greeting, and the call then
dies to a keepalive timeout with no obvious cause. The loop below is why that
does not happen.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from .base import ProviderEvent

log = logging.getLogger(__name__)


class GeminiLiveProvider:
    input_hz = 16000
    output_hz = 24000

    def __init__(
        self,
        api_key: str,
        model: str,
        voice: str = "Aoede",
        thinking_level: str = "minimal",
        end_of_speech_silence_ms: int = 500,
    ):
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.thinking_level = thinking_level
        # How long the model waits in silence before deciding the caller has
        # finished. This is added to every single turn, so the default is a
        # large share of perceived latency. Too low and it interrupts people
        # who pause mid-sentence; 400-600ms is the usable range on a phone.
        self.end_of_speech_silence_ms = end_of_speech_silence_ms
        self._session = None
        self._ctx = None
        self._closed = False

    async def connect(self, *, instructions: str, tools: list[dict]) -> None:
        from google import genai  # imported lazily so tests need no SDK

        self._client = genai.Client(api_key=self.api_key)
        config = {
            "response_modalities": ["AUDIO"],
            "system_instruction": instructions,
            "speech_config": {
                "voice_config": {"prebuilt_voice_config": {"voice_name": self.voice}}
            },
            "input_audio_transcription": {},
            "output_audio_transcription": {},
        }
        if tools:
            config["tools"] = [{"function_declarations": tools}]
        if self.end_of_speech_silence_ms:
            config["realtime_input_config"] = {
                "automatic_activity_detection": {
                    "silence_duration_ms": self.end_of_speech_silence_ms,
                }
            }
        if self.thinking_level:
            # Lower thinking means faster first audio. On a phone call the
            # caller hears the delay, so this is not a free knob.
            config["thinking_config"] = {"thinking_level": self.thinking_level}

        self._ctx = self._client.aio.live.connect(model=self.model, config=config)
        try:
            self._session = await self._ctx.__aenter__()
        except Exception as exc:
            # Model ids churn on the developer tier. Say which one failed
            # rather than surfacing a bare 404 from deep in the SDK.
            raise RuntimeError(
                f"could not open a Gemini Live session with model "
                f"{self.model!r}. Check the model id is current at "
                f"https://ai.google.dev/gemini-api/docs/models. Underlying "
                f"error: {type(exc).__name__}: {exc}"
            ) from exc

    async def send_audio(self, pcm: bytes) -> None:
        from google.genai import types

        await self._session.send_realtime_input(
            audio=types.Blob(data=pcm, mime_type=f"audio/pcm;rate={self.input_hz}")
        )

    async def send_text(self, text: str) -> None:
        await self._session.send_realtime_input(text=text)

    async def send_tool_result(self, call_id: str, name: str, result: dict) -> None:
        from google.genai import types

        await self._session.send_tool_response(
            function_responses=[
                types.FunctionResponse(id=call_id, name=name, response=result)
            ]
        )

    async def interrupt(self) -> None:
        """Gemini Live handles VAD-based interruption server side.

        We still call this so the bridge's contract holds for every provider,
        and so a provider that needs an explicit cancel can implement it.
        """
        return

    async def receive(self) -> AsyncIterator[ProviderEvent]:
        while not self._closed:
            try:
                turn = self._session.receive()
                async for response in turn:
                    sc = getattr(response, "server_content", None)

                    if getattr(response, "data", None):
                        yield ProviderEvent(kind="audio", audio=response.data)

                    if sc is not None:
                        it = getattr(sc, "input_transcription", None)
                        if it is not None and getattr(it, "text", ""):
                            yield ProviderEvent(
                                kind="transcript", role="caller", text=it.text
                            )
                        ot = getattr(sc, "output_transcription", None)
                        if ot is not None and getattr(ot, "text", ""):
                            yield ProviderEvent(
                                kind="transcript", role="agent", text=ot.text
                            )
                        if getattr(sc, "interrupted", False):
                            yield ProviderEvent(kind="turn_end")
                        if getattr(sc, "turn_complete", False):
                            yield ProviderEvent(kind="turn_end")

                    tc = getattr(response, "tool_call", None)
                    if tc is not None:
                        for fc in getattr(tc, "function_calls", []) or []:
                            yield ProviderEvent(
                                kind="tool_call",
                                tool_call_id=getattr(fc, "id", "") or "",
                                tool_name=fc.name,
                                tool_args=dict(fc.args or {}),
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                yield ProviderEvent(kind="error", detail=f"{type(exc).__name__}: {exc}")
                return

    async def close(self) -> None:
        self._closed = True
        if self._ctx is not None:
            try:
                await self._ctx.__aexit__(None, None, None)
            except Exception as exc:
                # A failed teardown must never mask the call's real outcome.
                log.warning("gemini session teardown failed: %s", exc)
SVEOF23

cat > app/providers/mock.py << 'SVEOF24'
"""Scripted provider for tests and offline demos.

Lets the whole bridge, including barge-in and latency accounting, be tested
without an API key, a network, or a phone.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import numpy as np

from .base import ProviderEvent


def tone(ms: int, hz: int = 440, rate: int = 16000) -> bytes:
    t = np.linspace(0, ms / 1000, int(rate * ms / 1000), endpoint=False)
    return (np.sin(2 * np.pi * hz * t) * 8000).astype(np.int16).tobytes()


class MockProvider:
    input_hz = 16000
    output_hz = 16000

    def __init__(self, script: list[ProviderEvent] | None = None):
        self.sent_audio: list[bytes] = []
        self.sent_text: list[str] = []
        self.tool_results: list[dict] = []
        self.interrupts = 0
        self.closed = False
        self.connected = False
        self.instructions = ""
        self.tools: list[dict] = []
        self._queue: asyncio.Queue[ProviderEvent] = asyncio.Queue()
        for ev in script or []:
            self._queue.put_nowait(ev)

    async def connect(self, *, instructions: str, tools: list[dict]) -> None:
        self.connected = True
        self.instructions = instructions
        self.tools = tools

    async def send_audio(self, pcm: bytes) -> None:
        self.sent_audio.append(pcm)

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)

    async def send_tool_result(self, call_id: str, name: str, result: dict) -> None:
        self.tool_results.append({"call_id": call_id, "name": name, "result": result})

    async def interrupt(self) -> None:
        self.interrupts += 1
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def push(self, ev: ProviderEvent) -> None:
        self._queue.put_nowait(ev)

    async def receive(self) -> AsyncIterator[ProviderEvent]:
        while not self.closed:
            try:
                ev = await asyncio.wait_for(self._queue.get(), timeout=0.05)
            except TimeoutError:
                continue
            yield ev

    async def close(self) -> None:
        self.closed = True
SVEOF24

cat > app/retrieval.py << 'SVEOF25'
"""In-memory cosine retrieval over the crawled site.

The whole index is a numpy matrix. At demo scale, a hundred pages is a few
thousand chunks, which is under 10 MB at 768 dimensions and searches in well
under a millisecond. A vector database here would add a network hop to a path
that is already competing with a 1.6 second model round trip.
"""
from __future__ import annotations

import numpy as np

from .store import Retrieved, load_matrix


class Index:
    def __init__(self, conn, dims: int):
        self.dims = dims
        self.matrix, self.rows = load_matrix(conn, dims)

    @property
    def size(self) -> int:
        return len(self.rows)

    def search(self, query_vec, *, top_k: int = 4, min_score: float = 0.55) -> list[Retrieved]:
        if self.size == 0:
            return []
        q = np.asarray(query_vec, dtype=np.float32)
        norm = np.linalg.norm(q)
        if norm == 0:
            return []
        scores = self.matrix @ (q / norm)

        order = np.argsort(-scores)[: max(top_k * 3, top_k)]
        hits: list[Retrieved] = []
        seen_urls: set[str] = set()
        for i in order:
            score = float(scores[i])
            if score < min_score:
                break
            row = self.rows[int(i)]
            # One chunk per page. Three chunks off the same page sound like one
            # source to a caller and waste the context budget.
            if row["url"] in seen_urls:
                continue
            seen_urls.add(row["url"])
            hits.append(
                Retrieved(
                    url=row["url"],
                    title=row["title"],
                    heading=row["heading"],
                    text=row["text"],
                    score=score,
                )
            )
            if len(hits) >= top_k:
                break
        return hits


def render(hits: list[Retrieved]) -> dict:
    """Shape handed back to the model as a tool result.

    Passages stay separate and each keeps its source so the agent can attribute
    an answer. `found: false` is explicit because a model reads an empty list as
    an error and starts improvising.
    """
    if not hits:
        return {
            "found": False,
            "passages": [],
            "hint": "Nothing on the site covers this. Say so plainly and offer to take a message.",
        }
    return {
        "found": True,
        "passages": [
            {
                "source": h.url,
                "section": h.heading or h.title,
                "text": h.text,
            }
            for h in hits
        ],
    }
SVEOF25

cat > app/screen.html << 'SVEOF26'
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>site-voice</title>
<style>
  :root{--bg:#0d0f12;--panel:#14171c;--line:#242a33;--ink:#e6e9ee;
        --dim:#7d8794;--accent:#5fd3a6;--warn:#e0a15f;--caller:#7fb2ff}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}
  header{display:flex;justify-content:space-between;align-items:baseline;
         padding:16px 22px;border-bottom:1px solid var(--line)}
  header h1{font-size:14px;margin:0;letter-spacing:.14em;text-transform:uppercase}
  header .meta{color:var(--dim);font-size:12px}
  .dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:#444;
       margin-right:7px;vertical-align:middle}
  .dot.live{background:var(--accent);box-shadow:0 0 8px var(--accent)}
  main{display:grid;grid-template-columns:1fr 330px;height:calc(100vh - 53px)}
  #feed{overflow-y:auto;padding:22px}
  aside{border-left:1px solid var(--line);background:var(--panel);overflow-y:auto;padding:18px}
  aside h2{font-size:11px;letter-spacing:.14em;color:var(--dim);text-transform:uppercase;
           margin:0 0 12px}
  aside h2:not(:first-child){margin-top:26px}
  .turn{margin-bottom:18px;max-width:70ch}
  .who{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--dim)}
  .caller .who{color:var(--caller)}
  .agent .who{color:var(--accent)}
  .said{margin-top:3px;white-space:pre-wrap}
  .cut{color:var(--warn);font-size:11px}
  .sys{color:var(--dim);font-size:12px;margin:10px 0;border-left:2px solid var(--line);
       padding-left:10px}
  .sys b{color:var(--warn);font-weight:400}
  .src{display:block;color:var(--dim);font-size:11px;margin-bottom:9px;text-decoration:none;
       word-break:break-all;border-left:2px solid var(--accent);padding-left:8px}
  .src:hover{color:var(--ink)}
  .stat{display:flex;justify-content:space-between;font-size:12px;color:var(--dim);
        padding:3px 0}
  .stat b{color:var(--ink);font-weight:400}
  .empty{color:var(--dim);font-size:12px}
</style>
</head>
<body>
<header>
  <h1><span class="dot" id="dot"></span>site-voice</h1>
  <div class="meta">__SITE__ &nbsp;·&nbsp; __ROOT__ &nbsp;·&nbsp; <span id="chunks">—</span> chunks</div>
</header>
<main>
  <div id="feed"><div class="empty">Waiting for a call.</div></div>
  <aside>
    <h2>Sources cited</h2>
    <div id="sources"><div class="empty">None yet.</div></div>
    <h2>This call</h2>
    <div id="stats"><div class="empty">—</div></div>
  </aside>
</main>
<script>
const feed = document.getElementById('feed');
const sourcesEl = document.getElementById('sources');
const statsEl = document.getElementById('stats');
const dot = document.getElementById('dot');

let cleared = false, current = null, currentRole = null, seen = new Set();
const stats = {latency: [], lookups: 0, misses: 0, barge: 0};

const clearOnce = () => { if (!cleared) { feed.innerHTML = ''; cleared = true; } };
const atBottom = () => feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
const scroll = () => { feed.scrollTop = feed.scrollHeight; };

// Gemini streams transcription in fragments, so accumulate into one bubble
// per role and close it when the role changes or the turn ends.
function say(role, text){
  clearOnce();
  const stick = atBottom();
  if (currentRole !== role || !current) {
    const el = document.createElement('div');
    el.className = 'turn ' + (role === 'caller' ? 'caller' : 'agent');
    el.innerHTML = '<div class="who">' + (role === 'caller' ? 'caller' : 'agent') +
                   '</div><div class="said"></div>';
    feed.appendChild(el);
    current = el.querySelector('.said');
    currentRole = role;
  }
  current.textContent += text;
  if (stick) scroll();
}

function closeTurn(){ current = null; currentRole = null; }

function sys(html){
  clearOnce();
  const stick = atBottom();
  const el = document.createElement('div');
  el.className = 'sys';
  el.innerHTML = html;
  feed.appendChild(el);
  if (stick) scroll();
}

function addSources(urls){
  (urls || []).filter(Boolean).forEach(u => {
    if (seen.has(u)) return;
    seen.add(u);
    if (seen.size === 1) sourcesEl.innerHTML = '';
    const a = document.createElement('a');
    a.className = 'src'; a.href = u; a.target = '_blank';
    a.textContent = u.replace(/^https?:\/\//, '');
    sourcesEl.appendChild(a);
  });
}

function renderStats(){
  const med = a => a.length ? [...a].sort((x,y)=>x-y)[Math.floor(a.length/2)] : null;
  const rows = [
    ['median response', med(stats.latency) ? med(stats.latency) + ' ms' : '—'],
    ['lookups', stats.lookups],
    ['not on the site', stats.misses],
    ['barge-ins', stats.barge],
  ];
  statsEl.innerHTML = rows.map(([k,v]) =>
    '<div class="stat"><span>' + k + '</span><b>' + v + '</b></div>').join('');
}

function connect(){
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(proto + '://' + location.host + '/ws/monitor');
  ws.onopen = () => dot.classList.add('live');
  ws.onclose = () => { dot.classList.remove('live'); setTimeout(connect, 1500); };
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    switch (m.type) {
      case 'hello':
        document.getElementById('chunks').textContent = m.chunks;
        break;
      case 'call_started':
        cleared = false; feed.innerHTML = ''; closeTurn();
        seen = new Set(); sourcesEl.innerHTML = '<div class="empty">None yet.</div>';
        stats.latency = []; stats.lookups = 0; stats.misses = 0; stats.barge = 0;
        renderStats();
        sys('<b>call started</b> from ' + (m.from || 'unknown'));
        break;
      case 'transcript':
        say(m.role, m.text);
        break;
      case 'turn_end':
        closeTurn();
        break;
      case 'latency':
        stats.latency.push(m.ms); renderStats();
        break;
      case 'tool_call':
        closeTurn();
        sys('<b>' + m.name + '</b> ' + JSON.stringify(m.args));
        break;
      case 'tool_result': {
        const r = m.result || {};
        const found = r.found !== false && !r.error;
        if (m.name === 'lookup_site') { stats.lookups++; if (!found) stats.misses++; }
        renderStats();
        sys('<b>' + m.name + '</b> ' + (found ? 'matched' : 'nothing on the site') +
            ' in ' + m.ms + 'ms');
        addSources((r.passages || []).map(p => p.source));
        break;
      }
      case 'stalling':
        sys('<b>slow tool</b> agent asked to hold the line');
        break;
      case 'barge_in':
        stats.barge++; renderStats(); closeTurn();
        sys('<b>barge-in</b> caller interrupted');
        break;
      case 'error':
        sys('<b>error</b> ' + m.detail);
        break;
      case 'call_finished':
        closeTurn();
        sys('<b>call ended</b>');
        (m.messages || []).forEach(x => sys('<b>message taken</b> ' +
          (x.caller_name || 'anon') + ' · ' + (x.contact || 'no contact') + ' · ' + x.message));
        break;
    }
  };
}
renderStats();
connect();
</script>
</body>
</html>
SVEOF26

cat > app/store.py << 'SVEOF27'
"""SQLite store for one crawled site.

Deliberately not Postgres. This service holds one site at a time, embeddings
live in a numpy matrix in memory, and the database is a single file that can be
committed, copied to Railway, or thrown away. Zero shared infrastructure with
any other project.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS site (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    root_url    TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    brief       TEXT NOT NULL DEFAULT '',
    facts       TEXT NOT NULL DEFAULT '',
    greeting    TEXT NOT NULL DEFAULT '',
    crawled_at  TEXT NOT NULL,
    page_count  INTEGER NOT NULL DEFAULT 0,
    dims        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pages (
    url         TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    words       INTEGER NOT NULL DEFAULT 0,
    text        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY,
    url         TEXT NOT NULL REFERENCES pages(url) ON DELETE CASCADE,
    title       TEXT NOT NULL DEFAULT '',
    heading     TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL,
    embedding   BLOB
);
CREATE INDEX IF NOT EXISTS chunks_url ON chunks(url);

CREATE TABLE IF NOT EXISTS calls (
    sid         TEXT PRIMARY KEY,
    from_number TEXT NOT NULL DEFAULT '',
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    turns       TEXT NOT NULL DEFAULT '[]',
    outcome     TEXT NOT NULL DEFAULT 'in_progress'
);
"""


@dataclass
class Retrieved:
    url: str
    title: str
    heading: str
    text: str
    score: float


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def pack(vec) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def unpack(blob: bytes, dims: int) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32).reshape(dims)


def load_matrix(conn: sqlite3.Connection, dims: int):
    """Return (matrix, rows). Rows are aligned with matrix row order."""
    rows = conn.execute(
        "SELECT id, url, title, heading, text, embedding FROM chunks "
        "WHERE embedding IS NOT NULL ORDER BY id"
    ).fetchall()
    if not rows:
        return np.zeros((0, dims), dtype=np.float32), []
    mat = np.vstack([unpack(r["embedding"], dims) for r in rows]).astype(np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms, rows


def site_row(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM site WHERE id = 1").fetchone()


def save_site(conn, *, root_url, name, brief, greeting, crawled_at, page_count,
              dims, facts=""):
    conn.execute("DELETE FROM site")
    conn.execute(
        "INSERT INTO site (id, root_url, name, brief, facts, greeting, crawled_at,"
        " page_count, dims) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
        (root_url, name, brief, facts, greeting, crawled_at, page_count, dims),
    )
    conn.commit()


def record_turn(conn, sid: str, role: str, text: str, sources: list[str] | None = None):
    row = conn.execute("SELECT turns FROM calls WHERE sid = ?", (sid,)).fetchone()
    turns = json.loads(row["turns"]) if row else []
    turns.append({"role": role, "text": text, "sources": sources or []})
    conn.execute("UPDATE calls SET turns = ? WHERE sid = ?", (json.dumps(turns), sid))
    conn.commit()
SVEOF27

cat > app/telephony/__init__.py << 'SVEOF28'

SVEOF28

cat > app/telephony/bridge.py << 'SVEOF29'
"""Twilio Media Stream <-> realtime provider bridge.

Design notes, most of them earned by breaking things previously.

Barge-in is one cancellable unit.
    When the caller starts talking over the agent, three things must happen
    together: the provider stops generating, our pending outbound frames are
    dropped, and Twilio's own playback buffer is flushed with a `clear`
    message. Doing only the first two leaves up to a second of already-sent
    audio still playing at the caller's ear, which feels like the agent
    ignoring them.

A grace window guards against line noise.
    Without it, a cough or a burst of background clatter clips the agent
    mid-word. We require sustained caller energy before treating it as a real
    interruption.

Interruption memory is honest.
    When a turn is cut short, the transcript records what actually reached the
    caller, not what the model intended to say. Otherwise the model believes it
    said things the caller never heard and the conversation drifts.

Outbound audio is paced in 20 ms frames.
    Twilio accepts larger writes, but a `clear` can only land between frames.
    Small frames mean interruption latency is bounded by one frame, not by the
    length of whatever blob we last wrote.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .. import audio as A

# Caller audio above this RMS counts as speech rather than line noise.
log = logging.getLogger(__name__)

BARGE_RMS_THRESHOLD = 550
# Sustained speech required before we treat it as a real interruption.
BARGE_SUSTAIN_FRAMES = 3
# Frames of agent audio to send per pacing tick.
FRAME_MS = 20


@dataclass
class TurnTiming:
    """One turn's latency, for the M1 harness."""

    caller_stopped_at: float | None = None
    last_speech_sent_at: float | None = None
    agent_first_audio_at: float | None = None

    @property
    def response_ms(self) -> int | None:
        if self.caller_stopped_at is None or self.agent_first_audio_at is None:
            return None
        return int((self.agent_first_audio_at - self.caller_stopped_at) * 1000)


@dataclass
class CallStats:
    turns: list[int] = field(default_factory=list)
    barge_ins: int = 0
    # Time from the last speech-bearing frame we forwarded to the model's
    # first audio back. This includes the model's own end-of-speech wait,
    # which is the dominant term and is configurable.
    model_rtt: list[int] = field(default_factory=list)

    def record(self, ms: int | None) -> None:
        if ms is not None and ms >= 0:
            self.turns.append(ms)

    def percentile(self, p: float) -> int | None:
        if not self.turns:
            return None
        return int(np.percentile(self.turns, p))

    def summary(self) -> dict[str, Any]:
        model_p50 = (
            int(np.percentile(self.model_rtt, 50)) if self.model_rtt else None
        )
        total_p50 = self.percentile(50)
        return {
            "turn_count": len(self.turns),
            "p50_response_ms": total_p50,
            "p95_response_ms": self.percentile(95),
            "p50_model_ms": model_p50,
            "p50_transport_ms": (
                total_p50 - model_p50
                if total_p50 is not None and model_p50 is not None
                else None
            ),
            "barge_ins": self.barge_ins,
        }


class MediaBridge:
    """Pumps audio between one Twilio call and one provider session."""

    def __init__(
        self,
        ws: Any,
        provider: Any,
        *,
        instructions: str = "",
        tools: list[dict] | None = None,
        on_event: Callable[[dict], Awaitable[None]] | None = None,
        max_call_seconds: int = 600,
        dispatch_tool: Callable[[str, dict], Awaitable[dict]] | None = None,
        tool_timeout_ms: int = 1200,
        connect_timeout_s: float = 10.0,
        greeting: str | None = None,
        stall_after_ms: int = 450,
    ):
        self.ws = ws
        self.provider = provider
        self.instructions = instructions
        self.tools = tools or []
        self.on_event = on_event
        self.max_call_seconds = max_call_seconds
        self.dispatch_tool = dispatch_tool
        self.tool_timeout_ms = tool_timeout_ms
        self.connect_timeout_s = connect_timeout_s
        # On an inbound call the agent speaks first. Without this both sides
        # wait for the other and the caller hears dead air, which reads as a
        # broken line rather than a silent agent.
        self.greeting = greeting
        # A tool slower than this gets the agent to say something. Dead air
        # is the single thing that makes a voice agent feel broken, and a
        # database round trip on a bad connection is easily half a second.
        self.stall_after_ms = stall_after_ms
        self.tool_calls: list[dict] = []
        self._tool_tasks: set[asyncio.Task] = set()

        self.stream_sid: str | None = None
        self.stats = CallStats()
        self.transcript: list[dict] = []

        self._outbound: asyncio.Queue[bytes] = asyncio.Queue()
        self._agent_speaking = False
        self._loud_frames = 0
        self._turn = TurnTiming()
        # Text of the current agent turn that has actually been sent to Twilio.
        self._spoken_this_turn = ""
        self._pending_this_turn = ""
        self._started_at = 0.0
        self._closing = False

    # ---------------------------------------------------------------- helpers

    async def _emit(self, payload: dict) -> None:
        if self.on_event:
            await self.on_event(payload)

    async def _send_to_twilio(self, ulaw: bytes) -> None:
        if not self.stream_sid:
            return
        await self.ws.send_text(
            json.dumps(
                {
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": base64.b64encode(ulaw).decode()},
                }
            )
        )

    async def _clear_twilio(self) -> None:
        """Flush audio Twilio has buffered but not yet played."""
        if not self.stream_sid:
            return
        await self.ws.send_text(
            json.dumps({"event": "clear", "streamSid": self.stream_sid})
        )

    def _drain_outbound(self) -> None:
        while not self._outbound.empty():
            try:
                self._outbound.get_nowait()
            except asyncio.QueueEmpty:
                break

    # ------------------------------------------------------------- barge-in

    async def _barge_in(self) -> None:
        """Caller talked over the agent. Stop everything at once."""
        self.stats.barge_ins += 1
        self._drain_outbound()
        await self._clear_twilio()
        await self.provider.interrupt()

        # Record only what the caller actually heard.
        heard = self._spoken_this_turn.strip()
        if heard or self._pending_this_turn.strip():
            self.transcript.append(
                {
                    "role": "agent",
                    "text": heard or "...",
                    "interrupted": True,
                    "unspoken": self._pending_this_turn.strip(),
                    "ts": time.time(),
                }
            )
        self._spoken_this_turn = ""
        self._pending_this_turn = ""
        self._agent_speaking = False
        self._loud_frames = 0
        await self._emit({"type": "barge_in"})

    # ------------------------------------------------------------- pump: in

    async def _phone_to_provider(self) -> None:
        """Twilio receive loop. Ends when the call ends."""
        async for raw in self.ws.iter_text():
            if self._closing:
                break
            msg = json.loads(raw)
            event = msg.get("event")

            if event == "start":
                self.stream_sid = msg["start"]["streamSid"]
                self._started_at = time.monotonic()
                await self._emit({"type": "status", "status": "live"})
                if self.greeting:
                    # The caller has just been connected. Nudge the model to
                    # open, rather than waiting for a caller who is waiting
                    # for it.
                    self._turn.caller_stopped_at = time.time()
                    await self.provider.send_text(self.greeting)

            elif event == "media":
                payload = base64.b64decode(msg["media"]["payload"])
                samples = A.ulaw_to_pcm16(payload)
                rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))

                if self._agent_speaking:
                    if rms > BARGE_RMS_THRESHOLD:
                        self._loud_frames += 1
                        if self._loud_frames >= BARGE_SUSTAIN_FRAMES:
                            await self._barge_in()
                    else:
                        self._loud_frames = 0
                else:
                    # Caller is talking; the clock for their turn keeps moving.
                    if rms > BARGE_RMS_THRESHOLD:
                        self._turn.caller_stopped_at = None
                    elif self._turn.caller_stopped_at is None:
                        self._turn.caller_stopped_at = time.time()

                await self.provider.send_audio(
                    A.resample(samples, 8000, self.provider.input_hz).tobytes()
                )
                # Only speech-bearing frames. Stamping every frame, silence
                # included, measured the gap to the most recent silent packet
                # and reported a meaningless single-digit millisecond figure.
                if rms > BARGE_RMS_THRESHOLD:
                    self._turn.last_speech_sent_at = time.time()

            elif event == "stop":
                break

    # ------------------------------------------------------------ pump: out

    async def _provider_to_phone(self) -> None:
        """Provider receive loop.

        The provider adapter guarantees this iterator survives turn
        boundaries. If it ever stops early the bridge goes deaf, which is the
        single worst failure mode this component has.
        """
        async for ev in self.provider.receive():
            if self._closing:
                break

            if ev.kind == "audio":
                if not self._agent_speaking:
                    self._agent_speaking = True
                    if self._turn.agent_first_audio_at is None:
                        self._turn.agent_first_audio_at = time.time()
                        ms = self._turn.response_ms
                        self.stats.record(ms)
                        if self._turn.last_speech_sent_at is not None:
                            self.stats.model_rtt.append(
                                int(
                                    (
                                        self._turn.agent_first_audio_at
                                        - self._turn.last_speech_sent_at
                                    )
                                    * 1000
                                )
                            )
                        if ms is not None:
                            await self._emit({"type": "latency", "ms": ms})
                ulaw = A.model_to_phone(ev.audio, self.provider.output_hz)
                chunks, _ = A.frames(ulaw)
                for c in chunks:
                    self._outbound.put_nowait(c)

            elif ev.kind == "transcript":
                self.transcript.append(
                    {"role": ev.role, "text": ev.text, "ts": time.time()}
                )
                if ev.role == "agent":
                    self._pending_this_turn += ev.text
                await self._emit({"type": "transcript", "role": ev.role, "text": ev.text})

            elif ev.kind == "tool_call":
                await self._emit(
                    {"type": "tool_call", "name": ev.tool_name, "args": ev.tool_args}
                )
                self.tool_calls.append({"name": ev.tool_name, "args": ev.tool_args})
                if self.dispatch_tool is not None:
                    # Run it as its own task so a slow tool cannot stall the
                    # audio pump. Silence is what makes an agent feel broken.
                    # The reference is held: an unreferenced task can be
                    # garbage collected mid-flight, which on a live call means
                    # a tool result that silently never arrives.
                    task = asyncio.create_task(self._run_tool(ev))
                    self._tool_tasks.add(task)
                    task.add_done_callback(self._tool_tasks.discard)

            elif ev.kind == "turn_end":
                self._agent_speaking = False
                self._spoken_this_turn += self._pending_this_turn
                self._pending_this_turn = ""
                self._turn = TurnTiming()
                self._spoken_this_turn = ""
                await self._emit({"type": "turn_end"})

            elif ev.kind == "error":
                await self._emit({"type": "error", "detail": ev.detail})
                break

    async def _run_tool(self, ev) -> None:
        """Execute one tool call and hand the result back to the model.

        Two deadlines, not one. The first is short: if the tool has not
        answered by then, the agent says something so the caller is not left
        in silence wondering whether the line dropped. The second is the real
        timeout, after which we give up and tell the model to offer a
        callback rather than stalling forever.
        """
        started = time.time()
        task = asyncio.ensure_future(self.dispatch_tool(ev.tool_name, ev.tool_args))
        try:
            result = await asyncio.wait_for(
                asyncio.shield(task), timeout=self.stall_after_ms / 1000
            )
        except TimeoutError:
            with contextlib.suppress(Exception):
                await self.provider.send_text(
                    "(That is taking a second. Tell the caller you are just "
                    "checking, in three or four words, then wait.)"
                )
            await self._emit({"type": "stalling", "name": ev.tool_name})
            remaining = max(0.1, (self.tool_timeout_ms - self.stall_after_ms) / 1000)
            try:
                result = await asyncio.wait_for(task, timeout=remaining)
            except TimeoutError:
                task.cancel()
                result = {"error": "that is taking too long, offer to call them back"}
                await self._emit({"type": "tool_slow", "name": ev.tool_name})
            except Exception as exc:
                result = {"error": "something went wrong on our end"}
                await self._emit({"type": "error", "detail": str(exc)})
        except Exception as exc:
            result = {"error": "something went wrong on our end"}
            await self._emit({"type": "error", "detail": str(exc)})

        took = int((time.time() - started) * 1000)
        await self._emit(
            {"type": "tool_result", "name": ev.tool_name, "ms": took, "result": result}
        )
        try:
            await self.provider.send_tool_result(ev.tool_call_id, ev.tool_name, result)
        except Exception as exc:
            await self._emit({"type": "error", "detail": f"tool result: {exc}"})

    async def _pace_outbound(self) -> None:
        """Forward agent audio to Twilio as fast as it arrives.

        This used to send one 20ms frame then sleep 20ms. That looks like
        correct real-time pacing and is not: asyncio.sleep overshoots by a
        millisecond or two every iteration, so a five second reply drifts
        several hundred milliseconds late, and the backlog compounds across
        turns until later replies feel like they never came.

        Pacing was never needed. Twilio buffers inbound media and plays it
        out itself, and barge-in works by flushing that buffer with `clear`,
        not by us withholding frames.
        """
        while not self._closing:
            try:
                frame = await asyncio.wait_for(self._outbound.get(), timeout=0.1)
            except TimeoutError:
                continue
            await self._send_to_twilio(frame)
            self._spoken_this_turn = self._pending_this_turn
            # Drain whatever else is ready without waiting on the clock.
            while True:
                try:
                    await self._send_to_twilio(self._outbound.get_nowait())
                except asyncio.QueueEmpty:
                    break
            # Yield so the inbound pump and tool tasks are not starved.
            await asyncio.sleep(0)

    async def _watchdog(self) -> None:
        """Hard cap on call length. A runaway call is a runaway bill."""
        while not self._closing:
            await asyncio.sleep(1)
            if self._started_at and (
                time.monotonic() - self._started_at > self.max_call_seconds
            ):
                await self._emit({"type": "status", "status": "max_duration"})
                self._closing = True
                break

    # ------------------------------------------------------------------ run

    async def run(self) -> dict:
        # A hanging connect is the worst failure mode here: the websocket is
        # open, no exception is raised, the logs look healthy, and the caller
        # hears nothing at all. Bound it so it becomes a visible error.
        try:
            await asyncio.wait_for(
                self.provider.connect(
                    instructions=self.instructions, tools=self.tools
                ),
                timeout=self.connect_timeout_s,
            )
        except TimeoutError:
            log.error(
                "the speech provider did not open a session within %ss; "
                "the caller is hearing silence",
                self.connect_timeout_s,
            )
            await self._emit({"type": "error", "detail": "provider connect timed out"})
            await self.provider.close()
            return self.stats.summary()
        except Exception as exc:
            log.exception("the speech provider failed to open a session")
            await self._emit({"type": "error", "detail": f"{type(exc).__name__}: {exc}"})
            await self.provider.close()
            return self.stats.summary()
        log.info("speech session open; bridging call audio")
        tasks = [
            asyncio.create_task(self._provider_to_phone()),
            asyncio.create_task(self._pace_outbound()),
            asyncio.create_task(self._watchdog()),
        ]
        try:
            await self._phone_to_provider()
        finally:
            self._closing = True
            # Let in-flight tools finish briefly so a confirmed order is not
            # abandoned halfway through firing.
            if self._tool_tasks:
                await asyncio.wait(self._tool_tasks, timeout=2)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.provider.close()
        return self.stats.summary()
SVEOF29

cat > app/telephony/twilio_webhook.py << 'SVEOF30'
"""Twilio inbound webhook and tenant resolution.

Signature validation is not optional. The stream URL is public, and without
validation anyone who finds it can make your agent talk and burn your minutes.
"""

from __future__ import annotations

from twilio.request_validator import RequestValidator


def public_url(request) -> str:
    """The URL Twilio actually signed.

    Behind a proxy (Codespaces port forwarding, Fly, any load balancer) the
    app sees http:// and an internal host, while Twilio signed the public
    https:// URL. Validating against the internal one fails every time and
    surfaces as a 403 with no explanation.
    """
    url = str(request.url)
    proto = request.headers.get("x-forwarded-proto")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if proto:
        url = url.replace(f"{request.url.scheme}://", f"{proto}://", 1)
    if host and host != request.url.netloc:
        url = url.replace(request.url.netloc, host, 1)
    return url


def validate_twilio_signature(
    auth_token: str, url: str, params: dict[str, str], signature: str
) -> bool:
    """Verify the request actually came from Twilio."""
    if not signature:
        return False
    return RequestValidator(auth_token).validate(url, params, signature)


def connect_stream_twiml(ws_url: str, greeting: str | None = None) -> str:
    """TwiML that hands the call to our media stream.

    <Connect><Stream> is bidirectional, unlike <Start><Stream> which is
    listen-only. Getting this wrong means the caller can hear nothing and the
    logs look completely healthy.
    """
    say = f"<Say>{greeting}</Say>" if greeting else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response>{say}"
        f'<Connect><Stream url="{ws_url}" /></Connect>'
        "</Response>"
    )
SVEOF30

cat > pytest.ini << 'SVEOF31'
[pytest]
asyncio_mode = auto
testpaths = tests
SVEOF31

cat > questions.txt << 'SVEOF32'
# What a parent actually asks in the first ninety seconds of a call.
# Run: python scripts/eval.py
# Lines starting with # are ignored.

# --- should hit the brief, no lookup needed ---
what does RobotiX Institute do
where are you located
what is the phone number
how much do the classes cost
what ages do you teach

# --- should hit a lookup, one clear source ---
how much is the Python coding class
how much is VEX V5 for high school
what age is LEGO Robotics Basic for
what is the difference between LEGO basic and LEGO advanced
how big are the classes
how long is each session
do you provide the robots or do we bring our own
does my child need any experience
can we try a class before paying
what happens if we miss a class
can my child move up to a harder class later
what is the address in Murfreesboro
what is the address in Brentwood
when is the summer camp
what do you do at the fall camp
are the instructors qualified
what is the difference between a camp and a regular program
can I open my own centre

# --- the ones the website does not answer ---
# Without data/facts.md these must all MISS. That is correct behaviour and
# it is exactly why the facts file exists.
what days are the classes
what time is the Saturday class
what are your opening hours
do you offer sibling discounts
do you take walk-ins

# --- must MISS no matter what. If any of these match, MIN_SCORE is too low ---
do you sell used cars
what is your refund policy for hotel bookings
can you fix my laptop
SVEOF32

cat > railway.json << 'SVEOF33'
{
  "$schema": "https://railway.app/railway.schema.json",
  "build": { "builder": "DOCKERFILE", "dockerfilePath": "Dockerfile" },
  "deploy": {
    "healthcheckPath": "/health",
    "healthcheckTimeout": 30,
    "restartPolicyType": "ON_FAILURE",
    "numReplicas": 1
  }
}
SVEOF33

cat > requirements.txt << 'SVEOF34'
fastapi==0.115.6
uvicorn[standard]==0.34.0
websockets==14.1
httpx==0.28.1
selectolax==0.3.27
numpy==2.2.1
scipy==1.15.0
google-genai==1.2.0
twilio==9.4.1
pydantic-settings==2.7.1
python-multipart==0.0.20
pytest==8.3.4
pytest-asyncio==0.25.0
SVEOF34

cat > scripts/check.py << 'SVEOF35'
#!/usr/bin/env python3
"""Pre-package checks: the mistakes that have actually cost us hours.

Parses rather than greps, so a comment explaining a bug is not mistaken for
the bug. Carried over from the restaurant build, plus two checks specific to
this demo.
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"
TESTS = ROOT / "tests"

failures: list[str] = []


def say(label: str, ok: bool, detail: str = "") -> None:
    print(f"{label:<54} {'ok' if ok else 'FAIL'}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


def fixed_depth_parents() -> list[str]:
    """`parents[3]` on /srv/app/main.py raises IndexError at import and the
    container dies before serving anything. Walk ancestors instead."""
    hits = []
    for path in APP.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "parents"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, int)
            ):
                hits.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return hits


def paced_sleep_in_bridge() -> list[str]:
    """Sending one frame then sleeping 20ms looks like correct real-time
    pacing and is not: asyncio.sleep overshoots every iteration, so a long
    reply drifts hundreds of milliseconds late and the backlog compounds.
    Twilio buffers inbound media itself. Any nonzero sleep in the outbound
    pump is this bug coming back."""
    src = (APP / "telephony" / "bridge.py").read_text()
    tree = ast.parse(src)
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.AsyncFunctionDef) and node.name == "_pace_outbound"):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "sleep"
                and inner.args
                and not (
                    isinstance(inner.args[0], ast.Constant) and inner.args[0].value == 0
                )
            ):
                hits.append(f"bridge.py:{inner.lineno}")
    return hits


def audio_read_from_response_data() -> bool:
    """Gemini Live puts audio on `response.data`. Walking
    server_content.model_turn.parts[].inline_data instead yields a session
    that transcribes correctly and plays nothing."""
    return "response, \"data\"" in (APP / "providers" / "gemini.py").read_text().replace(
        "'", '"'
    )


def cli_signature_matches() -> list[str]:
    """`main()` calling `run()` with an argument it does not accept parses
    fine, passes --help, and dies only when someone runs the command. A rename
    that made one patch miss silently is how that shipped once already."""
    tree = ast.parse((APP / "ingest" / "__main__.py").read_text())
    accepted: set[str] = set()
    passed: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run":
            args = node.args
            accepted = {a.arg for a in args.args + args.kwonlyargs}
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run"
        ):
            passed = {kw.arg for kw in node.keywords if kw.arg}
    return sorted(passed - accepted)


def no_live_model_in_tests() -> bool:
    """The suite must never call a live model: slow, costs money per run, and
    the result depends on what the model felt like returning."""
    return 'os.environ["GEMINI_API_KEY"] = ""' in (TESTS / "conftest.py").read_text()


def db_not_committed() -> list[str]:
    """The SQLite file ships inside the image, so a stale one silently
    deploys yesterday's crawl. It must be built by ingest, not by git."""
    ignore = (ROOT / ".gitignore").read_text()
    return [] if "data/*.db" in ignore else ["data/*.db is not gitignored"]


if __name__ == "__main__":
    hits = fixed_depth_parents()
    say("no fixed-depth parents[N]", not hits, " ".join(hits))
    hits = paced_sleep_in_bridge()
    say("no reintroduced pacing sleep", not hits, " ".join(hits))
    say("gemini audio read from response.data", audio_read_from_response_data())
    hits = cli_signature_matches()
    say("ingest CLI signature matches its caller", not hits, " ".join(hits))
    say("tests never reach a live model", no_live_model_in_tests())
    hits = db_not_committed()
    say("crawled db is gitignored", not hits, " ".join(hits))
    print()
    if failures:
        print(f"{len(failures)} check(s) failed")
        sys.exit(1)
    print("all checks passed")
SVEOF35

cat > scripts/eval.py << 'SVEOF36'
"""Score retrieval without a phone.

Run this before dialling anything. It is the cheapest way to find out whether
the crawl actually covers the questions a caller will ask, and to set
MIN_SCORE. Tuning that threshold on live calls wastes an afternoon.

    python scripts/eval.py                      # uses questions.txt if present
    python scripts/eval.py "what are your hours" "do you ship to canada"
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.embed import embed_query  # noqa: E402
from app.retrieval import Index  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.store import connect, site_row  # noqa: E402

DEFAULT = [
    "what does this company do",
    "where are you located",
    "what are your opening hours",
    "how much does it cost",
    "how do I contact a person",
    "who founded the company",
    "do you offer refunds",
]


async def main() -> int:
    settings = get_settings()
    conn = connect(settings.site_db)
    site = site_row(conn)
    if not site:
        print("no site ingested. run: python -m app.ingest <url>")
        return 1

    questions = sys.argv[1:]
    qfile = Path("questions.txt")
    if not questions and qfile.exists():
        questions = [
            q.strip()
            for q in qfile.read_text().splitlines()
            if q.strip() and not q.strip().startswith("#")
        ]
    questions = questions or DEFAULT

    index = Index(conn, site["dims"])
    print(f"{site['name']}  ·  {site['page_count']} pages  ·  {index.size} chunks")
    print(f"threshold {settings.min_score}\n")

    misses = 0
    for question in questions:
        vec = await embed_query(
            question,
            api_key=settings.gemini_api_key,
            model=settings.embedding_model,
            dims=settings.embedding_dims,
        )
        hits = index.search(vec, top_k=settings.top_k, min_score=settings.min_score)
        if not hits:
            misses += 1
            best = index.search(vec, top_k=1, min_score=0.0)
            near = f"  best was {best[0].score:.2f} on {best[0].url}" if best else ""
            print(f"MISS  {question}{near}")
        else:
            print(f"{hits[0].score:.2f}  {question}")
            for hit in hits:
                print(f"        {hit.score:.2f}  {hit.heading or hit.title}  {hit.url}")
            # A cluster of near-identical scores across different pages means
            # the same block was indexed on all of them, which is chrome the
            # crawl failed to strip rather than a genuine spread of answers.
            if len(hits) > 2 and hits[0].score - hits[-1].score < 0.02:
                print("        ^ near-identical scores across pages: likely "
                      "boilerplate that survived the crawl")
        print()

    print(f"{len(questions) - misses}/{len(questions)} answered from the crawl")
    if misses:
        print("For each miss: is the answer actually on the site? If yes, raise "
              "MAX_PAGES or lower MIN_SCORE. If no, that is correct behaviour and "
              "the agent will offer to take a message.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
SVEOF36

cat > scripts/netcheck.py << 'SVEOF37'
#!/usr/bin/env python3
"""Find out which layer is refusing a connection.

A ConnectTimeout from the crawler has three plausible causes and they need
different fixes:

1. The container has an IPv6 address but no working IPv6 route. httpx has no
   Happy Eyeballs, so it picks the AAAA record and hangs until timeout, while
   curl falls back to IPv4 in milliseconds and appears to work fine. This is
   the usual answer inside Codespaces.
2. The site's firewall silently drops traffic from cloud IP ranges. Packets
   disappear rather than being rejected, which also reads as a timeout.
3. Egress from this machine is filtered.

    python scripts/netcheck.py https://www.rxiedu.com/
"""
from __future__ import annotations

import asyncio
import socket
import sys
import time
from urllib.parse import urlparse


def resolve(host: str, family: int) -> list[str]:
    try:
        return sorted({i[4][0] for i in socket.getaddrinfo(host, 443, family)})
    except socket.gaierror:
        return []


def tcp(addr: str, family: int, timeout: float = 6.0) -> str:
    try:
        s = socket.socket(family, socket.SOCK_STREAM)
    except OSError as exc:
        # Errno 97 here means the container has no stack for this family at
        # all, which is itself the answer.
        return f"unavailable ({exc.strerror})"
    s.settimeout(timeout)
    started = time.time()
    try:
        s.connect((addr, 443))
        return f"open in {int((time.time() - started) * 1000)}ms"
    except socket.timeout:
        return "TIMED OUT (packets dropped)"
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        s.close()


async def via_httpx(url: str, force_ipv4: bool) -> str:
    import httpx

    transport = httpx.AsyncHTTPTransport(
        local_address="0.0.0.0" if force_ipv4 else None
    )
    started = time.time()
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=10.0, follow_redirects=True
        ) as c:
            r = await c.get(url)
        return f"{r.status_code} in {int((time.time() - started) * 1000)}ms"
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


async def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.rxiedu.com/"
    host = urlparse(url).hostname or url
    print(f"host: {host}\n")

    v4 = resolve(host, socket.AF_INET)
    v6 = resolve(host, socket.AF_INET6)
    print(f"  A     records    {', '.join(v4) or 'none'}")
    print(f"  AAAA  records    {', '.join(v6) or 'none'}")

    for addr in v4[:2]:
        print(f"  tcp v4 {addr:<18} {tcp(addr, socket.AF_INET)}")
    for addr in v6[:2]:
        print(f"  tcp v6 {addr:<18} {tcp(addr, socket.AF_INET6)}")

    print(f"\n  httpx default    {await via_httpx(url, force_ipv4=False)}")
    print(f"  httpx ipv4 only  {await via_httpx(url, force_ipv4=True)}")

    print("\nreading the result:")
    print("  ipv4 only works, default does not  -> broken IPv6 route. The")
    print("     crawler retries on IPv4 automatically; nothing to do.")
    print("  both fail, tcp v4 times out        -> the site drops traffic from")
    print("     this network. Crawl from your laptop and commit data/site.db.")
    print("  no A records at all                -> the hostname is wrong.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
SVEOF37

cat > scripts/offline_crawl.py << 'SVEOF38'
#!/usr/bin/env python3
"""Crawl a site using nothing but the Python standard library.

For the machine that can reach the site but has none of this project set up.
No pip install, no virtualenv, no git, no API key. Copy this one file across,
run it, and hand the resulting folder back.

    python3 offline_crawl.py https://www.rxiedu.com/

Writes ./pages/ with one HTML file per page plus manifest.json, which is
exactly what `python -m app.ingest --from-dir` expects.

Deliberately kept to the stdlib and to one file. Anything that needs
installing is one more thing to go wrong on a machine you are not sitting at.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
SKIP_EXT = re.compile(
    r"\.(jpg|jpeg|png|gif|svg|webp|ico|css|js|mjs|woff2?|ttf|eot|zip|gz|tar|"
    r"mp4|mp3|wav|avi|mov|pdf|doc|docx|xls|xlsx|ppt|pptx)$",
    re.I,
)
SKIP_PATH = re.compile(
    r"/(wp-json|wp-admin|cdn-cgi|feed|rss|tag|author|cart|checkout|login|"
    r"signin|signup|account)(/|$)",
    re.I,
)
SKIP_QUERY = re.compile(r"(^|&)(tag|author|page|paged|s|q|sort|filter)=", re.I)
LINK_RE = re.compile(r"""<a\s[^>]*href\s*=\s*["']([^"'>]+)["']""", re.I)
LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
SAFE = re.compile(r"[^a-z0-9._-]+")

MAX_PAGES = 120
MAX_DEPTH = 3
DELAY_S = 0.3


def strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def normalise(url: str) -> str:
    url, _ = urldefrag(url)
    p = urlparse(url)
    scheme = (p.scheme or "https").lower()
    host = p.netloc.lower()
    path = p.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query = "&".join(
        sorted(
            q
            for q in p.query.split("&")
            if q and not q.split("=")[0].lower().startswith(("utm_", "fbclid", "gclid"))
        )
    )
    return f"{scheme}://{host}{path}" + (f"?{query}" if query else "")


def key(url: str) -> str:
    p = urlparse(normalise(url))
    return strip_www(p.netloc) + p.path + (f"?{p.query}" if p.query else "")


def same_site(url: str, root: str) -> bool:
    return strip_www(urlparse(normalise(url)).netloc) == strip_www(
        urlparse(normalise(root)).netloc
    )


def crawlable(url: str) -> bool:
    p = urlparse(url)
    return (
        p.scheme in ("http", "https")
        and not SKIP_EXT.search(p.path)
        and not SKIP_PATH.search(p.path)
        and not (p.query and SKIP_QUERY.search(p.query))
    )


def get(url: str, timeout: float = 20.0):
    """Return (final_url, content_type, text) or raise."""
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept-Encoding": "gzip", "Accept": "text/html"}
    )
    # Some small hosts have a partial certificate chain. A crawl that dies on
    # that is more annoying than the risk it avoids on a public marketing site.
    ctx = ssl.create_default_context()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
    except ssl.SSLError:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        print("  note: certificate could not be verified, continuing anyway")
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    charset = resp.headers.get_content_charset() or "utf-8"
    return resp.geturl(), resp.headers.get("Content-Type", ""), raw.decode(
        charset, errors="replace"
    )


def filename(url: str) -> str:
    slug = SAFE.sub("-", url.split("://", 1)[-1].lower()).strip("-")[:80] or "page"
    return f"{slug}-{hashlib.sha1(url.encode()).hexdigest()[:8]}.html"


def sitemap_seeds(root: str, limit: int) -> list[str]:
    found, seen_maps = [], set()
    queue = [urljoin(root, "/sitemap.xml"), urljoin(root, "/sitemap_index.xml")]
    while queue and len(found) < limit:
        target = queue.pop(0)
        if target in seen_maps:
            continue
        seen_maps.add(target)
        try:
            _, ctype, text = get(target)
        except Exception:
            continue
        if "<loc" not in text.lower():
            continue
        for loc in (normalise(u) for u in LOC_RE.findall(text)):
            if loc.endswith(".xml") and len(seen_maps) < 12:
                queue.append(loc)
            elif same_site(loc, root) and crawlable(loc):
                found.append(loc)
    ordered, seen = [], set()
    for u in found:
        if key(u) not in seen:
            seen.add(key(u))
            ordered.append(u)
    return ordered[:limit]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    root = normalise(sys.argv[1])
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "pages")

    print(f"crawling {root}")
    try:
        final, ctype, html = get(root)
    except urllib.error.HTTPError as exc:
        print(f"the root returned HTTP {exc.code}. Nothing to crawl.")
        return 1
    except Exception as exc:
        print(f"could not reach the root: {type(exc).__name__}: {exc}")
        print("If this machine also cannot reach it, try a different network.")
        return 1
    if "html" not in ctype.lower():
        print(f"the root is {ctype}, not HTML.")
        return 1

    root = normalise(final)
    pages = [(root, html)]
    seen = {key(root)}
    frontier = [(u, 1) for u in sitemap_seeds(root, MAX_PAGES)]
    frontier += [
        (normalise(urljoin(root, h)), 1)
        for h in LINK_RE.findall(html)
        if not h.startswith(("mailto:", "tel:", "javascript:", "#"))
    ]
    print(f"  [  1] {root}")

    while frontier and len(pages) < MAX_PAGES:
        url, depth = frontier.pop(0)
        if key(url) in seen or not same_site(url, root) or not crawlable(url):
            continue
        seen.add(key(url))
        time.sleep(DELAY_S)
        try:
            final, ctype, body = get(url)
        except Exception as exc:
            print(f"       skip {url}  ({type(exc).__name__})")
            continue
        if "html" not in ctype.lower():
            continue
        # A link can redirect off-site: a payment link lands on the provider's
        # domain. Archiving that makes the agent answer for the wrong company.
        if not same_site(normalise(final), root):
            print(f"       skip {url}  (redirects off-site)")
            continue
        pages.append((normalise(final), body))
        print(f"  [{len(pages):3d}] {url}")
        if depth < MAX_DEPTH:
            for h in LINK_RE.findall(body):
                if h.startswith(("mailto:", "tel:", "javascript:", "#")):
                    continue
                link = normalise(urljoin(url, h))
                if key(link) not in seen and same_site(link, root):
                    frontier.append((link, depth + 1))

    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.html"):
        old.unlink()
    manifest = []
    for url, body in pages:
        name = filename(url)
        (out / name).write_text(body, encoding="utf-8")
        manifest.append({"url": url, "file": name, "status": 200})
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))

    bundle = out.with_suffix(".zip")
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.iterdir()):
            z.write(f, f"{out.name}/{f.name}")

    print(f"\n{len(pages)} pages saved to {out.resolve()}")
    print(f"zipped to {bundle.resolve()}")
    print("\nDrag that zip into the Codespaces file explorer, then run:")
    print(f"  unzip -o {bundle.name} -d data/ && python -m app.ingest --from-dir data/{out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
SVEOF38

cat > scripts/webhook.py << 'SVEOF39'
"""Point the Twilio number at this demo, and put it back afterwards.

The number is shared with the restaurant build, so every swap is recorded to
.webhook-backup.json before it happens. `restore` puts back whatever was there
the first time you ran `point`, so the restaurant demo is always one command
from working again.

    python scripts/webhook.py status
    python scripts/webhook.py point https://site-voice.up.railway.app
    python scripts/webhook.py restore
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from twilio.rest import Client

BACKUP = Path(__file__).resolve().parent.parent / ".webhook-backup.json"


def client() -> Client:
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        sys.exit("set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN first")
    return Client(sid, token)


def number(cli: Client):
    target = os.environ.get("TWILIO_NUMBER")
    if not target:
        sys.exit("set TWILIO_NUMBER, for example +16155551234")
    found = cli.incoming_phone_numbers.list(phone_number=target, limit=1)
    if not found:
        sys.exit(f"{target} is not on this Twilio account")
    return found[0]


def status() -> None:
    num = number(client())
    print(f"number      {num.phone_number}")
    print(f"voice url   {num.voice_url or '(none)'}")
    print(f"method      {num.voice_method}")
    if BACKUP.exists():
        saved = json.loads(BACKUP.read_text())
        print(f"backup      {saved['voice_url'] or '(none)'}  saved {saved['saved_at']}")
    else:
        print("backup      none recorded yet")


def point(base: str) -> None:
    from datetime import datetime, timezone

    cli = client()
    num = number(cli)
    if not BACKUP.exists():
        BACKUP.write_text(
            json.dumps(
                {
                    "phone_number": num.phone_number,
                    "voice_url": num.voice_url,
                    "voice_method": num.voice_method,
                    "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                },
                indent=2,
            )
        )
        print(f"backed up {num.voice_url or '(none)'} -> {BACKUP.name}")
    else:
        print(f"backup already exists, leaving it alone ({BACKUP.name})")

    url = base.rstrip("/") + "/twilio/voice"
    num.update(voice_url=url, voice_method="POST")
    print(f"pointed {num.phone_number} at {url}")


def restore() -> None:
    if not BACKUP.exists():
        sys.exit("no backup recorded. nothing to restore.")
    saved = json.loads(BACKUP.read_text())
    num = number(client())
    num.update(voice_url=saved["voice_url"], voice_method=saved["voice_method"] or "POST")
    print(f"restored {num.phone_number} to {saved['voice_url']}")
    print("delete .webhook-backup.json if you want the next `point` to re-record.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "status":
        status()
    elif cmd == "point":
        if len(sys.argv) < 3:
            sys.exit("usage: webhook.py point https://your-host")
        point(sys.argv[2])
    elif cmd == "restore":
        restore()
    else:
        sys.exit(__doc__)
SVEOF39

cat > tests/__init__.py << 'SVEOF40'

SVEOF40

cat > tests/conftest.py << 'SVEOF41'
"""Test environment.

Settings are read once and cached, so these must be set before anything
imports `app.config`. Putting them here rather than in a test module keeps
import order out of the tests themselves.
"""

import os
import warnings

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("REALTIME_PROVIDER", "mock")
os.environ.setdefault("PUBLIC_BASE_URL", "https://demo.test")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")
# No real model calls from the suite: slow, costs money per run, and the
# result depends on what the model felt like returning.
os.environ["GEMINI_API_KEY"] = ""

warnings.filterwarnings("ignore", category=DeprecationWarning)
SVEOF41

cat > tests/test_agent.py << 'SVEOF42'
"""Prompt assembly and tool dispatch."""
import pytest

from app import agent as agent_mod
from app.retrieval import Index
from app.store import connect, pack


def test_prompt_states_the_crawl_date_and_the_current_time():
    """Without the current date the model guesses what day it is and answers
    'are you open tomorrow' with confidence."""
    text = agent_mod.build(name="Acme", brief="Acme roofs things.",
                           crawled_at="9 August 2026", tz="America/Chicago")
    assert "Acme" in text
    assert "9 August 2026" in text
    assert "America/Chicago" in text
    assert "Acme roofs things." in text


def test_prompt_says_what_to_do_with_an_empty_brief():
    text = agent_mod.build(name="", brief="", crawled_at="today")
    assert "lookup_site for everything" in text


def test_greeting_is_a_stage_direction_not_dialogue():
    """The model reads dialogue out verbatim, brackets and all."""
    assert agent_mod.greeting("Acme").startswith("(")


def test_tool_schemas_require_a_self_contained_question():
    lookup = next(t for t in agent_mod.TOOL_SCHEMAS if t["name"] == "lookup_site")
    assert lookup["parameters"]["required"] == ["question"]
    assert "pronouns" in lookup["parameters"]["properties"]["question"]["description"]


class Settings:
    gemini_api_key = "x"
    embedding_model = "m"
    embedding_dims = 3
    top_k = 4
    min_score = 0.5


@pytest.fixture()
def index(tmp_path):
    conn = connect(tmp_path / "s.db")
    conn.execute("INSERT INTO pages (url,title) VALUES ('https://x/pricing','P')")
    conn.execute(
        "INSERT INTO chunks (id,url,title,heading,text,embedding) VALUES (1,?,?,?,?,?)",
        ("https://x/pricing", "P", "Pricing", "forty dollars a month" * 8, pack([1.0, 0, 0])),
    )
    conn.commit()
    return Index(conn, 3)


@pytest.mark.asyncio
async def test_a_failed_embed_never_becomes_an_invented_answer(index, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("network")

    monkeypatch.setattr("app.embed.embed_query", boom)
    d = agent_mod.ToolDispatcher(index=index, settings=Settings())
    out = await d.dispatch("lookup_site", {"question": "how much"})
    assert out["found"] is False
    assert "message" in out["hint"]


@pytest.mark.asyncio
async def test_a_hit_reports_its_source(index, monkeypatch):
    async def vec(*a, **k):
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr("app.embed.embed_query", vec)
    seen = []
    d = agent_mod.ToolDispatcher(index=index, settings=Settings(), on_sources=seen.extend)
    out = await d.dispatch("lookup_site", {"question": "how much"})
    assert out["found"] is True
    assert out["passages"][0]["source"] == "https://x/pricing"
    assert seen == ["https://x/pricing"]


@pytest.mark.asyncio
async def test_an_empty_question_is_a_miss_not_a_crash(index):
    d = agent_mod.ToolDispatcher(index=index, settings=Settings())
    assert (await d.dispatch("lookup_site", {}))["found"] is False


@pytest.mark.asyncio
async def test_take_message_is_recorded(index):
    d = agent_mod.ToolDispatcher(index=index, settings=Settings())
    out = await d.dispatch("take_message", {"caller_name": "Sam", "message": "call back"})
    assert out["saved"] is True
    assert d.messages[0]["caller_name"] == "Sam"


def test_provided_facts_are_labelled_as_not_from_the_website():
    """The agent must never claim a supplied fact is 'on their pricing page'."""
    text = agent_mod.build(
        name="Acme", brief="Acme roofs things.", crawled_at="today",
        facts="## Hours\nSaturdays 10am.",
    )
    assert "Saturdays 10am." in text
    assert "not on the website" in text
    assert "never say they came from a web page" in text


def test_no_facts_means_no_dangling_header():
    text = agent_mod.build(name="Acme", brief="b", crawled_at="today", facts="")
    assert "FACTS THE BUSINESS PROVIDED" not in text
SVEOF42

cat > tests/test_archive.py << 'SVEOF43'
"""Crawl on one machine, embed on another.

Needed because some hosts null-route cloud IP ranges, so the machine that can
reach the site is not the machine that runs the demo.
"""
import json

import pytest

from app.ingest import archive
from app.ingest.crawl import Page


def pages():
    return [
        Page(url="https://x.test/", status=200, html="<html>root</html>"),
        Page(url="https://x.test/about", status=200, html="<html>about</html>"),
    ]


def test_roundtrip_preserves_url_and_html(tmp_path):
    archive.save(pages(), tmp_path)
    back = archive.load(tmp_path)
    assert [(p.url, p.html) for p in back] == [(p.url, p.html) for p in pages()]


def test_filenames_are_readable_and_collision_proof():
    a = archive.filename("https://x.test/a/b")
    b = archive.filename("https://x.test/a-b")
    assert a != b, "two urls that slugify alike must not overwrite each other"
    assert "x.test" in a and a.endswith(".html")
    assert archive.filename("https://x.test/a/b") == a, "must be stable"


def test_a_stale_archive_is_replaced_not_merged(tmp_path):
    archive.save(pages(), tmp_path)
    archive.save(pages()[:1], tmp_path)
    assert len(archive.load(tmp_path)) == 1
    assert len(list(tmp_path.glob("*.html"))) == 1


def test_a_directory_without_a_manifest_says_so(tmp_path):
    (tmp_path / "loose.html").write_text("<html></html>")
    with pytest.raises(FileNotFoundError, match="manifest"):
        archive.load(tmp_path)


def test_a_missing_file_named_in_the_manifest_is_skipped(tmp_path):
    archive.save(pages(), tmp_path)
    entries = json.loads((tmp_path / archive.MANIFEST).read_text())
    (tmp_path / entries[0]["file"]).unlink()
    assert len(archive.load(tmp_path)) == 1


def test_the_standalone_crawler_needs_only_the_stdlib():
    """It runs on a laptop with nothing installed. A stray third-party import
    would only be discovered on the machine you are not sitting at."""
    import ast
    import pathlib
    import sys

    source = pathlib.Path("scripts/offline_crawl.py").read_text()
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= sys.stdlib_module_names, (
        f"non-stdlib imports: {sorted(imported - sys.stdlib_module_names)}"
    )


def test_both_crawlers_agree_on_filenames():
    """The two implementations must produce the same archive layout, or an
    archive from one will not load in the other."""
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location(
        "offline_crawl", pathlib.Path("scripts/offline_crawl.py")
    )
    offline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(offline)

    for url in (
        "https://www.rxiedu.com/",
        "https://www.rxiedu.com/Programs/lego-basic",
        "https://x.test/a/b",
    ):
        assert offline.filename(url) == archive.filename(url)
SVEOF43

cat > tests/test_audio.py << 'SVEOF44'
"""Codec correctness against the reference implementation.

audioop is gone in 3.13, so this pins the numpy tables to the standard
library's output while it still exists to compare against.
"""
import numpy as np
import pytest

from app import audio as A


def test_encode_matches_audioop_across_the_full_range():
    audioop = pytest.importorskip("audioop")
    samples = np.arange(-32768, 32768, dtype=np.int16)
    assert A.pcm16_to_ulaw(samples) == audioop.lin2ulaw(samples.tobytes(), 2)


def test_decode_matches_audioop_for_every_byte():
    audioop = pytest.importorskip("audioop")
    payload = bytes(range(256))
    expected = np.frombuffer(audioop.ulaw2lin(payload, 2), dtype=np.int16)
    assert np.array_equal(A.ulaw_to_pcm16(payload), expected)


def test_negative_samples_round_away_from_zero():
    """The `abs(s >> 2) << 2` case. A naive abs() disagrees on 381 values."""
    audioop = pytest.importorskip("audioop")
    tricky = np.array([-1, -2, -3, -4, -5, -33, -129], dtype=np.int16)
    assert A.pcm16_to_ulaw(tricky) == audioop.lin2ulaw(tricky.tobytes(), 2)


def test_twilio_frame_expands_to_model_rate():
    payload = b"\xff" * 160  # 20 ms
    assert len(A.phone_to_model(payload, 16000)) == 640


def test_model_output_collapses_to_whole_frames():
    pcm = b"\x00\x00" * 2400  # 100 ms at 24 kHz
    ulaw = A.model_to_phone(pcm, 24000)
    chunks, remainder = A.frames(ulaw)
    assert len(chunks) == 5
    assert remainder == b""
    assert all(len(c) == 160 for c in chunks)


def test_frames_returns_the_remainder_rather_than_padding():
    chunks, remainder = A.frames(b"\x00" * 170)
    assert len(chunks) == 1 and len(remainder) == 10
SVEOF44

cat > tests/test_boilerplate.py << 'SVEOF45'
"""Cross-page chrome removal."""
from app.ingest.boilerplate import repeated_lines, strip_repeats


class Doc:
    def __init__(self, url, text):
        self.url, self.text = url, text

    @property
    def words(self):
        return len(self.text.split())


TESTIMONIAL = "My son loves coming here. Great place for kids."
CARD = "VEX IQ Basic Ages 8-10 years $179 / 4 week"


def pages(n=10):
    return [
        Doc(f"https://x/p{i}", f"## Page {i}\nUnique body sentence for page {i}.\n"
                              f"{TESTIMONIAL}\n{CARD}")
        for i in range(n)
    ]


def test_blocks_repeated_on_every_page_are_chrome():
    chrome = repeated_lines(pages())
    assert TESTIMONIAL in chrome
    assert CARD in chrome


def test_unique_content_survives():
    docs, removed = strip_repeats(pages())
    assert removed == 20
    for i, doc in enumerate(docs):
        assert f"Unique body sentence for page {i}." in doc.text
        assert TESTIMONIAL not in doc.text


def test_headings_are_kept_even_when_repeated():
    """A chunk with no heading loses the label used to cite the answer."""
    docs = [Doc(f"https://x/{i}", "## Frequently Asked Questions\nbody " + str(i))
            for i in range(8)]
    docs, _ = strip_repeats(docs)
    assert all("## Frequently Asked Questions" in d.text for d in docs)


def test_small_crawls_are_left_alone():
    """On three pages, two hits looks like boilerplate and usually is not."""
    assert repeated_lines(pages(3)) == set()


def test_a_price_card_cannot_leak_across_products():
    """Asking about one product must not retrieve another product's card."""
    docs, _ = strip_repeats(pages())
    assert not any(CARD in d.text for d in docs)


def test_facts_file_ignores_comment_only_content():
    from app.facts import for_prompt, load

    import tempfile
    import pathlib

    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "facts.md"
        p.write_text("<!-- just a comment -->\n<!-- another -->\n")
        assert load(p) == ""
        assert for_prompt(load(p)) == ""
        assert load(pathlib.Path(d) / "missing.md") == ""
SVEOF45

cat > tests/test_bridge.py << 'SVEOF46'
"""Bridge behaviour, driven entirely by the mock provider.

No network, no API key, no phone. These are the tests that have to hold for
the demo not to embarrass anyone.
"""
import asyncio
import base64
import json

import numpy as np
import pytest

from app import audio as A
from app.providers.base import ProviderEvent
from app.providers.mock import MockProvider, tone
from app.telephony.bridge import BARGE_SUSTAIN_FRAMES, MediaBridge


class FakeTwilioWS:
    """Stands in for Twilio's WebSocket. Records everything we send it."""

    def __init__(self, inbound):
        self._inbound = inbound
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def iter_text(self):
        for msg in self._inbound:
            yield msg
            await asyncio.sleep(0.005)

    def events(self, kind):
        return [m for m in self.sent if m.get("event") == kind]


def start_msg(sid="MZ123"):
    return json.dumps({"event": "start", "start": {"streamSid": sid}})


def media_msg(samples):
    ulaw = A.pcm16_to_ulaw(samples.astype(np.int16))
    return json.dumps(
        {"event": "media", "media": {"payload": base64.b64encode(ulaw).decode()}}
    )


def loud(n=160):
    return (np.random.randn(n) * 6000).astype(np.int16)


def quiet(n=160):
    return np.zeros(n, dtype=np.int16)


STOP = json.dumps({"event": "stop"})


@pytest.mark.asyncio
async def test_agent_audio_reaches_twilio_as_20ms_frames():
    ws = FakeTwilioWS([start_msg(), media_msg(quiet()), STOP])
    bridge = MediaBridge(ws, MockProvider([ProviderEvent(kind="audio", audio=tone(100))]))
    await asyncio.wait_for(bridge.run(), timeout=5)
    media = ws.events("media")
    assert media, "no audio was sent to Twilio"
    assert len(base64.b64decode(media[0]["media"]["payload"])) == 160


@pytest.mark.asyncio
async def test_inbound_call_gets_a_greeting_nudge():
    """Both sides waiting for the other is dead air, which reads as a dead line."""
    ws = FakeTwilioWS([start_msg(), STOP])
    provider = MockProvider()
    bridge = MediaBridge(ws, provider, greeting="(Greet the caller.)")
    await asyncio.wait_for(bridge.run(), timeout=5)
    assert provider.sent_text == ["(Greet the caller.)"]


@pytest.mark.asyncio
async def test_sustained_speech_interrupts_and_clears_twilio():
    ws = FakeTwilioWS(
        [start_msg()]
        + [media_msg(loud()) for _ in range(BARGE_SUSTAIN_FRAMES + 2)]
        + [STOP]
    )
    provider = MockProvider([ProviderEvent(kind="audio", audio=tone(2000))])
    bridge = MediaBridge(ws, provider)
    await asyncio.wait_for(bridge.run(), timeout=5)
    assert ws.events("clear"), "Twilio's buffer was never flushed"
    assert bridge.stats.barge_ins == 1
    assert provider.interrupts == 1


@pytest.mark.asyncio
async def test_a_single_loud_frame_is_not_an_interruption():
    """A cough or a door slam must not clip the agent mid-word."""
    ws = FakeTwilioWS([start_msg(), media_msg(loud()), media_msg(quiet()), STOP])
    bridge = MediaBridge(ws, MockProvider([ProviderEvent(kind="audio", audio=tone(500))]))
    await asyncio.wait_for(bridge.run(), timeout=5)
    assert bridge.stats.barge_ins == 0
    assert not ws.events("clear")


@pytest.mark.asyncio
async def test_receive_loop_survives_turn_boundaries():
    """The deafness regression: a turn_end must not end the receive loop."""
    ws = FakeTwilioWS([start_msg()] + [media_msg(quiet()) for _ in range(60)] + [STOP])
    provider = MockProvider(
        [
            ProviderEvent(kind="audio", audio=tone(40)),
            ProviderEvent(kind="turn_end"),
        ]
    )
    bridge = MediaBridge(ws, provider)
    task = asyncio.ensure_future(bridge.run())
    await asyncio.sleep(0.15)
    provider.push(ProviderEvent(kind="audio", audio=tone(40)))
    await asyncio.wait_for(task, timeout=5)
    # Audio arriving after the turn boundary still reached the caller.
    assert len(ws.events("media")) >= 4


@pytest.mark.asyncio
async def test_tool_result_is_returned_with_its_call_id():
    ws = FakeTwilioWS([start_msg()] + [media_msg(quiet()) for _ in range(8)] + [STOP])
    provider = MockProvider(
        [ProviderEvent(kind="tool_call", tool_call_id="fc1", tool_name="lookup_site",
                       tool_args={"question": "how much"})]
    )

    async def dispatch(name, args):
        return {"found": True, "passages": [{"source": "https://x/pricing"}]}

    bridge = MediaBridge(ws, provider, dispatch_tool=dispatch)
    await asyncio.wait_for(bridge.run(), timeout=5)
    assert provider.tool_results == [
        {"call_id": "fc1", "name": "lookup_site",
         "result": {"found": True, "passages": [{"source": "https://x/pricing"}]}}
    ]


@pytest.mark.asyncio
async def test_a_slow_lookup_makes_the_agent_hold_the_line():
    """Dead air is the one thing that makes a voice agent feel broken."""
    ws = FakeTwilioWS([start_msg()] + [media_msg(quiet()) for _ in range(30)] + [STOP])
    provider = MockProvider(
        [ProviderEvent(kind="tool_call", tool_call_id="fc1", tool_name="lookup_site",
                       tool_args={})]
    )

    async def slow(name, args):
        await asyncio.sleep(0.2)
        return {"found": True, "passages": []}

    bridge = MediaBridge(ws, provider, dispatch_tool=slow, stall_after_ms=50,
                         tool_timeout_ms=2000)
    await asyncio.wait_for(bridge.run(), timeout=5)
    assert any("checking" in t for t in provider.sent_text)
    assert provider.tool_results, "the result must still arrive after the stall"


@pytest.mark.asyncio
async def test_a_tool_that_never_answers_does_not_hang_the_call():
    ws = FakeTwilioWS([start_msg()] + [media_msg(quiet()) for _ in range(30)] + [STOP])
    provider = MockProvider(
        [ProviderEvent(kind="tool_call", tool_call_id="fc1", tool_name="lookup_site",
                       tool_args={})]
    )

    async def never(name, args):
        await asyncio.sleep(30)

    bridge = MediaBridge(ws, provider, dispatch_tool=never, stall_after_ms=40,
                         tool_timeout_ms=150)
    await asyncio.wait_for(bridge.run(), timeout=5)
    assert provider.tool_results[0]["result"]["error"]


@pytest.mark.asyncio
async def test_a_hanging_provider_connect_becomes_a_visible_error():
    """An open socket with a silent caller and healthy logs is the worst case."""
    class Hanging(MockProvider):
        async def connect(self, *, instructions, tools):
            await asyncio.sleep(30)

    events = []
    ws = FakeTwilioWS([start_msg(), STOP])
    bridge = MediaBridge(ws, Hanging(), connect_timeout_s=0.05,
                         on_event=lambda e: _record(events, e))
    await asyncio.wait_for(bridge.run(), timeout=5)
    assert any(e["type"] == "error" for e in events)


async def _record(sink, event):
    sink.append(event)


@pytest.mark.asyncio
async def test_interrupted_turn_records_only_what_the_caller_heard():
    ws = FakeTwilioWS(
        [start_msg()]
        + [media_msg(loud()) for _ in range(BARGE_SUSTAIN_FRAMES + 2)]
        + [STOP]
    )
    provider = MockProvider(
        [
            ProviderEvent(kind="audio", audio=tone(2000)),
            ProviderEvent(kind="transcript", role="agent", text="Our hours are"),
        ]
    )
    bridge = MediaBridge(ws, provider)
    await asyncio.wait_for(bridge.run(), timeout=5)
    cut = [t for t in bridge.transcript if t.get("interrupted")]
    assert cut, "an interrupted agent turn must be recorded as interrupted"
SVEOF46

cat > tests/test_cli.py << 'SVEOF47'
"""The ingest CLI end to end.

Written because a rename made a patch miss silently: `main()` started passing
arguments `run()` did not accept, `--help` still looked right, and the failure
only appeared on the machine actually running it. Testing the pieces is not
the same as testing the command.
"""
import json

import pytest

from app.ingest import __main__ as cli
from app.ingest import archive
from app.ingest.crawl import CrawlReport, Page

PAGE = (
    "<html><head><title>Acme Robotics</title></head><body><main>"
    "<h1>Programs</h1><p>" + "The LEGO Basic programme costs one hundred and fifty nine dollars. " * 6
    + "</p></main></body></html>"
)


def make_pages(n=8):
    return [
        Page(url=f"https://acme.test/p{i}", status=200, html=PAGE.replace("Programs", f"Page {i}"))
        for i in range(n)
    ]


@pytest.fixture()
def offline(monkeypatch, tmp_path):
    """No network, no model. Only the CLI's own wiring is under test."""
    async def fake_crawl(root, **kw):
        on_page = kw.get("on_page")
        pages = make_pages()
        for i, p in enumerate(pages, 1):
            if on_page:
                on_page(p, i)
        report = CrawlReport(root=root, final_root=root, fetched=len(pages))
        return pages, report

    async def fake_brief(docs, **kw):
        return {"name": "Acme Robotics", "brief": "Acme teaches robotics.",
                "greeting": "Acme Robotics, how can I help?"}

    async def fake_embed(texts, **kw):
        return [[float(len(t) % 7), 1.0, 0.0] for t in texts]

    monkeypatch.setattr(cli, "crawl", fake_crawl)
    monkeypatch.setattr(cli, "generate_brief", fake_brief)
    monkeypatch.setattr(cli, "embed_documents", fake_embed)
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("EMBEDDING_DIMS", "3")
    monkeypatch.setenv("SITE_DB", str(tmp_path / "site.db"))
    monkeypatch.setenv("FACTS_FILE", str(tmp_path / "facts.md"))
    from app.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_crawl_only_archives_and_stops(offline):
    """The half that runs on a laptop. It must not need an API key."""
    out = offline / "pages"
    code = await cli.run(
        "https://acme.test", max_pages=20, max_depth=2, db=None,
        save_html=str(out), crawl_only=True,
    )
    assert code == 0
    assert (out / archive.MANIFEST).is_file()
    assert len(json.loads((out / archive.MANIFEST).read_text())) == 8
    assert not (offline / "site.db").exists(), "crawl-only must not build the index"


@pytest.mark.asyncio
async def test_crawl_only_works_without_an_api_key(offline, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    from app.config import get_settings

    get_settings.cache_clear()
    code = await cli.run(
        "https://acme.test", max_pages=20, max_depth=2, db=None,
        save_html=str(offline / "pages"), crawl_only=True,
    )
    assert code == 0


@pytest.mark.asyncio
async def test_from_dir_builds_the_index_without_crawling(offline, monkeypatch):
    out = offline / "pages"
    archive.save(make_pages(), out)

    async def explode(*a, **k):
        raise AssertionError("--from-dir must not touch the network")

    monkeypatch.setattr(cli, "crawl", explode)
    code = await cli.run(
        "", max_pages=20, max_depth=2, db=str(offline / "site.db"), from_dir=str(out)
    )
    assert code == 0

    from app.store import connect, site_row

    conn = connect(offline / "site.db")
    row = site_row(conn)
    assert row["name"] == "Acme Robotics"
    assert row["page_count"] == 8
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] > 0


@pytest.mark.asyncio
async def test_an_empty_archive_directory_fails_loudly(offline):
    empty = offline / "nothing"
    empty.mkdir()
    (empty / archive.MANIFEST).write_text("[]")
    assert await cli.run("", max_pages=5, max_depth=1, db=None, from_dir=str(empty)) == 1


def test_main_passes_exactly_what_run_accepts():
    """The specific failure: main() grew arguments run() did not have."""
    import inspect

    accepted = set(inspect.signature(cli.run).parameters)
    for name in ("save_html", "from_dir", "crawl_only", "max_pages", "max_depth", "db"):
        assert name in accepted, f"run() is missing {name}"


def test_crawl_only_without_save_html_is_rejected(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ingest", "https://acme.test", "--crawl-only"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "--save-html" in capsys.readouterr().err
SVEOF47

cat > tests/test_crawl.py << 'SVEOF48'
from app.ingest.chunk import chunk_doc
from app.ingest.crawl import crawlable, extract_links, key, normalise, same_site
from app.ingest.extract import extract


def test_the_same_page_written_four_ways_has_one_key():
    variants = [
        "https://www.Example.com/about/",
        "https://example.com/about",
        "https://example.com/about#team",
        "https://example.com/about?utm_source=x",
    ]
    assert len({key(v) for v in variants}) == 1


def test_fetching_keeps_www_because_some_hosts_only_serve_one():
    """Stripping www from the URL we request is how a crawl of a working site
    returns zero pages."""
    assert normalise("https://www.example.com/about/") == "https://www.example.com/about"
    assert key("https://www.example.com/about") == "example.com/about"


def test_normalise_keeps_meaningful_query():
    assert normalise("https://example.com/p?id=7&utm_medium=x") == "https://example.com/p?id=7"


def test_same_site_ignores_www():
    assert same_site("https://www.example.com/a", "https://example.com")
    assert not same_site("https://cdn.example.com/a", "https://example.com")


def test_assets_and_admin_paths_are_skipped():
    assert not crawlable("https://example.com/logo.PNG")
    assert not crawlable("https://example.com/wp-admin/x")
    assert not crawlable("https://example.com/checkout")
    assert crawlable("https://example.com/pricing")


def test_extract_links_resolves_relative_and_drops_schemes():
    html = '<a href="/a">x</a><a href="mailto:x@y.com">m</a><a href="tel:123">t</a>'
    assert extract_links(html, "https://example.com/dir/") == ["https://example.com/a"]


HTML = """
<html><head><title>Acme - Pricing</title>
<meta name="description" content="Plans and pricing"></head>
<body>
<nav><a href="/">Home</a><a href="/about">About</a></nav>
<div class="cookie-banner">We use cookies</div>
<main>
<h1>Pricing</h1>
<p>The starter plan is forty dollars a month and includes two seats.</p>
<h2>Enterprise</h2>
<p>Enterprise pricing is custom. Contact the sales team for a quote today.</p>
<ul><li>Unlimited seats</li><li>Priority support</li></ul>
</main>
<footer>Copyright Acme</footer>
</body></html>
"""


def test_extract_strips_boilerplate_and_keeps_content():
    doc = extract("https://acme.com/pricing", HTML)
    assert doc.title == "Acme - Pricing"
    assert doc.description == "Plans and pricing"
    assert "forty dollars" in doc.text
    assert "cookies" not in doc.text
    assert "Copyright" not in doc.text
    assert "Home" not in doc.text
    assert doc.headings[:2] == ["Pricing", "Enterprise"]


def test_chunks_carry_their_heading():
    doc = extract("https://acme.com/pricing", HTML)
    chunks = chunk_doc(doc.url, doc.title, doc.text)
    assert chunks
    assert all(c.url == "https://acme.com/pricing" for c in chunks)
    assert any("Enterprise" in c.heading for c in chunks)


def test_long_text_windows_with_overlap():
    body = "## Section\n" + ("Sentence about the product. " * 200)
    chunks = chunk_doc("https://x/y", "T", body)
    assert len(chunks) > 1
    assert all(len(c.text) <= 1000 for c in chunks)


# --- crawl reporting ---------------------------------------------------------

import asyncio  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.ingest import crawl as crawl_mod  # noqa: E402
from app.ingest.crawl import FALLBACK_UA, crawl  # noqa: E402

PAGE = b"<html><head><title>T</title></head><body><main><p>" + b"word " * 60 + b"</p></main></body></html>"


def fake_client(monkeypatch, handler):
    """Route every crawler request through a handler.

    Patches the crawler's own client factory rather than httpx.AsyncClient,
    so the real transport plumbing (including the IPv4 fallback) stays out of
    the way and there is no chance of the replacement calling itself.
    """
    monkeypatch.setattr(
        crawl_mod,
        "open_client",
        lambda *a, **kw: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ),
    )


@pytest.mark.asyncio
async def test_a_ua_block_is_retried_with_a_browser_agent(monkeypatch):
    """Small-business sites sit behind WAFs that reject declared crawlers."""
    seen = []

    def handler(request):
        ua = request.headers.get("user-agent", "")
        seen.append(ua)
        if "SiteVoiceDemoBot" in ua:
            return httpx.Response(403)
        if request.url.path in ("/robots.txt", "/sitemap.xml", "/sitemap_index.xml"):
            return httpx.Response(404)
        return httpx.Response(200, content=PAGE, headers={"content-type": "text/html"})

    fake_client(monkeypatch, handler)
    pages, report = await crawl("https://blocked.test", max_pages=2)
    assert report.ua_fallback_used
    assert FALLBACK_UA in seen
    assert pages, "the fallback must actually recover the crawl"


@pytest.mark.asyncio
async def test_a_dead_root_reports_the_status_not_a_guess(monkeypatch):
    def handler(request):
        return httpx.Response(404)

    fake_client(monkeypatch, handler)
    pages, report = await crawl("https://gone.test", max_pages=2)
    assert pages == []
    assert report.root_status == "404"
    assert any("404" in n for n in report.notes)


@pytest.mark.asyncio
async def test_a_root_redirect_is_adopted_rather_than_argued_with(monkeypatch):
    def handler(request):
        if request.url.host == "apex.test":
            return httpx.Response(301, headers={"location": "https://www.apex.test/"})
        if request.url.path in ("/robots.txt", "/sitemap.xml", "/sitemap_index.xml"):
            return httpx.Response(404)
        return httpx.Response(200, content=PAGE, headers={"content-type": "text/html"})

    fake_client(monkeypatch, handler)
    pages, report = await crawl("https://apex.test", max_pages=2)
    assert report.final_root == "https://www.apex.test/"
    assert pages


@pytest.mark.asyncio
async def test_a_non_html_root_says_so(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"ok": True})

    fake_client(monkeypatch, handler)
    pages, report = await crawl("https://api.test", max_pages=2)
    assert pages == []
    assert any("not" in n and "HTML" in n for n in report.notes)


@pytest.mark.asyncio
async def test_a_connect_timeout_is_retried_on_ipv4(monkeypatch):
    """httpx has no Happy Eyeballs. Given an AAAA record and a container with
    no IPv6 route it hangs, while curl in the same shell succeeds."""
    attempts = []

    def handler(request):
        if request.url.path in ("/robots.txt", "/sitemap.xml", "/sitemap_index.xml"):
            return httpx.Response(404)
        return httpx.Response(200, content=PAGE, headers={"content-type": "text/html"})

    def factory(*a, force_ipv4=False, **kw):
        attempts.append(force_ipv4)
        if not force_ipv4:
            def boom(request):
                raise httpx.ConnectTimeout("timed out", request=request)
            return httpx.AsyncClient(transport=httpx.MockTransport(boom))
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        )

    monkeypatch.setattr(crawl_mod, "open_client", factory)
    pages, report = await crawl("https://v6trap.test", max_pages=2)
    assert attempts == [False, True]
    assert report.ipv4_forced
    assert pages, "the IPv4 retry must actually recover the crawl"


@pytest.mark.asyncio
async def test_failing_on_both_sockets_says_which_checks_to_run(monkeypatch):
    def factory(*a, **kw):
        def boom(request):
            raise httpx.ConnectTimeout("timed out", request=request)
        return httpx.AsyncClient(transport=httpx.MockTransport(boom))

    monkeypatch.setattr(crawl_mod, "open_client", factory)
    pages, report = await crawl("https://dropped.test", max_pages=2)
    assert pages == []
    assert any("netcheck" in n for n in report.notes)


def test_query_listing_pages_are_skipped():
    """?tag= and ?author= reshuffle posts that are already indexed alone. They
    add near-duplicate chunks and no new facts."""
    assert not crawlable("https://x.test/Blogs?tag=Robotics")
    assert not crawlable("https://x.test/Blogs?author=Admin")
    assert not crawlable("https://x.test/Blogs?page=2")
    assert crawlable("https://x.test/Blogs/blog-1")
    assert crawlable("https://x.test/p?id=7")


@pytest.mark.asyncio
async def test_a_link_that_redirects_off_site_is_not_archived(monkeypatch):
    """A payment link lands on the provider's domain. Archived, the agent ends
    up answering questions about the wrong company."""
    def handler(request):
        if request.url.path in ("/robots.txt", "/sitemap.xml", "/sitemap_index.xml"):
            return httpx.Response(404)
        if request.url.host == "acme.test" and request.url.path == "/":
            return httpx.Response(
                200,
                content=b'<html><body><main><a href="/pay">pay</a><p>'
                + b"word " * 60 + b"</p></main></body></html>",
                headers={"content-type": "text/html"},
            )
        if request.url.path == "/pay":
            return httpx.Response(302, headers={"location": "https://billing.other.test/x"})
        return httpx.Response(200, content=PAGE, headers={"content-type": "text/html"})

    fake_client(monkeypatch, handler)
    pages, report = await crawl("https://acme.test", max_pages=10)
    assert all("acme.test" in p.url for p in pages), [p.url for p in pages]
    assert report.failures.get("redirected off-site") == 1
SVEOF48

cat > tests/test_retrieval.py << 'SVEOF49'
import numpy as np
import pytest

from app.retrieval import Index, render
from app.store import connect, pack


@pytest.fixture()
def index(tmp_path):
    conn = connect(tmp_path / "s.db")
    rows = [
        ("https://x/pricing", [1.0, 0.0, 0.0]),
        ("https://x/pricing", [0.98, 0.02, 0.0]),
        ("https://x/about", [0.0, 1.0, 0.0]),
    ]
    for url, _ in rows:
        conn.execute("INSERT OR IGNORE INTO pages (url,title) VALUES (?,?)", (url, "T"))
    for i, (url, vec) in enumerate(rows, 1):
        conn.execute(
            "INSERT INTO chunks (id,url,title,heading,text,embedding) VALUES (?,?,?,?,?,?)",
            (i, url, "T", f"H{i}", "x" * 200, pack(vec)),
        )
    conn.commit()
    return Index(conn, 3)


def test_returns_one_chunk_per_page(index):
    hits = index.search([1.0, 0.05, 0.0], top_k=3, min_score=0.2)
    assert [h.url for h in hits] == ["https://x/pricing"]


def test_threshold_excludes_weak_matches(index):
    assert index.search([0.0, 0.0, 1.0], min_score=0.5) == []


def test_render_tells_the_model_what_to_do_when_empty():
    out = render([])
    assert out["found"] is False
    assert "message" in out["hint"].lower()


def test_render_keeps_source_per_passage(index):
    out = render(index.search([1.0, 0.0, 0.0], min_score=0.2))
    assert out["found"] is True
    assert out["passages"][0]["source"] == "https://x/pricing"


def test_empty_index_is_not_an_error(tmp_path):
    idx = Index(connect(tmp_path / "e.db"), 3)
    assert idx.size == 0
    assert idx.search(np.zeros(3)) == []
SVEOF49

cat > tests/test_webhook.py << 'SVEOF50'
"""Webhook surface: signature URL reconstruction and the TwiML Twilio gets."""
import pytest
from fastapi.testclient import TestClient

from app.config import resolve_base_url
from app.telephony.twilio_webhook import connect_stream_twiml, public_url


class FakeURL:
    def __init__(self, url, scheme, netloc):
        self._url, self.scheme, self.netloc = url, scheme, netloc

    def __str__(self):
        return self._url


class FakeRequest:
    def __init__(self, url, scheme, netloc, headers):
        self.url = FakeURL(url, scheme, netloc)
        self.headers = headers


def test_public_url_rebuilds_what_twilio_actually_signed():
    """Behind a proxy the app sees http and an internal host. Validating
    against that fails every time and surfaces as an unexplained 403."""
    req = FakeRequest(
        "http://10.0.0.4:8000/twilio/voice", "http", "10.0.0.4:8000",
        {"x-forwarded-proto": "https", "x-forwarded-host": "demo.up.railway.app"},
    )
    assert public_url(req) == "https://demo.up.railway.app/twilio/voice"


def test_public_url_is_unchanged_without_proxy_headers():
    req = FakeRequest("https://demo.test/twilio/voice", "https", "demo.test", {})
    assert public_url(req) == "https://demo.test/twilio/voice"


def test_twiml_uses_connect_not_start():
    """<Start><Stream> is listen-only: the caller hears nothing and the logs
    look completely healthy."""
    xml = connect_stream_twiml("wss://demo.test/ws/twilio/CA1")
    assert "<Connect>" in xml and "<Start>" not in xml
    assert 'url="wss://demo.test/ws/twilio/CA1"' in xml


@pytest.mark.parametrize(
    "configured,env,expected",
    [
        ("https://demo.test/", {}, "demo.test"),
        ("", {"RAILWAY_PUBLIC_DOMAIN": "site-voice.up.railway.app"},
         "site-voice.up.railway.app"),
        ("", {"FLY_APP_NAME": "site-voice"}, "site-voice.fly.dev"),
        ("", {}, ""),
    ],
)
def test_base_url_falls_back_to_the_platform(configured, env, expected):
    assert resolve_base_url(configured, env) == expected


@pytest.fixture()
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_health_says_no_site_before_ingest(client):
    body = client.get("/health").json()
    assert body["chunks"] == 0
    assert body["ok"] is False
    assert body["note"] == "no site ingested"


def test_health_admits_the_mock_fallback(client):
    """Asking for gemini with no key used to silently yield a mock while
    /health still said gemini."""
    body = client.get("/health").json()
    assert body["provider"] == "mock"


def test_inbound_call_returns_a_stream_url_with_the_call_sid(client):
    resp = client.post("/twilio/voice", data={"To": "+1615", "From": "+1901", "CallSid": "CA9"})
    assert resp.status_code == 200
    assert 'wss://demo.test/ws/twilio/CA9' in resp.text
SVEOF50

touch data/.gitkeep
chmod +x scripts/check.py scripts/netcheck.py scripts/offline_crawl.py
echo
echo "site-voice updated."
echo "  make test && make check"
echo "  python -m app.ingest --from-dir data/pages"
