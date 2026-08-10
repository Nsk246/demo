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
