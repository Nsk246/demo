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
    nudge = next((t for t in provider.sent_text if "lookup is still running" in t), "")
    assert nudge, "the agent was never told to hold the line"
    # The nudge must forbid answering. On a real call a softer wording let the
    # model run past its holding phrase and invent opening hours.
    assert "Do not answer the question" in nudge
    assert "Do not state any fact" in nudge
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


@pytest.mark.asyncio
async def test_a_failing_monitor_cannot_end_the_call():
    """The screen is an observer. A browser reconnecting at the wrong moment
    used to raise out of the fan-out and kill the bridge, which looked like a
    random disconnect with nothing in the log."""
    ws = FakeTwilioWS([start_msg()] + [media_msg(quiet()) for _ in range(10)] + [STOP])

    async def hostile(_payload):
        raise RuntimeError("Set changed size during iteration")

    provider = MockProvider([ProviderEvent(kind="audio", audio=tone(200))])
    bridge = MediaBridge(ws, provider, on_event=hostile)
    await asyncio.wait_for(bridge.run(), timeout=5)
    # The call ran to Twilio's stop rather than dying on the observer.
    assert ws.events("media"), "audio must still reach the caller"


@pytest.mark.asyncio
async def test_the_agent_does_not_resume_after_being_interrupted():
    """Clearing the queue is not enough. The model is still generating, and
    the next audio event used to flip speaking back on, so the agent resumed
    a second after being cut off. That is what makes barge-in feel unreliable."""
    ws = FakeTwilioWS(
        [start_msg()]
        + [media_msg(loud()) for _ in range(BARGE_SUSTAIN_FRAMES + 2)]
        + [media_msg(quiet()) for _ in range(20)]
        + [STOP]
    )
    provider = MockProvider([ProviderEvent(kind="audio", audio=tone(1500))])
    bridge = MediaBridge(ws, provider)
    task = asyncio.ensure_future(bridge.run())
    await asyncio.sleep(0.15)
    # The model keeps talking after the interruption, as it really does.
    provider.push(ProviderEvent(kind="audio", audio=tone(1500)))
    await asyncio.wait_for(task, timeout=5)

    assert bridge.stats.barge_ins == 1
    clear_at = next(i for i, m in enumerate(ws.sent) if m.get("event") == "clear")
    after = [m for m in ws.sent[clear_at:] if m.get("event") == "media"]
    assert not after, f"{len(after)} frames reached the caller after the interruption"


@pytest.mark.asyncio
async def test_suppression_lifts_when_the_turn_ends():
    """Otherwise a missing turn_end would mute the agent for the whole call."""
    ws = FakeTwilioWS(
        [start_msg()]
        + [media_msg(loud()) for _ in range(BARGE_SUSTAIN_FRAMES + 2)]
        + [media_msg(quiet()) for _ in range(40)]
        + [STOP]
    )
    provider = MockProvider([ProviderEvent(kind="audio", audio=tone(1500))])
    bridge = MediaBridge(ws, provider)
    task = asyncio.ensure_future(bridge.run())
    await asyncio.sleep(0.15)
    provider.push(ProviderEvent(kind="turn_end"))
    await asyncio.sleep(0.05)
    provider.push(ProviderEvent(kind="audio", audio=tone(200)))
    await asyncio.wait_for(task, timeout=5)

    clear_at = next(i for i, m in enumerate(ws.sent) if m.get("event") == "clear")
    after = [m for m in ws.sent[clear_at:] if m.get("event") == "media"]
    assert after, "the next turn must be audible"


@pytest.mark.asyncio
async def test_a_quiet_line_can_still_interrupt():
    """A fixed threshold works on the line it was tuned on. On a quiet mobile
    the caller never reaches it and interruption simply does not happen.

    The silent frames come before the agent speaks, as on a real call: that
    is the window in which the line's noise floor is learned.
    """
    soft = (np.random.randn(160) * 700).astype(np.int16)
    ws = FakeTwilioWS(
        [start_msg()]
        + [media_msg(np.zeros(160, dtype=np.int16)) for _ in range(40)]
        + [media_msg(soft) for _ in range(BARGE_SUSTAIN_FRAMES + 4)]
        + [STOP]
    )
    provider = MockProvider()
    # Threshold set for a loud line; this caller never gets near it.
    bridge = MediaBridge(ws, provider, barge_rms_threshold=5000)
    task = asyncio.ensure_future(bridge.run())
    await asyncio.sleep(0.15)          # the floor learns from the quiet line
    provider.push(ProviderEvent(kind="audio", audio=tone(2000)))
    await asyncio.wait_for(task, timeout=5)
    assert bridge.stats.barge_ins == 1, "adaptive floor should catch soft speech"


@pytest.mark.asyncio
async def test_the_learned_floor_never_drops_below_the_hard_minimum():
    """On a perfectly silent line the floor would decay toward zero and line
    hum would start registering as speech."""
    ws = FakeTwilioWS(
        [start_msg()]
        + [media_msg(np.zeros(160, dtype=np.int16)) for _ in range(120)]
        + [STOP]
    )
    bridge = MediaBridge(ws, MockProvider(), barge_rms_floor=300)
    await asyncio.wait_for(bridge.run(), timeout=5)
    assert bridge.stats.barge_ins == 0
