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
        end_of_speech_sensitivity: str = "END_SENSITIVITY_LOW",
        start_of_speech_sensitivity: str = "",
        prefix_padding_ms: int = 0,
        vocabulary: list[str] | None = None,
        temperature: float | None = None,
        affective_dialog: bool = False,
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
        self.end_of_speech_sensitivity = end_of_speech_sensitivity
        self.start_of_speech_sensitivity = start_of_speech_sensitivity
        self.prefix_padding_ms = prefix_padding_ms
        self.vocabulary = list(vocabulary or [])[:100]
        self.temperature = temperature
        self.affective_dialog = affective_dialog
        self._validate()
        self._session = None
        self._ctx = None
        self._entered = False
        self._closed = False

    async def connect(self, *, instructions: str, tools: list[dict]) -> None:
        from google import genai  # imported lazily so tests need no SDK

        self._require_sdk()
        self._validate()
        self._client = genai.Client(api_key=self.api_key)
        config = self.build_config(instructions, tools)

        # Retry once. Live sessions are rate limited by concurrent count on
        # the developer tier, and a slot freed by the previous call is often
        # a second or two behind the call that freed it.
        last: Exception | None = None
        for attempt in range(2):
            self._ctx = self._client.aio.live.connect(model=self.model, config=config)
            try:
                self._session = await self._ctx.__aenter__()
                self._entered = True
                return
            except asyncio.CancelledError:
                # The caller timed us out. Drop the context without entering
                # it, or close() will call __aexit__ on something that never
                # opened and the session slot stays held.
                self._ctx = None
                raise
            except Exception as exc:
                last = exc
                self._ctx = None
                if attempt == 0:
                    log.warning(
                        "live session did not open (%s: %s), retrying once",
                        type(exc).__name__, exc,
                    )
                    await asyncio.sleep(1.5)
        # Model ids churn on the developer tier. Say which one failed rather
        # than surfacing a bare 404 from deep in the SDK.
        detail = f"{type(last).__name__}: {last}"
        hint = ""
        if "1011" in detail and self.affective_dialog:
            # Measured on gemini-3.1-flash-live-preview: enabling affective
            # dialog fails the handshake with a bare internal error rather
            # than saying the option is unsupported.
            hint = (
                " GEMINI_AFFECTIVE_DIALOG is on and this model does not "
                "support it. The handshake fails with a bare internal error "
                "rather than naming the option. Turn it off, or move to a "
                "native-audio model."
            )
        elif "429" in detail or "RESOURCE_EXHAUSTED" in detail.upper():
            hint = (
                " This is a quota error, not a code problem. The Live API "
                "limits concurrent sessions by usage tier and the free tier "
                "is very low, so a call placed while another is still open "
                "gets refused. Enable billing on the project for Tier 1, "
                "which is pay as you go and costs almost nothing at demo "
                "volume."
            )
        # `from last`, not `from exc`: Python deletes the `except ... as exc`
        # name at the end of its block, so referencing it here is a NameError
        # on the exact path that was supposed to report the real failure.
        raise RuntimeError(
            f"could not open a Gemini Live session with model {self.model!r}. "
            f"Check the model id is current at "
            f"https://ai.google.dev/gemini-api/docs/models. Underlying error: "
            f"{detail}.{hint}"
        ) from last

    def build_config(self, instructions: str, tools: list[dict]) -> dict:
        """Assemble the session config.

        Separate from connect() so it can be asserted in a test. A mistyped
        key here is either silently ignored by the API or fails the handshake,
        and either way the first evidence is a phone call going wrong.
        """
        config: dict = {
            "response_modalities": ["AUDIO"],
            "system_instruction": instructions,
            "speech_config": {
                "voice_config": {"prebuilt_voice_config": {"voice_name": self.voice}}
            },
            # Words the transcriber will otherwise mangle. On real calls
            # "RobotiX" came back as "New Teach" and "7th grader" as "207
            # grader", and the agent then answered a question nobody asked.
            # These are the business's own proper nouns, so they are exactly
            # what the model has least chance of guessing.
            "input_audio_transcription": (
                {"adaptation_phrases": self.vocabulary} if self.vocabulary else {}
            ),
            "output_audio_transcription": {},
        }
        if self.temperature is not None:
            # Higher is less flat and more varied in phrasing. Too high and it
            # starts embellishing facts, which matters more here than sounding
            # lively, so this stays conservative.
            config["temperature"] = self.temperature
        if self.affective_dialog:
            # Lets the model read and match the caller's tone. Supported on
            # native-audio models; on others the session may refuse to open,
            # which is why it is off unless asked for.
            config["enable_affective_dialog"] = True
        if tools:
            config["tools"] = [{"function_declarations": tools}]
        if self.thinking_level:
            # Lower thinking means faster first audio. On a phone call the
            # caller hears the delay, so this is not a free knob.
            config["thinking_config"] = {"thinking_level": self.thinking_level}

        # Turn taking. Only the two settings that cannot make the agent deaf
        # are on by default.
        #
        # silence_duration_ms is how long a pause must last before the model
        # decides the caller finished. At 500ms it interrupts anyone who
        # pauses to think, which on a phone call is most people.
        #
        # end_of_speech_sensitivity LOW makes it require more confidence
        # before ending the caller's turn. Worst case it waits longer, so it
        # is safe to default on.
        #
        # start_of_speech_sensitivity and prefix_padding_ms are off by
        # default and deliberately so. prefix_padding_ms is the duration of
        # speech required before start-of-speech commits, not padding around
        # it. Set to 300 with LOW start sensitivity, a caller answering "yes"
        # or "no" in under a third of a second can be dropped entirely. That
        # trades an eager agent for a deaf one on the shortest and most
        # important turns.
        detection: dict = {}
        if self.end_of_speech_silence_ms:
            detection["silence_duration_ms"] = self.end_of_speech_silence_ms
        if self.end_of_speech_sensitivity:
            detection["end_of_speech_sensitivity"] = self.end_of_speech_sensitivity
        if self.start_of_speech_sensitivity:
            detection["start_of_speech_sensitivity"] = self.start_of_speech_sensitivity
        if self.prefix_padding_ms:
            detection["prefix_padding_ms"] = self.prefix_padding_ms

        realtime: dict = {
            # The caller can always cut the agent off. The alternative,
            # NO_INTERRUPTION, makes the agent finish its sentence over them.
            "activity_handling": "START_OF_ACTIVITY_INTERRUPTS",
        }
        if detection:
            realtime["automatic_activity_detection"] = detection
        config["realtime_input_config"] = realtime
        return config

    VALID_END = {"END_SENSITIVITY_LOW", "END_SENSITIVITY_HIGH", ""}
    VALID_START = {"START_SENSITIVITY_LOW", "START_SENSITIVITY_HIGH", ""}

    def _validate(self) -> None:
        """Reject bad enum values before a caller is on the line.

        An unrecognised value fails the handshake, and a failed handshake is
        a silent line.
        """
        if self.end_of_speech_sensitivity not in self.VALID_END:
            raise ValueError(
                f"GEMINI_END_OF_SPEECH_SENSITIVITY={self.end_of_speech_sensitivity!r} "
                f"is not valid. Use one of {sorted(self.VALID_END - {''})}."
            )
        if self.start_of_speech_sensitivity not in self.VALID_START:
            raise ValueError(
                f"GEMINI_START_OF_SPEECH_SENSITIVITY="
                f"{self.start_of_speech_sensitivity!r} is not valid. Use one of "
                f"{sorted(self.VALID_START - {''})}."
            )

    @staticmethod
    def _require_sdk() -> None:
        """Check the SDK surface before a caller is on the line.

        `send_realtime_input` and `send_tool_response` arrived in google-genai
        1.9.0. On an older one the session opens, the caller hears the line go
        live, and then an AttributeError kills the call with nothing said. Fail
        at connect instead, where the log is readable.
        """
        import google.genai
        from google.genai import live

        missing = [
            name
            for name in ("send_realtime_input", "send_tool_response")
            if not hasattr(live.AsyncSession, name)
        ]
        if missing:
            raise RuntimeError(
                f"google-genai {getattr(google.genai, '__version__', 'unknown')} "
                f"is too old: it has no {', '.join(missing)}. Needs 1.9.0 or "
                f"later. Fix requirements.txt and redeploy."
            )

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
        if self._ctx is not None and self._entered:
            try:
                await self._ctx.__aexit__(None, None, None)
            except Exception as exc:
                # A failed teardown must never mask the call's real outcome.
                log.warning("gemini session teardown failed: %s", exc)
