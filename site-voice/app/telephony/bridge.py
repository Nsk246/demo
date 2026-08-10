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
        stall_after_ms: int = 1500,
        barge_rms_threshold: int = BARGE_RMS_THRESHOLD,
        barge_sustain_frames: int = BARGE_SUSTAIN_FRAMES,
        barge_rms_floor: int = 300,
        barge_noise_multiple: float = 3.5,
        suppress_cap_s: float = 4.0,
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
        # A tool slower than this gets the agent to say something. Set above
        # the measured lookup time on purpose: every nudge is a chance for the
        # model to keep generating past the holding phrase and invent an
        # answer, which it has done. Lookups here land around 1100ms, so at
        # 1500ms a normal one never triggers this path at all.
        self.stall_after_ms = stall_after_ms
        # Tunable per line. Lower the threshold if the agent keeps talking
        # over the caller; raise it if background noise cuts the agent off.
        # Sustain frames are 20ms each, so 3 is 60ms of continuous speech.
        self.barge_rms_threshold = barge_rms_threshold
        self.barge_sustain_frames = barge_sustain_frames
        # Never go below this, however quiet the line. The fixed threshold
        # that worked before adaptation was 550, so going far under it invites
        # echo and hum being read as speech.
        self.barge_rms_floor = barge_rms_floor
        self.barge_noise_multiple = barge_noise_multiple
        self.suppress_cap_s = suppress_cap_s
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
        self._media_frames = 0
        self._stop_reason = "socket closed without a stop event"
        self._agent_frames = 0
        self._suppress_audio = False
        self._suppressed_at = 0.0
        # Noise floor, learned from the line rather than assumed. A fixed RMS
        # threshold works on the line it was tuned on and fails on the next
        # one: a quiet mobile never reaches it, a noisy speakerphone sits
        # above it permanently.
        self._noise_floor = float(barge_rms_threshold) / 3.5

    # ---------------------------------------------------------------- helpers

    async def _emit(self, payload: dict) -> None:
        """Send an event to the observer, and never let it end the call.

        The observer is a monitoring screen. Nothing it does should reach the
        caller. Without this guard a browser reconnecting at the wrong moment
        raised out of the fan-out, through here, and killed the bridge task.
        """
        if not self.on_event:
            return
        try:
            await self.on_event(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("monitor fan-out failed, call continues: %r", exc)

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
        # The model is still generating. Clearing the queue is not enough:
        # the next audio event would flip _agent_speaking back on and the
        # agent would resume talking a second after being cut off, until the
        # server's own detection caught up. Drop its remaining audio until
        # this turn actually ends.
        self._suppress_audio = True
        self._suppressed_at = time.monotonic()
        await self._emit({"type": "barge_in"})

    # ------------------------------------------------------------- pump: in

    async def _phone_to_provider(self) -> None:
        """Twilio receive loop. Ends when the call ends.

        How it ends is logged. A silent exit here looks identical whether
        Twilio hung up, the socket dropped, or the model died, and those need
        different fixes.
        """
        self._media_frames = 0
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

                # Learn the line's noise floor while the agent is silent,
                # and only from quiet frames, so the caller's own speech does
                # not drag the floor up. Echo during agent speech is excluded
                # for the same reason.
                if not self._agent_speaking:
                    # Asymmetric on purpose. A symmetric average took four
                    # seconds to settle from its starting guess, which is most
                    # of a short call. Falling fast tracks a quiet line almost
                    # immediately; rising slowly stops one loud word from
                    # dragging the floor up and deafening the next
                    # interruption.
                    if rms < self._noise_floor:
                        self._noise_floor = 0.90 * self._noise_floor + 0.10 * rms
                    elif rms < self._noise_floor * 2.5:
                        self._noise_floor = 0.995 * self._noise_floor + 0.005 * rms
                threshold = max(
                    self.barge_rms_floor, self._noise_floor * self.barge_noise_multiple
                )

                if self._agent_speaking:
                    if rms > threshold:
                        self._loud_frames += 1
                        if self._loud_frames >= self.barge_sustain_frames:
                            await self._barge_in()
                    else:
                        self._loud_frames = 0
                else:
                    # Caller is talking; the clock for their turn keeps moving.
                    if rms > threshold:
                        self._turn.caller_stopped_at = None
                    elif self._turn.caller_stopped_at is None:
                        self._turn.caller_stopped_at = time.time()

                self._media_frames += 1
                await self.provider.send_audio(
                    A.resample(samples, 8000, self.provider.input_hz).tobytes()
                )
                # Only speech-bearing frames. Stamping every frame, silence
                # included, measured the gap to the most recent silent packet
                # and reported a meaningless single-digit millisecond figure.
                if rms > threshold:
                    self._turn.last_speech_sent_at = time.time()

            elif event == "stop":
                log.info(
                    "twilio sent stop after %d inbound frames (%.1fs of audio); "
                    "the caller or Twilio ended the call",
                    self._media_frames,
                    self._media_frames * 0.02,
                )
                self._stop_reason = "twilio stop"
                break

            elif event == "mark":
                pass

    # ------------------------------------------------------------ pump: out

    async def _log_exit(self) -> None:
        log.info(
            "call ended: %s | caller audio %d frames | agent audio %d frames | "
            "turns %d | barge-ins %d | tool calls %d",
            self._stop_reason,
            self._media_frames,
            self._agent_frames,
            len(self.transcript),
            self.stats.barge_ins,
            len(self.tool_calls),
        )
        log.info(
            "agent spoke roughly %.1fs across the call",
            self._agent_frames * 0.02,
        )
        if self._agent_frames and not self._media_frames:
            log.warning(
                "the agent spoke but no caller audio ever arrived. Twilio is "
                "not sending inbound media, which usually means a one-way "
                "stream: check the TwiML uses <Connect><Stream>, not "
                "<Start><Stream>."
            )
        if self._media_frames and not self._agent_frames:
            log.warning(
                "caller audio arrived but the agent never produced any. The "
                "model session opened and then said nothing."
            )

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
                if self._suppress_audio:
                    # Cap it. If a turn_end never arrives the agent would be
                    # mute for the rest of the call, which is worse than a
                    # little overlap.
                    if time.monotonic() - self._suppressed_at < self.suppress_cap_s:
                        continue
                    self._suppress_audio = False
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
                # The model finished, or the server noticed the interruption.
                # Either way the tail we were dropping is over.
                self._suppress_audio = False
                self._agent_speaking = False
                self._spoken_this_turn += self._pending_this_turn
                self._pending_this_turn = ""
                self._turn = TurnTiming()
                self._spoken_this_turn = ""
                await self._emit({"type": "turn_end"})

            elif ev.kind == "error":
                log.error("provider error, ending the call: %s", ev.detail)
                self._stop_reason = f"provider error: {ev.detail}"
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
                    "(SYSTEM: the lookup is still running and you do not have "
                    "the answer yet. Say exactly one short holding phrase of "
                    "three or four words, such as 'one moment please', and "
                    "then STOP. Do not answer the question. Do not state any "
                    "fact, price, time, or address. The result is coming.)"
                )
            await self._emit({"type": "stalling", "name": ev.tool_name})
            remaining = max(0.1, (self.tool_timeout_ms - self.stall_after_ms) / 1000)
            try:
                result = await asyncio.wait_for(task, timeout=remaining)
            except TimeoutError:
                task.cancel()
                # Not `found: false`. That would have the agent tell the
                # caller the answer is not on the site, which is a different
                # and false statement.
                result = {
                    "error": "timeout",
                    "hint": (
                        "The search did not finish in time. This does not mean "
                        "the answer is missing. Say you are having trouble "
                        "pulling it up and offer to take a message. Do not say "
                        "it is not on the site."
                    ),
                }
                await self._emit({"type": "tool_slow", "name": ev.tool_name})
            except Exception as exc:
                result = {
                    "error": "failed",
                    "hint": (
                        "The lookup failed. Apologise briefly and offer to take "
                        "a message. Do not say the answer is not on the site."
                    ),
                }
                await self._emit({"type": "error", "detail": str(exc)})
        except Exception as exc:
            result = {
                "error": "failed",
                "hint": (
                    "The lookup failed. Apologise briefly and offer to take a "
                    "message. Do not say the answer is not on the site."
                ),
            }
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
            self._agent_frames += 1
            self._spoken_this_turn = self._pending_this_turn
            # Drain whatever else is ready without waiting on the clock.
            while True:
                try:
                    await self._send_to_twilio(self._outbound.get_nowait())
                    self._agent_frames += 1
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
            await self._log_exit()
        return self.stats.summary()
