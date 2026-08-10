#!/usr/bin/env python3
"""Sweep the settings that affect response latency, without a phone call.

A measured call on the restaurant build came back at 1614ms total with 1633ms
of that being the model round trip, so the network is not the problem and
there is nothing to gain from tuning it. What remains is the model: how long
it waits before deciding the caller finished, how much it thinks, and how much
prompt it carries.

Each run costs a few seconds of audio, so a full sweep is pennies. That is far
cheaper than a phone call per hypothesis.

    python tools/sweep_latency.py
    python tools/sweep_latency.py --runs 3
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.probe_gemini import real_context, speech_like  # noqa: E402


async def one_run(model, silence_ms, thinking, instructions, tools) -> float | None:
    """Time to first audio for a single turn."""
    from app.config import get_settings
    from app.providers.gemini import GeminiLiveProvider

    provider = GeminiLiveProvider(
        api_key=get_settings().gemini_api_key,
        model=model,
        thinking_level=thinking,
        end_of_speech_silence_ms=silence_ms,
    )
    try:
        await asyncio.wait_for(
            provider.connect(instructions=instructions, tools=tools), timeout=25
        )
    except Exception:
        return None

    await provider.send_audio(speech_like(0.6))
    await provider.send_text("A caller just said: what ages do you teach?")
    sent = time.time()
    first: float | None = None

    async def listen():
        nonlocal first
        async for ev in provider.receive():
            if ev.kind == "audio":
                first = time.time()
                return

    try:
        await asyncio.wait_for(listen(), timeout=25)
    except (TimeoutError, asyncio.TimeoutError):
        pass
    finally:
        await provider.close()
        # The Live API limits concurrent sessions. Back to back runs without a
        # pause refuse each other and the sweep measures nothing.
        await asyncio.sleep(2.0)

    return (first - sent) * 1000 if first else None


async def main() -> int:
    from app.config import get_settings

    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    settings = get_settings()
    if not settings.gemini_api_key:
        print("GEMINI_API_KEY is empty. Set it in .env.")
        return 1
    model = args.model or settings.gemini_live_model
    instructions, tools = real_context()

    print(f"model  {model}")
    print(f"prompt {len(instructions)} chars, {len(tools)} tools")
    print(f"{args.runs} runs per combination, 2s between sessions\n")
    print(f"{'silence':>8} {'thinking':>9} {'median':>8} {'runs':>18}")

    best = None
    for silence in (250, 500, 800):
        for thinking in ("minimal", "low"):
            times = []
            for _ in range(args.runs):
                ms = await one_run(model, silence, thinking, instructions, tools)
                if ms:
                    times.append(ms)
            if not times:
                print(f"{silence:>8} {thinking:>9} {'failed':>8}")
                continue
            med = statistics.median(times)
            print(f"{silence:>8} {thinking:>9} {med:>7.0f}ms "
                  f"{', '.join(f'{t:.0f}' for t in times):>18}")
            if best is None or med < best[0]:
                best = (med, silence, thinking)

    if best:
        print(f"\nfastest: {best[0]:.0f}ms at GEMINI_END_OF_SPEECH_MS={best[1]} "
              f"GEMINI_THINKING_LEVEL={best[2]}")
        print("Endpointing below roughly 350ms starts cutting off callers who "
              "pause mid-sentence, so the fastest number is not always the "
              "right one.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
