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
