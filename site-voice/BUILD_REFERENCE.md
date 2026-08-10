# site-voice: build reference

A phone number that answers and can only tell a caller what is actually on a
given website. Crawl a site, call the number, ask questions, hear grounded
answers with the source page shown on a live screen.

This document is the source of truth for rebuilding it. It records what works,
what does not, and why, including every failure from the first build. Followed
in order, the guess-implement-break-debug cycle does not need repeating.

Written after a build that took roughly twenty debugging rounds. Most were
avoidable. The avoidable ones are all here.

---

## Table of contents

1. [Read this first](#1-read-this-first)
2. [What the system does](#2-what-the-system-does)
3. [Architecture, and why each decision was made](#3-architecture-and-why-each-decision-was-made)
4. [Build order with verification gates](#4-build-order-with-verification-gates)
5. [Component reference](#5-component-reference)
6. [Configuration reference](#6-configuration-reference)
7. [Deployment](#7-deployment)
8. [Failure catalogue](#8-failure-catalogue)
9. [Guardrails](#9-guardrails)
10. [Testing discipline](#10-testing-discipline)
11. [Tuning](#11-tuning)
12. [Demo runbook](#12-demo-runbook)
13. [Porting to a different site](#13-porting-to-a-different-site)

---

## 1. Read this first

Five rules. Each was learned by violating it.

### 1.1 Read the working code, do not reconstruct it from memory

The voice layer of this project is a copy of a proven one. The first build
reconstructed it from conversation summaries instead of reading the files, and
three things came out wrong in ways that pass tests and fail on a phone:

- The mu-law encode table used a naive `abs()`. It disagreed with the reference
  codec on exactly 381 of 65536 values, all negative, the loudest ones.
  Distortion on loud audio, invisible to any test that does not compare against
  `audioop`.
- The Gemini adapter read audio from `server_content.model_turn.parts[].inline_data`.
  The real field is `response.data`. A session that transcribes correctly and
  plays no audio at all.
- Playback paced frames with `await asyncio.sleep(0.02)` between each. The
  source build had already tried and removed exactly that, with a comment
  explaining why. Reintroduced anyway.

If a working implementation exists, clone it and read it. Every hour spent
reading saves several hours of phone calls.

### 1.2 A failure must report itself, not be guessed at

Roughly half the debugging rounds were spent guessing at failures that produced
no diagnostic output. Each time, the fix was to add instrumentation, and the
real cause was then visible immediately, often something nobody had proposed.

- "nothing fetched, wrong URL or the site blocks crawlers" wasted two rounds.
  Replaced with an eight-line report: root status, content type, robots result,
  user agent, sitemap count, per-reason failure counts.
- Calls ended silently. Replaced with one line naming the exit reason and
  whether audio flowed each way.
- Retrieval returned nothing in production and everything locally. Replaced
  with an HTTP endpoint running the production path and returning scores plus
  config. The cause was visible in one request.

Build the diagnostic before the third guess. Not the fifth.

### 1.3 Instrument the boundary between environments

Every remaining bug lived where two environments disagreed: local versus
container, Codespace versus Railway, one SDK version versus another. None were
logic errors.

So every service reports, at startup and on `/health`:

- which config file it read, and whether that file existed
- the installed version of any SDK whose API has changed
- the model names it will use
- the size and dimensionality of any index it loaded

The worst bug in the build was `EMBEDDING_MODEL` in the Railway variables
holding a twenty-character fragment of a secret instead of a model name. One
request to find, once `/health` printed the model name. Several rounds of
speculation before that.

### 1.4 A threshold is not a judge

Cosine similarity against a single-topic corpus is compressed. On this site
every question scored between 0.55 and 0.77, including "do you sell used cars"
at 0.57.

Two attempts to fix this with cleverness failed, one of them actively harmful
(see [z-score](#83-retrieval-and-ranking)). What works is a coarse floor tuned
by sweeping against deliberately unrelated control questions, plus an explicit
instruction telling the model that retrieved passages are the closest text on
the site and not necessarily an answer.

Retrieval finds candidates. The language model decides. Do not make similarity
do a job it cannot do.

### 1.5 Never assert what a site does not contain

The prompt at one point said "opening hours are NOT in your summary, you do not
know them". That was written after checking two pages of a twenty-six page
site. The location pages publish full hours. The instruction made the agent
refuse questions it could answer correctly, and it was hard-coded into a test.

State the rule positively: check you can point to it in the summary or a lookup
result. Never enumerate what is missing.

---

## 2. What the system does

1. **Ingest.** Crawl a website, strip boilerplate, chunk what remains, embed
   the chunks, and have a cheap text model write a factual brief of the
   business. All offline, one command.
2. **Serve.** A caller dials a Twilio number. Twilio opens a media stream
   WebSocket. The service bridges that audio to a realtime speech model with
   the brief in its system instruction and a `lookup_site` tool over the chunks.
3. **Observe.** A browser screen shows the transcript live, every source URL
   cited, per-turn latency, tool outcomes and barge-ins.

Answering rule: the agent may state only what came from the brief or a lookup
result. Anything else gets "that isn't on the site" and an offer to take a
message.

That refusal is the product. An agent that invents a price for someone else's
business is worse than no agent, and demonstrating restraint is what makes the
rest credible.

---

## 3. Architecture, and why each decision was made

### 3.1 The brief lives in the prompt, not behind a tool

Measured on the source build: about 1.6 seconds of model round trip per turn,
transport effectively zero. A tool call is a second full round trip.

Most callers ask what the business does, where it is, when it is open and what
it costs. Those go in the system instruction so they return in one turn. The
tool handles the long tail.

The brief is generated once at ingest by a cheap text model reading the crawl,
so its cost never touches a live call. Target 300 to 400 words.

### 3.2 SQLite and a numpy matrix, not a vector database

One site at a time, roughly 70 chunks, 768 dimensions. Under a megabyte,
searches in well under a millisecond in memory.

A vector database would add a network hop to a path already competing with a
model round trip, plus a service to run and a schema to migrate. At this scale
it is strictly worse.

The database is a single file that ships inside the container image. No
database service, no connection pool, no deploy-time migration.

### 3.3 Crawling is separate from embedding

They run on different machines, because sometimes they must.

The target site was on Hostinger, which null-routes cloud IP ranges. From a
Codespace, the TCP connect to port 443 timed out with dropped packets. Nothing
at the HTTP layer fixes that: no user agent, no proxy header, no retry.

So the crawler can archive raw HTML plus a URL manifest, and ingest can read
that archive instead of the network. Crawl on a laptop, commit the archive,
embed anywhere. A dependency-free single-file crawler
(`scripts/offline_crawl.py`, standard library only) covers a machine with
nothing installed.

This is not a workaround. Ingest was always an offline build step, since the
database ships in the image and the server never crawls.

### 3.4 The provider is behind an adapter

The bridge talks to a `RealtimeProvider` protocol, never a vendor SDK. That
buys three things:

- a `MockProvider` exercising the entire bridge with no API key, no network and
  no phone, which is what makes the bridge testable at all
- one file to change when the vendor renames a method, which happened during
  this build
- the ability to move the adapter between projects unchanged

### 3.5 One process, one worker

Call state lives in the process. A second worker would drop callers onto a
process that has never heard of them. `--workers 1`, and if that ever needs to
change, the state moves to Redis first.

### 3.6 The observer can never affect the call

The monitoring screen is strictly an observer. Its failures are logged and
swallowed. Learned the hard way: a browser reconnecting mid-iteration raised
out of the event fan-out and killed live calls.

The same principle appears in the source build as "a text that fails must never
fail an order the kitchen is already cooking".

---

## 4. Build order with verification gates

Do not proceed past a gate that has not passed. Each catches a class of error
far more expensive to find later.

### Phase 0: skeleton and audio

```
app/audio.py
app/providers/base.py
app/providers/mock.py
tests/test_audio.py
```

**Gate:** the codec matches `audioop` across all 65536 sample values and all
256 byte values. Use `pytest.importorskip("audioop")` since it is removed in
Python 3.13, but run the comparison at least once where it still exists.
Without this the 381-value discrepancy ships silently.

### Phase 1: the bridge

```
app/telephony/bridge.py
app/telephony/twilio_webhook.py
tests/test_bridge.py
```

**Gate:** with the mock provider and a fake WebSocket, all of these pass:

- caller audio reaches the provider at the provider's input rate
- provider audio reaches Twilio as 160-byte frames with the right `streamSid`
- an inbound call gets a greeting nudge (the agent speaks first)
- sustained loud input triggers barge-in, sends Twilio `clear`, stops playback
- a single loud frame does not trigger barge-in
- a `turn_end` does not end the receive loop
- a tool call is dispatched and the result returns with its call id
- a slow tool produces a holding phrase and still returns
- a tool that never returns does not hang the call
- a hanging `connect()` becomes a visible error
- a failing event observer does not end the call

### Phase 2: ingest

```
app/ingest/crawl.py, extract.py, boilerplate.py, chunk.py, brief.py, archive.py
app/store.py, app/embed.py, app/retrieval.py
```

**Gate:** `python -m app.ingest <url>` reports usable pages, lines removed as
chrome, chunk count and average chunk size. Average chunk size must exceed 400
characters. Under 350 means chunking is wrong and every similarity score will
compress into an unusable band.

### Phase 3: retrieval evaluation

```
scripts/eval.py, questions.txt
```

**Gate:** the threshold sweep finds a clean split. Write 25 to 30 questions
grouped as: answerable from the brief, answerable via lookup, genuinely absent
from the site, and three deliberately unrelated controls. A clean split means
some threshold keeps nearly all real questions and rejects every control. No
clean split means the crawl or the chunking is wrong, not the threshold.

### Phase 4: the model, without a phone

```
tools/probe_gemini.py
```

**Gate:** `python tools/probe_gemini.py --repeat 3` passes three times
consecutively, with the real prompt and real tool schemas loaded. Three
consecutive runs is the test for session-quota limits, which do not appear on a
single run.

This tool should exist before the first phone call, not after the fifth failed
one.

### Phase 5: wire it up

```
app/agent.py, app/main.py, app/screen.html
```

**Gate:** `/health` returns the provider actually in use (not the configured
one), the SDK version, the embedding model name, the index size and the crawl
date. `/api/lookup?q=` returns scores from the production path.

### Phase 6: deploy

**Gate:** `/health` on the deployed host shows the same SDK version, embedding
model and chunk count as local. Then `/api/lookup?q=` with a question known to
score well returns a hit. Only then point the phone number at it.

Most production-only failures in this build would have been caught by comparing
`/health` between environments before dialling.

---

## 5. Component reference

### 5.1 `app/audio.py`

G.711 mu-law codec and PSTN resampling. Twilio carries 8 kHz mu-law; realtime
models want 16 kHz PCM16 in and emit 24 kHz PCM16 out.

**Do not use `audioop`.** Removed in Python 3.13. Build numpy lookup tables at
import time.

**The encode table must use the 14-bit domain**: `abs(sample >> 2) << 2`, not a
naive `abs(sample)`. The naive version disagrees with the reference codec on
381 values. Pin the tables against `audioop` in a test.

Provide a `frames()` helper returning whole 20 ms frames plus a remainder.
Never pad a partial frame with silence; carry it to the next batch.

### 5.2 `app/providers/base.py`

The provider protocol. Two contract notes that cost real debugging time and
belong in the docstring:

- `receive()` must keep yielding across turn boundaries. Vendor SDKs terminate
  their async iterator at the end of every model turn. Passing that through
  makes the bridge go deaf after the greeting and the call dies to a keepalive
  timeout with no error anywhere. Adapters wrap the vendor iterator in an outer
  `while` loop.
- `interrupt()` must be safe to call when the model is not speaking.

Events are a small closed set: `audio`, `transcript`, `tool_call`, `turn_end`,
`error`. The bridge understands nothing else.

### 5.3 `app/providers/gemini.py`

**Audio arrives on `response.data`.** Not on `server_content.model_turn.parts`.
Getting this wrong produces a session that transcribes perfectly and plays
nothing.

**Pin the SDK floor.** `send_realtime_input` and `send_tool_response` arrive in
`google-genai` 1.9.0 and do not exist before it. Use `google-genai>=1.9,<3`.
Never invent a version number: the first build pinned `==1.2.0`, which opens a
session fine and then raises `AttributeError` on the first thing sent.

Check the SDK surface at `connect()` and raise a named error. An
`AttributeError` mid-call kills the line with nothing said.

**Retry the connect once**, and drop the context manager without entering it if
the caller cancels. Calling `__aexit__` on a context that never entered can
leave a session slot held, which makes the next call fail too. Track an
`_entered` flag.

Latency levers, both in the connect config:

- `thinking_config.thinking_level`: `minimal` for fastest first audio
- `realtime_input_config.automatic_activity_detection.silence_duration_ms`: how
  long the model waits before deciding the caller finished. Added to every
  turn, so usually the largest single term in perceived latency. Below roughly
  350 ms it starts cutting off people who pause mid-sentence.

### 5.4 `app/telephony/bridge.py`

The most subtle file. Copy a working one if you have it.

**Do not pace outbound frames with sleeps.** Sending one frame then sleeping
20 ms looks like correct real-time pacing and is not. `asyncio.sleep` overshoots
every iteration, so a long reply drifts hundreds of milliseconds late and the
backlog compounds across turns. Twilio buffers inbound media itself. Drain the
queue as fast as frames arrive.

**Barge-in is client-side RMS with a sustain count.** Compute RMS on inbound
frames; require several consecutive loud frames before interrupting. A single
loud frame is a cough or a door and must not clip the agent mid-word. On
interrupt: clear the outbound queue, send Twilio `{"event": "clear"}`, call
`provider.interrupt()`.

**Tools get two deadlines.** A stall threshold triggering a holding phrase, and
a real timeout that gives up. Set the stall threshold *above* the measured tool
time. Every stall nudge is a chance for the model to keep generating past the
holding phrase and invent an answer, which happened on a real call. With
lookups at 224 ms, a 1500 ms stall threshold means the nudge almost never fires.

The stall nudge must be explicit and forbidding:

```
(SYSTEM: the lookup is still running and you do not have the answer yet.
Say exactly one short holding phrase of three or four words, such as
'one moment please', and then STOP. Do not answer the question. Do not
state any fact, price, time, or address. The result is coming.)
```

A softer wording ending in "then wait" was not obeyed.

**A tool timeout is not a miss.** Return `{"error": "timeout", "hint": ...}`
with an explicit instruction saying the search did not finish, this does not
mean the answer is missing, and do not tell the caller it is not on the site.
Returning `found: false` for a timeout makes the agent state something false.

**Bound `connect()`.** A hanging connect is an open socket, healthy logs and a
silent caller. Time it out and emit a visible error.

**Log how every call ended**: exit reason, inbound frame count, outbound frame
count, turns, barge-ins, tool calls. A silent exit looks identical whether
Twilio hung up, the socket dropped or the model died, and those need different
fixes. Inbound frames but no outbound means the model went quiet; outbound but
no inbound means a one-way stream, usually `<Start><Stream>` instead of
`<Connect><Stream>`.

**`_emit` must swallow observer failures.** Catch everything except
`CancelledError` and log a warning.

### 5.5 `app/telephony/twilio_webhook.py`

**Use `<Connect><Stream>`, not `<Start><Stream>`.** `<Start>` is listen-only:
the caller hears nothing and the logs look completely healthy.

**Reconstruct the signed URL from forwarded headers.** Behind a proxy the app
sees `http` and an internal host, so validating against `request.url` fails
every time and surfaces as an unexplained 403. Read `x-forwarded-proto` and
`x-forwarded-host`.

When validation fails, log the URL that was validated against. That single line
turns a 403 into a five-second fix.

### 5.6 `app/ingest/crawl.py`

**The URL you fetch is not the URL you compare.** Stripping `www.` is correct
for deciding whether two links are the same page and wrong for deciding what to
request, because many hosts serve only one form. Keep `normalise()` faithful to
the host and provide a separate `key()` for comparison and deduplication.

**Probe the root before anything else** and report: requested URL, resolved
URL, status, content type, robots result, user agent, sitemap count, pages
fetched, per-reason failure counts. Same eight lines on success and failure.

**Adopt the root's redirect.** A site that sends the apex to `www` knows better
than the crawler does.

**Retry once with a browser user agent** if the root answers 401, 403, 406, 429
or 503. Many small-business sites sit behind a WAF that rejects declared
crawlers. Say so in the report rather than switching silently.

**Retry on a forced-IPv4 socket** if the connect times out. `httpx` has no
Happy Eyeballs: given an AAAA record and a container with no working IPv6
route, it picks v6 and hangs, while `curl` in the same shell falls back in
milliseconds and makes the site look reachable. Build the client through a
factory taking a `force_ipv4` flag, both so this is one line and so tests can
replace it.

**Check same-origin on the final URL, after redirects.** Payment links redirect
to the provider's domain. Archived, they make the agent answer questions about
the wrong company. This build indexed four Zoho billing pages before the check
existed.

**Skip query-parameter listing pages**: `?tag=`, `?author=`, `?page=`,
`?paged=`, `?s=`, `?q=`, `?sort=`, `?filter=`. Each reshuffles posts already
indexed individually and contributes near-duplicate chunks with no new facts.
Nine of forty-one pages in the first crawl were these.

Also skip asset extensions and admin, cart, checkout and account paths. Honour
`robots.txt`. A demo that ignores it is a demo you cannot show to the company
whose site you crawled.

### 5.7 `app/ingest/extract.py` and `boilerplate.py`

Per-page extraction strips `script`, `style`, `nav`, `footer`, `header`,
`aside`, `form`, plus any element whose class, id or role matches a chrome
pattern. Prefer `main`, then `article`, then `[role=main]`, then `body`.

**Cross-page removal is separate and necessary.** Marketing sites repeat
testimonial carousels, shared FAQ accordions and product cards inside `<main>`
on every page. Left in, asking the price of one product retrieves a different
product's page, because that page carries the first product's card.

Rule: a line appearing on half the pages or more is chrome. Apply only above
about six pages, since on three pages two hits is usually a shared sentence.
Skip very long lines, which are almost never chrome.

**Strip repeated headings too.** An early version exempted them to preserve
citation labels, and the result was that "Frequently Asked Questions", "Have
questions?" and a registration banner appeared on every page and started
winning matches. Let the chunker fall back to the page title, which cites
better anyway.

On this site the filter removed 15 distinct repeated lines, taking the corpus
from 133k to 54k characters. That is the same block removed 25 times over, not
lost content.

### 5.8 `app/ingest/chunk.py`

**Merge sections; do not emit one chunk per heading.** The first version did,
and on a heading-dense marketing site produced 777 chunks averaging 171
characters. Short text embeds toward the corpus average, so every query scored
between 0.55 and 0.77 and no threshold could separate a real match from an
unrelated one.

After merging: 71 chunks averaging 768 characters, from the same pages.

Target around 1100 characters, minimum around 450 before a chunk can close,
overlap around 150 when splitting a genuinely long section. Break on line then
sentence boundaries.

Every chunk carries its URL and a label. If the heading was stripped as chrome,
fall back to the page title.

### 5.9 `app/ingest/brief.py`

One call to a cheap text model over the crawl, returning JSON with `name`,
`brief` and `greeting`.

Front-load the pages a caller is most likely to ask about (about, contact,
pricing, services, hours, location, faq) by path, then by depth, then by
length. Cap the corpus sent.

Instruct it to include only what is stated, to lead with what the business does
in one sentence a caller would recognise, and to say explicitly if hours,
prices or a phone number are absent so the agent knows not to guess. Under 400
words.

### 5.10 `app/ingest/archive.py` and `scripts/offline_crawl.py`

Save raw HTML plus `manifest.json` mapping file to URL, so citations point at
the real page rather than a local filename.

Filenames need a hash suffix. Two different URLs can slugify to the same string
and silently overwrite each other, which surfaces much later as a page
mysteriously absent from the index.

`offline_crawl.py` is a single standard-library file for a machine with nothing
installed. Assert in a test that every import is in `sys.stdlib_module_names`,
and that both crawlers produce identical filenames. A stray dependency is only
discovered on the machine you are not sitting at.

### 5.11 `app/retrieval.py`

Cosine over a normalised numpy matrix loaded once at startup. One chunk per
page in the results; three chunks from one page sound like one source to a
caller and waste the context budget.

`render()` returns passages with `source` and `section`, plus a `hint` telling
the model what to do. The hint is load-bearing:

```
These passages are the closest text on the site, which is not the same as
an answer. Read them. If they do not actually answer the question, say it
is not on the site and offer to take a message. Do not infer, estimate, or
generalise from a related passage.
```

For an empty result, `found: false` with a hint saying to say so plainly and
offer a message. A model reads an empty list as an error and starts improvising.

### 5.12 `app/agent.py`

The system instruction. Rules that were each earned:

- **First person plural, on every turn.** Without an explicit rule the model
  drifts into "they offer" and "their Brentwood campus", which sounds like an
  outsourced service reading a brochure rather than the front desk. State it
  once with a concrete example and once for the possessive.
- **Speak calmly and unhurried.** And remove any conflicting rule such as
  "match the caller's pace, if they are brisk, be brisk".
- **One sentence when one will do, two at most.**
- **Do not close every turn with an offer of further help.** Ask once, at the end.
- **Ground positively.** "Before saying any time of day, price, address or
  phone number, check that you can point to it in the summary or a lookup
  result. If you cannot, say so and offer to take a message. Never reason out
  what a business like this one probably charges or when it probably opens."
  Do not enumerate what the site lacks (see
  [rule 1.5](#15-never-assert-what-a-site-does-not-contain)).
- **While a lookup is running you do not have the answer.** Say the holding
  phrase and stop. Never continue past it.
- **A lookup returning nothing is the answer.** Do not fill the gap from
  general knowledge about what a business like this probably does.
- **Inject the current date and time**, with timezone. Without it the model
  answers "are you open tomorrow" with a confident guess about what day it is.
- The greeting nudge must be a stage direction in brackets and must say to name
  the business exactly once. Dialogue gets read out verbatim, and without the
  constraint the agent opens with "RobotiX Institute, this is RobotiX Institute".

### 5.13 `app/main.py`

**Key monitor sockets by call id, or snapshot the set before iterating.** This
was the single worst bug in the build. A global subscriber set iterated live,
with `await send_text` as a suspension point, plus a screen that reconnects
every 1.5 seconds, means a browser reconnecting mid-iteration mutates the set
being walked. Python raises `RuntimeError: Set changed size during iteration`,
which escaped into the bridge and killed live calls with nothing in the log.

**`/health` must report the effective provider**, not the configured one.
Asking for a real provider with no API key silently yields a mock while health
still says otherwise. In production, refuse to start instead.

**`/health` must also report** the SDK version, the embedding model name, the
index size and the crawl date. This makes environment comparison a one-line
operation.

**Provide `/api/lookup?q=`**, running the production retrieval path and
returning the config, the stats, the filtered results and the results with no
floor applied. On the error path, return the config too: an error alone does
not say which model was asked for.

### 5.14 `app/config.py`

Resolve `.env` by walking ancestors from the config file, never a bare relative
`".env"`, which resolves against the working directory and silently loads
nothing.

Never index parents at a fixed depth. In the container the app sits at
`/srv/app` and `parents[3]` raises `IndexError` at import, killing the process
before it can serve anything.

Split public base URL resolution into a pure function taking a dict, so it can
be tested without depending on whether the developer has a `.env`.

Report which file was read and whether it existed. An env file that is silently
not found looks identical to one with every value at its default.

---

## 6. Configuration reference

```bash
APP_ENV=dev                      # prod makes a missing key a hard startup failure
PUBLIC_BASE_URL=                 # host only, no scheme; blank on Railway is fine

TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_NUMBER=+1
TWILIO_VALIDATE_SIGNATURE=true   # never false in production

REALTIME_PROVIDER=gemini
GEMINI_API_KEY=
GEMINI_LIVE_MODEL=gemini-3.1-flash-live-preview
GEMINI_THINKING_LEVEL=minimal    # minimal | low | medium | high
GEMINI_END_OF_SPEECH_MS=500      # below ~350 cuts off mid-sentence pauses
GEMINI_VOICE=Aoede
GEMINI_TEXT_MODEL=gemini-2.5-flash

EMBEDDING_MODEL=gemini-embedding-001
EMBEDDING_DIMS=768

TOP_K=4
MIN_SCORE=0.58                   # fitted per site; sweep it
MIN_Z=0.0                        # leave at 0, see failure catalogue

SITE_DB=data/site.db
SITE_TIMEZONE=America/Chicago
FACTS_FILE=data/facts.md
MAX_PAGES=120
MAX_DEPTH=3
```

Dependencies worth pinning deliberately:

```
google-genai>=1.9,<3   # 1.9 is the floor for send_realtime_input
fastapi, uvicorn[standard], websockets
httpx, selectolax       # crawl and extract
numpy, scipy            # codec, resampling, retrieval
twilio
pydantic-settings
python-multipart        # required for Form(); missing it is a 500 on the webhook
```

---

## 7. Deployment

### 7.1 Where

**Railway, Dockerfile builder, US East, paid tier.** US East for media latency.

**Not GitHub Codespaces.** The forwarded-port relay returns 404 on the
WebSocket upgrade and Twilio reports error 31920.

**Not Fly's trial tier.** Machines stop after five minutes.

### 7.2 The Dockerfile CMD

```dockerfile
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} \
     --workers 1 --proxy-headers --forwarded-allow-ips '*'"]
```

Every part matters:

- **Shell form** so `$PORT` expands. Railway assigns a port and probes the
  health check there. A hardcoded port means the process is healthy, the probe
  knocks on the wrong door, and the deploy is marked failed with the
  application logs looking perfectly fine.
- **`--workers 1`** because call state lives in the process.
- **`--proxy-headers --forwarded-allow-ips '*'`** so `X-Forwarded-*` is visible.
- **No `--ws-ping-interval` or `--ws-ping-timeout`.** These look like a
  keepalive and act as a kill switch. Uvicorn pings the client and closes the
  socket when no pong arrives inside the timeout. Twilio's Media Streams client
  does not reliably answer ping frames, so a live call drops twenty to forty
  seconds in at no particular point in the conversation. This produced hours of
  misdiagnosis as "random disconnects".

### 7.3 Deploy sequence

1. Ingest locally. Commit `data/site.db` with `git add -f` (it is gitignored on
   purpose so a stale one is never committed by accident).
2. Push. Set variables. Wait for the deployment to show **Active**.
3. `curl /health` and compare against local: SDK version, embedding model,
   chunk count, provider.
4. `curl "/api/lookup?q=..."` with a question known to score well.
5. Only then point the phone number.

**Wait for Active before dialling.** The old container keeps serving until the
new one passes its health check, and calling into that window produces a
failure that has already been fixed. This wasted a round in this build.

### 7.4 Sharing a phone number

If the number is shared with another project, make the swap reversible:

- record the current voice URL to a backup file before the first swap
- refuse to overwrite an existing backup, so running the command twice cannot
  lose the original
- provide `status`, `point <url>` and `restore`
- read credentials through the same config the service uses, not `os.environ`
  directly, or the script works only in the shell where they were exported and
  can silently disagree with the app about which account it is using

---

## 8. Failure catalogue

Symptom, cause, fix. Every one of these happened.

### 8.1 Crawl and ingest

| Symptom | Cause | Fix |
|---|---|---|
| Zero pages fetched, no explanation | The error message was a guess | Probe the root and report status, content type, robots, UA, failures |
| Crawls a different host than requested | `normalise()` stripped `www.` from the fetch URL | Keep the host for fetching; separate `key()` for comparison |
| `ConnectTimeout`, `curl` works fine | httpx has no Happy Eyeballs; broken IPv6 route | Retry on a forced-IPv4 transport |
| `ConnectTimeout` on both stacks, raw TCP to an A record times out | Host null-routes cloud IP ranges | Crawl elsewhere, archive, ingest from the archive |
| Off-site pages in the index | Same-origin checked on the requested URL, not the final one | Check after redirects |
| Many near-duplicate chunks | `?tag=`, `?author=`, `?page=` listing pages | Skip by query parameter |
| Most pages zero words | Bot wall serving a challenge page | Count separately and warn above a third |
| `run() got an unexpected keyword argument` | A rename made a patch silently miss; caller and callee diverged | AST check that `main()` passes only what `run()` accepts |
| `no such column` on an existing database | `CREATE TABLE IF NOT EXISTS` leaves old tables alone | Lightweight `ALTER TABLE` migrations at connect |

### 8.2 Chunking and extraction

| Symptom | Cause | Fix |
|---|---|---|
| Every score in a 0.20 band, threshold useless | 777 chunks averaging 171 chars | Merge sections to ~1100 chars |
| Asking about product A retrieves product B's page | Repeated price cards inside `<main>` | Cross-page chrome removal |
| "Frequently Asked Questions" wins matches | Headings exempted from chrome removal | Strip repeated headings; fall back to page title |

### 8.3 Retrieval and ranking

**The z-score attempt, recorded so it is not tried again.**

Theory: an unrelated question is uniformly mediocre against every chunk while a
real one spikes on a few, so the top hit standing `k` standard deviations above
the mean should be scale-free and need no per-site tuning.

Measured result: anti-correlated with relevance. A query matching many chunks
("when is the summer camp" against four camp pages) raises the mean and
flattens its own z. A query relevant to nothing sits against a low mean with
tiny variance, so a weak spike looks like an outlier.

```
"what is your refund policy for hotel bookings"  z 2.8   (control, should fail)
"when is the summer camp"                        z 1.6   (real, should pass)
```

No z threshold separates them. Raw cosine at 0.57 kept 23/23 real questions and
rejected 3/3 controls. The simpler signal was already the better one.

Leave `MIN_Z` at 0. Sweep `MIN_SCORE` against control questions instead, and
let the model do the judging.

| Symptom | Cause | Fix |
|---|---|---|
| Unrelated questions match | Threshold below the control band | Sweep against controls, take the lowest clean split |
| Agent asserts something is absent when it is present | Retrieval failure reported as `found: false` | Distinguish error from miss everywhere |

### 8.4 The bridge and the call

| Symptom | Cause | Fix |
|---|---|---|
| Deaf after the greeting, then keepalive timeout | `receive()` ended at a turn boundary | Outer `while` loop in the adapter |
| Transcribes correctly, plays no audio | Reading audio from the wrong SDK field | `response.data` |
| Agent talks over an interruption | Twilio already buffered the audio | Clear the queue and send Twilio `clear` |
| Reply drifts progressively late | `asyncio.sleep` pacing overshoot compounding | Do not pace; drain as fast as frames arrive |
| Agent clipped by a cough | Barge-in on a single loud frame | Require sustained loud frames |
| Random drops 20 to 40 seconds in | `--ws-ping-timeout`; Twilio does not pong | Remove both ping flags |
| Random drops with the screen open | Global subscriber set iterated live | Snapshot before iterating; swallow observer errors |
| `AttributeError: send_realtime_input` | SDK pinned below 1.9.0 | `google-genai>=1.9,<3`, plus a surface check at connect |
| Session opens, then silence, then hangup | Provider error broke the loop with no log | Log the exit reason and audio counts every call |
| Consecutive calls fail to open a session | Concurrent-session quota; a cancelled connect held a slot | Retry once; do not `__aexit__` a context that never entered; enable billing |
| Agent invents a fact mid-lookup | Stall nudge too soft, model ran past the holding phrase | Explicit forbidding nudge; raise the stall threshold above measured tool time |

### 8.5 Deployment and configuration

| Symptom | Cause | Fix |
|---|---|---|
| Deploy fails, application logs look healthy | Hardcoded port; health probe on `$PORT` | Shell-form CMD with `${PORT:-8000}` |
| 403 on the Twilio webhook | Signature validated against the internal URL | Rebuild from forwarded headers; log the URL used |
| 404 from the embedding endpoint | `EMBEDDING_MODEL` held a fragment of a secret | Report the model name on `/health` and on the error path |
| Works locally, fails deployed | Environments disagree on SDK, model or threshold | Compare `/health` before dialling |
| Fix appears not to work | Called into the old container mid-rollout | Wait for Active |
| A test passes for one person and fails for another | The suite read the real `data/site.db` | Point tests at a temp database in `conftest.py` |
| Raw SDK `ValueError` about "key inputs" | Missing key surfaced from three frames deep | Check for the key and name the file to edit |
| Script cannot see credentials the app can | Read `os.environ` instead of the config | Use the same settings object |

### 8.6 Process

| Symptom | Cause | Fix |
|---|---|---|
| A patch reverts an earlier fix | Local edits and generated patches diverged | Replay local edits into the source of truth before generating a patch |
| Confident claim about the site turns out wrong | Checked two pages of twenty-six | Fetch the specific page before asserting absence |

---

## 9. Guardrails

`scripts/check.py` parses rather than greps, so a comment explaining a bug is
not mistaken for the bug. Run it in `make check` and in CI.

Checks worth having from day one:

1. **No fixed-depth `parents[N]`.** Raises `IndexError` at import in the container.
2. **No pacing sleep in the outbound pump.** Any nonzero sleep there is the drift bug returning.
3. **Audio read from the correct SDK field.**
4. **CLI signature matches its caller.** Walk the AST, compare keywords passed against parameters accepted.
5. **Tests never reach a live model.** Assert `conftest.py` pins the key empty.
6. **Tests use a sandbox database.** Assert the temp path appears in `conftest.py`.
7. **No test reloads the settings module.** Environment-dependent tests pass on one machine and fail on another.
8. **The generated database is gitignored.** A committed stale one silently deploys an old crawl.

Each encodes a bug that actually shipped. Add one whenever a class of mistake
recurs.

---

## 10. Testing discipline

The mock provider is what makes any of this testable. Every bridge behaviour is
verified with no API key, no network and no phone.

**`conftest.py` must**:

- set config before anything imports the config module
- pin the API key empty so no test can reach a live model
- point `SITE_DB` and `FACTS_FILE` at a temp directory

**Test the pure function, not the settings object.** Anything reading a
developer's `.env` produces a test that passes for whoever has not set it up
yet and fails for everyone else.

**Test the CLI end to end**, not just the functions it calls. The
`run()`/`main()` signature mismatch passed every unit test and `--help`, and
failed only when someone ran the command.

**Write the test that reproduces the report.** The observer-kills-the-call bug
was reproducible in fifteen lines once the mechanism was understood, and that
test is what stops it returning.

Roughly 90 tests covers this system: codec against `audioop`, crawl and
normalisation, extraction and chunking, chrome removal, retrieval thresholds,
archive round-trip, bridge behaviours, CLI paths, prompt content, HTTP surface.

---

## 11. Tuning

**`MIN_SCORE`** is the dial that matters, and it is fitted per site. Run the
sweep in `eval.py` after every re-crawl. Too low and the agent answers
unrelated questions with whatever chunk scored highest, confidently and wrong.
Too high and it refuses things that are present.

**`GEMINI_END_OF_SPEECH_MS`** is the main latency lever. Added to every turn.
Below roughly 350 ms it cuts off people who pause mid-sentence.

**`GEMINI_THINKING_LEVEL`** at `minimal` for fastest first audio.

**Brief size** trades latency against how many questions avoid a lookup. Around
900 tokens gave sub-second first audio here.

**`stall_after_ms`** above the measured tool time, so the nudge rarely fires.

Use `tools/sweep_latency.py` rather than a phone call per hypothesis. Each run
costs a few seconds of audio.

---

## 12. Demo runbook

Before the call:

```bash
make test && make check
python tools/probe_gemini.py --repeat 3
curl -s https://<host>/health
curl -s "https://<host>/api/lookup?q=<a question you know works>"
python scripts/webhook.py status      # confirm what you are about to overwrite
python scripts/webhook.py point https://<host>
```

Open the screen on a laptop. It shows the transcript, the cited source URLs and
the latency, and it is the part that actually sells the system.

A five-turn arc that works:

1. **"What do you do?"** Instant, from the brief.
2. **"How much is X, and what ages?"** Two facts, cites the page. Point at the
   source panel.
3. **Something the site genuinely does not publish.** It refuses and offers to
   take a message. Say the line out loud: their site does not publish this, so
   it will not guess; give us the real values and it answers them tomorrow. The
   refusal is the feature, and it lands hardest after two confident answers.
4. **"Can someone call me back?"** Takes a message, reads the number back.
5. **Interrupt it.** Shows a conversation rather than a recording.

Have the answer ready for "what if we want it to know X": one plain-text facts
file, no re-crawl.

Afterwards: `python scripts/webhook.py restore`.

---

## 13. Porting to a different site

1. `python -m app.ingest <url>`, or crawl elsewhere and `--from-dir`.
2. Check the reported average chunk size is above 400 characters.
3. Rewrite `questions.txt` for the new business. Keep three deliberately
   unrelated controls at the bottom.
4. Run `eval.py`, take the lowest clean split, set `MIN_SCORE`.
5. Read the generated brief. It is the highest-leverage artifact and a bad one
   is visible immediately.
6. Fetch two or three pages by hand and check the brief against them. Do not
   assert what the site lacks without looking.
7. `data/facts.md` only if the business supplies real values. Never ship the
   template's placeholders; an agent reading invented times to the person who
   owns the business is the worst available outcome.
8. Set `SITE_TIMEZONE`.
9. Probe, deploy, compare `/health`, then dial.

---

## Appendix: the shortest possible version

- Clone the working voice implementation. Do not reconstruct it.
- No pacing sleeps. No ws-ping flags. `<Connect><Stream>`. `response.data`.
  `google-genai>=1.9`. `${PORT}`. `--proxy-headers`. `--workers 1`.
- Snapshot any collection before iterating it across an `await`.
- The observer never affects the call.
- Merge chunks to ~1100 chars. Strip cross-page chrome including headings.
- Sweep the threshold against control questions. Leave z alone.
- The model judges relevance, not the threshold.
- A timeout is not a miss.
- Report config on `/health` and compare environments before dialling.
- Build the diagnostic before the third guess.
- Never assert what a site does not contain without looking.
