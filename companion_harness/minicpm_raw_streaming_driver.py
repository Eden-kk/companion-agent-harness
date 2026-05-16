"""MiniCPMRawStreamingDriver — demo-mode pipeline that bypasses SpeakPolicy.

# DEMO MODE: bypasses SpeakPolicy + audit gates per spec invariants #2/#4.

API CONSTRAINT NOTE (discovered during implementation):
  MiniCPMStreamingModel does not expose a continuous audio-in/audio-out
  streaming_generate() interface. The duplex object is constructed with
  generate_audio=False; audio output requires a separate TtsAdapter step.
  This driver uses infer_stream() (the real available API), which yields
  ThinkerProposal (text) candidates. Those proposals bypass SpeakPolicy
  and are fed directly to the provided tts_adapter for synthesis.

  The "raw" in --minicpm-streaming-raw refers to bypassing the policy/ASR/
  addressing gates, not to raw audio-to-audio generation, which is not
  currently supported by the MiniCPMStreamingModel API surface.

DEDUP STRATEGY (Option C — debounce-then-synthesize):
  MiniCPM emits proposal bursts every ~100ms while generating. Option A
  (cancel-previous) caused indefinite delay: each new proposal cancelled the
  in-flight TTS so audio never started until proposals stopped. Option C
  waits DEBOUNCE_MS after the last proposal before synthesizing. While
  waiting, earlier debounce tasks are silently cancelled; only the final
  (most complete) proposal content is synthesized. No audit event is emitted
  for pure debounce-overruns — the superseded event is reserved for cases
  where synthesis had already started on substantially different content.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import traceback
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

_log = logging.getLogger(__name__)

from companion_harness.schemas import Event

__all__ = ["MiniCPMRawStreamingDriver"]

_SCHEMA_VERSION = "v0.1g"
_SOURCE = "minicpm_raw_streaming_driver"
_DEBOUNCE_MS = 300


def _make_session_started_event(
    session_id: str,
    seq: int,
    raw_audio_event_id: str | None,
) -> Event:
    now_ms = int(time.monotonic() * 1000)
    event_id = f"minicpm_raw_streaming_session_started-{uuid.uuid4().hex[:12]}"
    payload = {
        "warning": "policy/audit gates bypassed; demo mode only",
        "bypassed": ["SpeakPolicy", "ASR", "AddressingClassifier", "VAD", "SmartTurn", "Backchannel"],
    }
    payload_hash = hashlib.sha256(str(payload).encode()).hexdigest()[:16]
    caused_by = [raw_audio_event_id] if raw_audio_event_id else []
    return Event(
        event_id=event_id,
        session_id=session_id,
        schema_version=_SCHEMA_VERSION,
        seq_no=seq,
        event_type="minicpm_raw_streaming_session_started",
        timestamp_mono_ms=now_ms,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
        source=_SOURCE,
        caused_by=caused_by,
        payload_hash=payload_hash,
        payload_ref=None,
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="signal_default_30d",
        payload_inline=payload,
    )


class MiniCPMRawStreamingDriver:
    """DEMO MODE: bypasses SpeakPolicy + audit gates per spec invariants #2/#4.

    Subscribes to the audio_in queue and pumps chunks through
    foreground_model.infer_stream(), yielding proposals directly to
    tts_adapter.synthesize() without going through SpeakPolicy.

    Emits exactly one minicpm_raw_streaming_session_started event per session.

    No policy_decision, asr_transcript_emitted, or addressing_classified events
    are emitted. raw_audio_chunk events are still logged by InputIngest upstream
    (input audit preserved per invariant #1 for inputs).
    """

    def __init__(
        self,
        *,
        session_id: str,
        foreground_model: Any,
        tts_adapter: Any,
        audio_out_broker: Any,
        logger: Any,
    ) -> None:
        self._session_id = session_id
        self._foreground = foreground_model
        self._tts = tts_adapter
        self._broker = audio_out_broker
        self._logger = logger
        self._audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)
        self._seq = 0
        self._audio_seq = 0
        self._session_event_emitted = False
        self._task: asyncio.Task[None] | None = None
        self._tts_in_flight: bool = False
        self._debounce_task: asyncio.Task[None] | None = None

    @property
    def audio_in(self) -> asyncio.Queue[tuple[bytes, str]]:
        return self._audio_in

    def push_audio(self, frame_bytes: bytes, raw_audio_event_id: str) -> None:
        """Enqueue audio frame. Drop-oldest on overflow (invariant #10)."""
        try:
            self._audio_in.put_nowait((frame_bytes, raw_audio_event_id))
        except asyncio.QueueFull:
            try:
                self._audio_in.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._audio_in.put_nowait((frame_bytes, raw_audio_event_id))
            except asyncio.QueueFull:
                pass

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _emit_session_started(self, raw_audio_event_id: str | None) -> None:
        if self._session_event_emitted:
            return
        self._session_event_emitted = True
        evt = _make_session_started_event(
            session_id=self._session_id,
            seq=self._next_seq(),
            raw_audio_event_id=raw_audio_event_id,
        )
        self._logger.log(evt)

    async def _frame_iter(self) -> AsyncIterator[tuple[bytes, bytes | None]]:
        """Drain audio_in and yield (audio_bytes, None) — no video in raw mode."""
        while True:
            frame_bytes, raw_audio_event_id = await self._audio_in.get()
            self._emit_session_started(raw_audio_event_id)
            yield frame_bytes, None  # type: ignore[misc]

    def _make_tts_error_event(self, error_summary: str) -> Event:
        now_ms = int(time.monotonic() * 1000)
        event_id = f"raw_tts_synthesis_error-{uuid.uuid4().hex[:12]}"
        payload: dict = {"error_summary": error_summary[:256]}
        payload_hash = hashlib.sha256(str(payload).encode()).hexdigest()[:16]
        return Event(
            event_id=event_id,
            session_id=self._session_id,
            schema_version=_SCHEMA_VERSION,
            seq_no=self._next_seq(),
            event_type="raw_tts_synthesis_error",
            timestamp_mono_ms=now_ms,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source=_SOURCE,
            caused_by=[],
            payload_hash=payload_hash,
            payload_ref=None,
            payload_kind="signal",
            subject_class="self",
            sensitivity="safe",
            retention_policy_id="signal_default_30d",
            payload_inline=payload,
        )

    def _make_superseded_event(self, proposal_content: str) -> Event:
        now_ms = int(time.monotonic() * 1000)
        event_id = f"raw_proposal_superseded_by_newer-{uuid.uuid4().hex[:12]}"
        payload: dict = {"superseded_content_len": len(proposal_content)}
        payload_hash = hashlib.sha256(str(payload).encode()).hexdigest()[:16]
        return Event(
            event_id=event_id,
            session_id=self._session_id,
            schema_version=_SCHEMA_VERSION,
            seq_no=self._next_seq(),
            event_type="raw_proposal_superseded_by_newer",
            timestamp_mono_ms=now_ms,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source=_SOURCE,
            caused_by=[],
            payload_hash=payload_hash,
            payload_ref=None,
            payload_kind="signal",
            subject_class="self",
            sensitivity="safe",
            retention_policy_id="signal_default_30d",
            payload_inline=payload,
        )

    async def _debounce_then_synthesize(self, text: str) -> None:
        """Wait DEBOUNCE_MS, then synthesize.  Silently exits if cancelled (newer proposal arrived)."""
        try:
            await asyncio.sleep(_DEBOUNCE_MS / 1000)
        except asyncio.CancelledError:
            return  # newer proposal arrived — abandon silently, no audit event
        await self._synthesize_and_publish(text)

    async def _synthesize_and_publish(self, text: str) -> None:
        """Synthesize TTS for one proposal and publish chunks.

        Runs as a separate task so the infer_stream consumer loop (and its
        frame_iter) keeps draining audio_in without waiting for synthesis.
        _tts_in_flight is cleared on completion or cancel.
        """
        try:
            async for chunk in self._tts.synthesize(text, []):
                if chunk and self._broker is not None:
                    self._audio_seq += 1
                    self._broker.publish(self._session_id, self._audio_seq, chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            summary = f"{type(exc).__name__}: {exc}"
            _log.error("raw TTS synthesis failed: %s\n%s", summary, traceback.format_exc())
            self._logger.log(self._make_tts_error_event(summary))
        finally:
            self._tts_in_flight = False

    async def _run(self) -> None:
        # DEMO MODE: bypasses SpeakPolicy + audit gates per spec invariants #2/#4.
        #
        # TTS synthesis is dispatched via a debounce task (Option C) so that
        # frame_iter continues draining audio_in while the debounce timer runs.
        # Without this, the audio_in queue (maxsize=64) saturates within ~1.3 s
        # at 50 fps, audio frames are dropped, and infer_stream never sees
        # silence/EOU — causing indefinite response delay.
        #
        # Debounce-then-synthesize (Option C): every new proposal updates the
        # accumulated content and resets the debounce timer. When no new proposal
        # has arrived for DEBOUNCE_MS, the latest content is synthesized once.
        # This ensures audio starts promptly after the proposal burst settles
        # rather than chasing every mid-thought partial (Option A bug).
        try:
            gen = await self._foreground.infer_stream(
                frame_iter=self._frame_iter(),
                caused_by=[],
            )
            async for proposal in gen:
                # Cancel any pending debounce for the prior proposal.
                if self._debounce_task is not None and not self._debounce_task.done():
                    self._debounce_task.cancel()
                    # No audit event — debounce-overrun is by design, not an audit-worthy drop.
                # Start a fresh debounce timer for the latest proposal content.
                self._debounce_task = asyncio.create_task(
                    self._debounce_then_synthesize(proposal.content)
                )
        except asyncio.CancelledError:
            if self._debounce_task is not None:
                self._debounce_task.cancel()
            raise
        except Exception:
            pass
        # Drain any pending debounce/TTS task before returning.
        if self._debounce_task is not None:
            await asyncio.gather(self._debounce_task, return_exceptions=True)
