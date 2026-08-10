"""FastAPI entrypoint: Twilio webhook, media stream bridge, monitor socket."""

from __future__ import annotations

import contextlib
import json
import logging
import pathlib
import time
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
        end_of_speech_sensitivity=settings.gemini_end_of_speech_sensitivity,
        start_of_speech_sensitivity=settings.gemini_start_of_speech_sensitivity,
        prefix_padding_ms=settings.gemini_prefix_padding_ms,
    )


@app.get("/health")
async def health():
    actual = effective_provider()
    try:
        import google.genai

        sdk = getattr(google.genai, "__version__", "unknown")
    except Exception:
        sdk = "not installed"
    body = {
        "ok": True,
        "provider": actual,
        "google_genai": sdk,
        "embedding_model": settings.embedding_model,
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
        """Push an event to every open screen.

        Iterate a snapshot, not the live set. `send_text` is a suspension
        point, and the screen reconnects every 1.5 seconds when its socket
        drops, so a browser reconnecting mid-iteration mutates the set being
        walked and Python raises "Set changed size during iteration". That
        used to escape into the bridge and end the call, which looked like a
        random disconnect with nothing in the log.
        """
        payload["call_id"] = call_id
        subscribers = list(_monitors.get("*", ()))
        dead = set()
        for sub in subscribers:
            try:
                await sub.send_text(json.dumps(payload))
            except Exception:
                dead.add(sub)
        if dead:
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
        stall_after_ms=settings.stall_after_ms,
        barge_rms_threshold=settings.barge_rms_threshold,
        barge_sustain_frames=settings.barge_sustain_frames,
        # Two connect attempts with a pause between them do not fit in ten
        # seconds, and a timeout here cancels the retry that would have worked.
        connect_timeout_s=20.0,
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


@app.get("/api/lookup")
async def lookup(q: str):
    """Run the production retrieval path over HTTP.

    Same index, same settings, same embedding call the agent makes. A caller
    reporting "nothing on the site" for something that scores well locally is
    otherwise impossible to tell apart from a slow network, a stale database,
    or a threshold difference between environments.
    """
    if INDEX is None:
        return {"error": "no site loaded"}

    from .embed import embed_query

    # Report the configuration on the failure path too. An error alone does
    # not say which model was asked for, and a 404 from the embedding endpoint
    # is almost always a model name that differs between environments.
    config = {
        "embedding_model": settings.embedding_model,
        "embedding_dims": settings.embedding_dims,
        "min_score": settings.min_score,
        "min_z": settings.min_z,
        "top_k": settings.top_k,
        "key_fingerprint": (
            f"{settings.gemini_api_key[:6]}...{settings.gemini_api_key[-4:]}"
            f" ({len(settings.gemini_api_key)} chars)"
            if settings.gemini_api_key else "EMPTY"
        ),
        "index_dims": INDEX.dims,
        "index_chunks": INDEX.size,
    }

    started = time.monotonic()
    try:
        vec = await embed_query(
            q,
            api_key=settings.gemini_api_key,
            model=settings.embedding_model,
            dims=settings.embedding_dims,
        )
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        hint = ""
        if "404" in detail:
            hint = (
                "A 404 from the embedding endpoint means the model name is not "
                "available to this key. Check EMBEDDING_MODEL in the Railway "
                "variables matches the local .env, and that the SDK version "
                "matches too: a name valid in one google-genai release can 404 "
                "in another."
            )
        return {"error": detail, "hint": hint, "config": config}
    embed_ms = (time.monotonic() - started) * 1000

    hits = INDEX.search(
        vec, top_k=settings.top_k, min_score=settings.min_score,
        min_z=settings.min_z,
    )
    # Also search with no floor, so a threshold problem is distinguishable
    # from an index or embedding problem at a glance.
    unfiltered = INDEX.search(vec, top_k=5, min_score=0.0, min_z=0.0)
    return {
        "query": q,
        "embed_ms": round(embed_ms),
        "embed_dims": len(vec),
        "config": config,
        "stats": INDEX.last_stats,
        "returned": [
            {"score": round(h.score, 3), "url": h.url, "section": h.heading}
            for h in hits
        ],
        "best_regardless_of_threshold": [
            {"score": round(h.score, 3), "url": h.url, "section": h.heading}
            for h in unfiltered
        ],
    }


@app.get("/api/calls")
async def calls():
    return {"calls": _history}


@app.get("/", response_class=HTMLResponse)
async def screen():
    html = (pathlib.Path(__file__).with_name("screen.html")).read_text()
    return html.replace("__SITE__", SITE.get("name", "no site ingested")).replace(
        "__ROOT__", SITE.get("root_url", "")
    )
