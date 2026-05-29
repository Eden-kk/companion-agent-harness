"""ASRSidecar — parallel ASR task that fans out mic audio and emits asr_transcript_emitted.

Decoupled from the duplex model loop: audio is pushed via a bounded queue by
ContinuousLivePipeline.push_audio(); energy-VAD segments utterances; each
utterance is transcribed off-loop via ThreadPoolExecutor; the result is emitted
as asr_transcript_emitted (matching the turn-based emitter in realtime_orchestrator.py).
"""

from __future__ import annotations

import array
import asyncio
import hashlib
import math
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from companion_harness.schemas import Event

if TYPE_CHECKING:
    from companion_harness.event_logger import EventLogger

__all__ = ["ASRSidecar"]

_SOURCE = "asr_sidecar"
_SCHEMA_VERSION = "0.1"

# Energy-VAD parameters
_RMS_THRESHOLD: float = 0.01          # normalized float32 threshold
_SILENCE_CLOSE_MS: int = 700          # ms of silence before closing utterance
_MAX_UTTERANCE_MS: int = 15_000       # cap: 15 s
_SAMPLE_RATE: int = 16_000
_BYTES_PER_SAMPLE: int = 2


def _frame_rms(pcm16: bytes) -> float:
    if len(pcm16) < 2:
        return 0.0
    samples = array.array("h", pcm16[: len(pcm16) - len(pcm16) % 2])
    if not samples:
        return 0.0
    rms_int = math.sqrt(sum(s * s for s in samples) / len(samples))
    return rms_int / 32768.0


class ASRSidecar:
    """Async task: consumes (pcm16_bytes, raw_audio_evt_id) pairs, segments, transcribes, emits.

    asr_model: callable(bytes, sample_rate=16000) -> str  (ASRModel protocol).
    logger: EventLogger or SharedLoggerProxy.
    config_store: ConfigStore or None (None → seam assumed ON; safe for CPU tests).
    session_id: used in emitted event IDs.
    """

    def __init__(
        self,
        *,
        session_id: str,
        asr_model: Any,
        logger: "EventLogger",
        config_store: Any = None,
        queue: "asyncio.Queue[tuple[bytes, str]]",
    ) -> None:
        self._session_id = session_id
        self._asr_model = asr_model
        self._logger = logger
        self._config_store = config_store
        self._queue = queue
        self._seq = 0
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr_sidecar")

    def _seam_on(self) -> bool:
        if self._config_store is None:
            return True
        try:
            return bool(self._config_store.get_seam("asr"))
        except Exception:
            return True

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _emit(self, transcript: str, caused_by: list[str]) -> None:
        if not transcript:
            return
        seq = self._next_seq()
        now_ms = int(time.monotonic() * 1000)
        payload_inline = {"text_preview": transcript[:200]}
        payload_hash = hashlib.sha256(
            transcript[:200].encode()
        ).hexdigest()[:16]
        self._logger.log(Event(
            event_id=f"{self._session_id}-asr-{seq}-{uuid.uuid4().hex[:8]}",
            session_id=self._session_id,
            schema_version=_SCHEMA_VERSION,
            seq_no=seq,
            event_type="asr_transcript_emitted",
            timestamp_mono_ms=now_ms,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source=_SOURCE,
            caused_by=caused_by,
            payload_hash=payload_hash,
            payload_ref=None,
            payload_kind="transcript",
            subject_class="self",
            sensitivity="sensitive",
            retention_policy_id="transcript_audit_30d",
            payload_inline=payload_inline,
        ))

    async def _transcribe(self, pcm16: bytes) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor,
            lambda: self._asr_model(pcm16, _SAMPLE_RATE),
        )

    async def _run_inner(self) -> None:
        buf: list[bytes] = []
        evt_ids: list[str] = []
        silence_frames = 0
        speech_frames = 0
        frame_ms = 0  # estimated; computed from first frame

        while True:
            try:
                pcm16, evt_id = await self._queue.get()
            except asyncio.CancelledError:
                break

            # Sentinel: empty bytes + empty id → stop
            if pcm16 == b"" and evt_id == "":
                break

            # Estimate frame duration once
            if frame_ms == 0 and len(pcm16) >= _BYTES_PER_SAMPLE:
                n_samples = len(pcm16) // _BYTES_PER_SAMPLE
                frame_ms = max(1, int(n_samples * 1000 / _SAMPLE_RATE))

            rms = _frame_rms(pcm16)
            is_speech = rms >= _RMS_THRESHOLD

            if is_speech:
                buf.append(pcm16)
                evt_ids.append(evt_id)
                speech_frames += 1
                silence_frames = 0
            else:
                if buf:
                    buf.append(pcm16)  # include trailing silence in the audio
                    silence_frames += 1
                    accumulated_ms = len(buf) * frame_ms
                    silence_ms = silence_frames * frame_ms

                    should_close = (
                        silence_ms >= _SILENCE_CLOSE_MS
                        or accumulated_ms >= _MAX_UTTERANCE_MS
                    )
                    if should_close:
                        if self._seam_on():
                            combined = b"".join(buf)
                            caused = evt_ids[-1:] if evt_ids else []
                            try:
                                transcript = await self._transcribe(combined)
                            except Exception:
                                transcript = ""
                            self._emit(transcript, caused)
                        buf = []
                        evt_ids = []
                        silence_frames = 0
                        speech_frames = 0

        # Flush any remaining buffered audio on stop
        if buf and self._seam_on():
            combined = b"".join(buf)
            caused = evt_ids[-1:] if evt_ids else []
            try:
                transcript = await self._transcribe(combined)
            except Exception:
                transcript = ""
            self._emit(transcript, caused)

    async def run(self) -> None:
        """Main loop: energy-VAD segmentation → transcribe → emit."""
        try:
            await self._run_inner()
        finally:
            self._executor.shutdown(wait=False)
