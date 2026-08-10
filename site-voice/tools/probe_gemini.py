#!/usr/bin/env python3
"""Check that Gemini Live actually works, without a phone in the way.

A call that connects and then sits in silence gives you nothing to debug: the
websocket is open, no exception is raised, and the logs look healthy. This
isolates the model session so a failure has a message attached to it.

    python tools/probe_gemini.py
    python tools/probe_gemini.py --model gemini-2.5-flash-native-audio-preview-12-2025

Run it twice in quick succession to test the thing that has actually been
failing here: the Live API limits concurrent sessions by usage tier, and a
second session opened while the first is still closing gets refused.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402


def speech_like(seconds: float = 1.0, rate: int = 16000) -> bytes:
    """A tone sweep. Not speech, but enough to make the model take a turn."""
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    return (np.sin(2 * np.pi * (200 + 400 * t) * t) * 8000).astype(np.int16).tobytes()


def real_context() -> tuple[str, list[dict]]:
    """The actual prompt and tools a call uses.

    Probing with a toy prompt measures the wrong thing. On the restaurant
    build a toy prompt came back at 714ms while real calls measured 1633ms,
    and the difference was the prompt body and the tool declarations. The site
    brief here is a similar size, so the benchmark has to carry it.
    """
    from app import agent as agent_mod
    from app.config import get_settings
    from app.store import connect, site_row

    settings = get_settings()
    row = site_row(connect(settings.site_db))
    if row is None:
        return "You answer the phone. Keep replies short.", []
    return (
        agent_mod.build(
            name=row["name"],
            brief=row["brief"],
            crawled_at=row["crawled_at"],
            facts=row["facts"] if "facts" in row.keys() else "",
            tz=settings.site_timezone,
        ),
        list(agent_mod.TOOL_SCHEMAS),
    )


async def probe(model: str, timeout: float) -> int:
    from app.config import get_settings, log_config_source
    from app.providers.gemini import GeminiLiveProvider

    settings = get_settings()
    key = settings.gemini_api_key
    if not key:
        print(f"FAIL: GEMINI_API_KEY is empty. Config came from {log_config_source()}.")
        return 1

    print(f"key      : {key[:6]}...{key[-4:]} ({len(key)} chars)")
    print(f"model    : {model}")
    instructions, tools = real_context()
    print(f"thinking : {settings.gemini_thinking_level}")
    print(f"endpoint : {settings.gemini_end_of_speech_ms}ms of silence")
    print(f"end sens : {settings.gemini_end_of_speech_sensitivity or 'API default'}")
    print(f"start sen: {settings.gemini_start_of_speech_sensitivity or 'API default'}")
    print(f"prefix   : {settings.gemini_prefix_padding_ms or 'API default'}")
    print(f"voice    : {settings.gemini_voice}")
    print(f"temp     : {settings.gemini_temperature if settings.gemini_temperature is not None else 'API default'}")
    print(f"affective: {settings.gemini_affective_dialog}")
    print(f"prompt   : {len(instructions)} chars (~{len(instructions) // 4} tokens)")
    print(f"tools    : {len(tools)}")

    from app import agent as agent_mod
    from app.store import connect, site_row

    row = site_row(connect(settings.site_db))
    vocab = (
        agent_mod.vocabulary(row["name"], row["brief"]) if row is not None else []
    )
    print(f"vocab    : {len(vocab)} phrases {vocab[:6]}")

    provider = GeminiLiveProvider(
        api_key=key,
        model=model,
        voice=settings.gemini_voice,
        thinking_level=settings.gemini_thinking_level,
        end_of_speech_silence_ms=settings.gemini_end_of_speech_ms,
        end_of_speech_sensitivity=settings.gemini_end_of_speech_sensitivity,
        start_of_speech_sensitivity=settings.gemini_start_of_speech_sensitivity,
        prefix_padding_ms=settings.gemini_prefix_padding_ms,
        vocabulary=vocab,
        temperature=settings.gemini_temperature,
        affective_dialog=settings.gemini_affective_dialog,
    )

    # Print what will actually be sent. A turn-taking setting that silently
    # failed to apply is otherwise only visible on a phone call.
    detection = provider.build_config(instructions, tools)["realtime_input_config"]
    print(f"turn cfg : {detection}")

    print("\n[1/3] opening a session...")
    started = time.time()
    try:
        await asyncio.wait_for(
            provider.connect(instructions=instructions, tools=tools), timeout=timeout
        )
    except (TimeoutError, asyncio.TimeoutError):
        print(f"FAIL: connect hung for {timeout}s with no error.")
        print("      Usually a model id the API accepts but never opens, a key")
        print("      without Live access, or the concurrent-session quota.")
        print("      Try --model with another id from")
        print("      https://ai.google.dev/gemini-api/docs/models")
        return 1
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")
        return 1
    print(f"      connected in {time.time() - started:.2f}s")

    print("\n[2/3] sending audio...")
    await provider.send_audio(speech_like(0.6))
    await provider.send_text("A caller just said: what ages do you teach?")

    print("\n[3/3] waiting for a response...")
    audio_bytes, events = 0, 0
    first_at: float | None = None
    sent_at = time.time()

    async def listen():
        nonlocal audio_bytes, events, first_at
        async for ev in provider.receive():
            events += 1
            if ev.kind == "audio":
                if first_at is None:
                    first_at = time.time()
                audio_bytes += len(ev.audio)
            elif ev.kind == "transcript":
                print(f"      transcript [{ev.role}]: {ev.text!r}")
            elif ev.kind == "tool_call":
                print(f"      tool call: {ev.tool_name}({ev.tool_args})")
            elif ev.kind == "error":
                print(f"      ERROR event: {ev.detail}")
                return

    try:
        await asyncio.wait_for(listen(), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        pass
    finally:
        await provider.close()

    print()
    if audio_bytes:
        ms = (first_at - sent_at) * 1000 if first_at else 0
        secs = audio_bytes / (provider.output_hz * 2)
        print(f"PASS: {events} events, first audio after {ms:.0f}ms, "
              f"{secs:.1f}s of speech")
        return 0

    print(f"FAIL: the session opened but produced no audio ({events} events).")
    print("      The model is reachable and is not speaking. Check the model")
    print("      supports the AUDIO response modality.")
    return 1


def main() -> int:
    from app.config import get_settings

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=None)
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--repeat", type=int, default=1,
                        help="run N times back to back to test session quota")
    args = parser.parse_args()
    model = args.model or get_settings().gemini_live_model

    worst = 0
    for i in range(args.repeat):
        if args.repeat > 1:
            print(f"\n{'=' * 60}\nrun {i + 1} of {args.repeat}\n{'=' * 60}")
        worst = max(worst, asyncio.run(probe(model, args.timeout)))
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
