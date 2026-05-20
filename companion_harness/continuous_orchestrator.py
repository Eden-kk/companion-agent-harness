"""ContinuousOrchestrator — turn-free continuous companion loop.

Consumes audio_in continuously via the foreground model's stream_chunks()
adapter; per chunk it emits a continuous_chunk_processed audit event, calls the
per-chunk gate decide_chunk (PR2), emits a policy_decision, and starts speech via
the injected audio_output adapter on a speak decision (PR3a — start-only; PR3b
adds the model-native barge-in stop path; PR3c adds backchannel veto).

Adapter-first (CLAUDE.md): this module imports no model SDK and never touches
_duplex / streaming_prefill / streaming_generate. All per-chunk mechanics live
inside the ForegroundModel adapter's stream_chunks() generator.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol

from companion_harness.continuous_speak_policy import _BACKCHANNEL_THRESHOLD, decide_chunk
from companion_harness.schemas import Event, PerChunkPolicyInputs, SpeakDecision

if TYPE_CHECKING:
    from companion_harness.event_logger import EventLogger

__all__ = ["ContinuousOrchestrator"]

_SOURCE = "continuous_orchestrator"
_SCHEMA_VERSION = "0.1"

# Action types that start audio synthesis. NOTE: "tool_call" is intentionally
# excluded — it dispatches via tool routing, not TTS, and is handled in a later
# PR (cf. realtime_orchestrator.py's tool_call path), not by _act's start path.
_SPEAK_ACTIONS = frozenset({
    "full_response", "backchannel", "short_reaction", "clarification",
    "alert", "tool_status", "aesthetic_reaction",
})


class _BackchannelSourceProtocol(Protocol):
    def latest_score(self) -> float: ...


class _NullBackchannelSource:
    def latest_score(self) -> float:
        return 0.0


_NULL_BC_SOURCE = _NullBackchannelSource()


class _BackgroundThoughtSourceProtocol(Protocol):
    def pending_thought(self) -> str | None: ...


class _NullThoughtSource:
    def pending_thought(self) -> str | None:
        return None


_NULL_THOUGHT_SOURCE = _NullThoughtSource()


class _ForegroundModelProtocol(Protocol):
    async def stream_chunks(
        self,
        audio_in: "asyncio.Queue[tuple[bytes, str]]",
    ) -> AsyncGenerator[tuple[bool, str, int | None, str], None]:
        ...

    def inject_scratchpad(self, text: str) -> None: ...


class _AudioOutputProtocol(Protocol):
    @property
    def is_playing(self) -> bool: ...
    def start_generation(self, caused_by: list[str]) -> str: ...
    def request_stop(self, caused_by: list[str]) -> str: ...


class ContinuousOrchestrator:
    """Flat continuous loop: audio_in → per-chunk model call → per-chunk event.

    Constructor args mirror StreamingRealtimeOrchestrator for the shared
    session_id / logger / audio_in contract so the eval feeder path is
    identical.
    """

    def __init__(
        self,
        *,
        session_id: str,
        logger: "EventLogger",
        audio_in: "asyncio.Queue[tuple[bytes, str]]",
        foreground_model: _ForegroundModelProtocol,
        audio_output: _AudioOutputProtocol,
        backchannel_source: _BackchannelSourceProtocol = _NULL_BC_SOURCE,
        thought_source: _BackgroundThoughtSourceProtocol = _NULL_THOUGHT_SOURCE,
        privacy_mode: str = "normal",
        social_mode: str = "user_addressing_agent",
        budget_full_response_remaining: int = 1,
    ) -> None:
        self._session_id = session_id
        self._logger = logger
        self._audio_in = audio_in
        self._foreground = foreground_model
        self._audio_output = audio_output
        self._backchannel_source = backchannel_source
        self._thought_source = thought_source
        self._privacy_mode = privacy_mode
        self._social_mode = social_mode
        self._budget_full_response_remaining = budget_full_response_remaining
        self._seq = 0

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Consume stream_chunks until the feeder pushes the sentinel (b"", "")."""
        prior_kv_len: int | None = None
        chunk_idx = 0

        async for is_listen, text, audio_kv_len, caused_by_evt_id in self._foreground.stream_chunks(self._audio_in):
            self._emit_chunk_processed(
                is_listen=is_listen,
                audio_kv_len=audio_kv_len,
                n_chars=len(text),
                caused_by_evt_id=caused_by_evt_id,
            )

            if (
                audio_kv_len is not None
                and prior_kv_len is not None
                and audio_kv_len < prior_kv_len
            ):
                self._emit_audio_kv_reset(
                    prior_len=prior_kv_len,
                    new_len=audio_kv_len,
                    caused_by_evt_id=caused_by_evt_id,
                )

            if audio_kv_len is not None:
                prior_kv_len = audio_kv_len

            inputs = PerChunkPolicyInputs(
                chunk_index=chunk_idx,
                model_is_listen=is_listen,
                backchannel_score=0.0,        # bc_score is orchestrator-level only (read separately below); never fed to decide_chunk — invariant #4
                user_addressed_agent=True,    # continuous companion is addressed by construction; refined later
                privacy_mode=self._privacy_mode,
                social_mode=self._social_mode,
                # budget decrement is wired in PR3b/PR3c; constant here is intentional
                # for PR3a (full_response re-entry is guarded by audio is_playing).
                budget_full_response_remaining=self._budget_full_response_remaining,
            )
            bc_score = self._backchannel_source.latest_score()
            decision = decide_chunk(inputs, caused_by_evt_id=caused_by_evt_id)
            self._emit_policy_decision(decision, caused_by_evt_id)
            self._act(decision, is_listen, bc_score, caused_by_evt_id)
            chunk_idx += 1

            thought = self._thought_source.pending_thought()
            if thought is not None:
                self._foreground.inject_scratchpad(thought)
                self._emit_background_think_injected(n_chars=len(thought), caused_by_evt_id=caused_by_evt_id)

    # ------------------------------------------------------------------
    # Act on the per-chunk decision (PR3a — start speech only; PR3b adds stop;
    # PR3c adds backchannel veto)
    # ------------------------------------------------------------------

    def _act(self, decision: SpeakDecision, is_listen: bool, bc_score: float, caused_by_evt_id: str) -> None:
        if is_listen and self._audio_output.is_playing:
            if bc_score >= _BACKCHANNEL_THRESHOLD:
                self._emit_barge_in_suppressed(bc_score, caused_by_evt_id)
                return
            self._audio_output.request_stop(caused_by=[caused_by_evt_id])
            self._emit_barge_in(caused_by_evt_id)
            # stop wins: model yielded (is_listen); a speak decision (if any) is
            # overridden by silence this chunk. decide_chunk also returns silence
            # on is_listen, so the two layers agree.
            return
        if decision.action_type in _SPEAK_ACTIONS and not self._audio_output.is_playing:
            self._audio_output.start_generation(caused_by=[caused_by_evt_id])

    # ------------------------------------------------------------------
    # Event emission helpers
    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _emit_chunk_processed(
        self,
        *,
        is_listen: bool,
        audio_kv_len: "int | None",
        n_chars: int,
        caused_by_evt_id: str,
    ) -> None:
        now_ms = int(time.monotonic() * 1000)
        seq = self._next_seq()
        event_id = f"{self._session_id}-cco-{seq}-{now_ms}"
        payload_inline = {
            "is_listen": is_listen,
            "audio_kv_len": audio_kv_len,
            "n_chars": n_chars,
        }
        payload_hash = hashlib.sha256(
            json.dumps(payload_inline, sort_keys=True).encode()
        ).hexdigest()[:16]
        evt = Event(
            event_id=event_id,
            session_id=self._session_id,
            schema_version=_SCHEMA_VERSION,
            seq_no=seq,
            event_type="continuous_chunk_processed",
            timestamp_mono_ms=now_ms,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source=_SOURCE,
            caused_by=[caused_by_evt_id],
            payload_hash=payload_hash,
            payload_ref=None,
            payload_kind="signal",
            subject_class="self",
            sensitivity="safe",
            retention_policy_id="signal_default_30d",
            payload_inline=payload_inline,
        )
        self._logger.log(evt)

    def _emit_audio_kv_reset(
        self,
        *,
        prior_len: int,
        new_len: int,
        caused_by_evt_id: str,
    ) -> None:
        now_ms = int(time.monotonic() * 1000)
        seq = self._next_seq()
        event_id = f"{self._session_id}-kvreset-{seq}-{now_ms}"
        kv_payload = {"prior_len": prior_len, "new_len": new_len}
        payload_hash = hashlib.sha256(
            json.dumps(kv_payload, sort_keys=True).encode()
        ).hexdigest()[:16]
        evt = Event(
            event_id=event_id,
            session_id=self._session_id,
            schema_version=_SCHEMA_VERSION,
            seq_no=seq,
            event_type="audio_kv_reset",
            timestamp_mono_ms=now_ms,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source=_SOURCE,
            caused_by=[caused_by_evt_id],
            payload_hash=payload_hash,
            payload_ref=None,
            payload_kind="signal",
            subject_class="self",
            sensitivity="safe",
            retention_policy_id="signal_default_30d",
            payload_inline=kv_payload,
        )
        self._logger.log(evt)

    def _emit_barge_in(self, caused_by_evt_id: str) -> None:
        now_ms = int(time.monotonic() * 1000)
        seq = self._next_seq()
        event_id = f"{self._session_id}-bi-{seq}-{now_ms}"
        payload_inline: dict = {}
        payload_hash = hashlib.sha256(
            json.dumps(payload_inline, sort_keys=True).encode()
        ).hexdigest()[:16]
        evt = Event(
            event_id=event_id,
            session_id=self._session_id,
            schema_version=_SCHEMA_VERSION,
            seq_no=seq,
            event_type="model_native_barge_in",
            timestamp_mono_ms=now_ms,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source=_SOURCE,
            caused_by=[caused_by_evt_id],
            payload_hash=payload_hash,
            payload_ref=None,
            payload_kind="signal",
            subject_class="self",
            sensitivity="safe",
            retention_policy_id="signal_default_30d",
            payload_inline=payload_inline,
        )
        self._logger.log(evt)

    def _emit_barge_in_suppressed(self, bc_score: float, caused_by_evt_id: str) -> None:
        now_ms = int(time.monotonic() * 1000)
        seq = self._next_seq()
        event_id = f"{self._session_id}-bis-{seq}-{now_ms}"
        payload_inline = {"backchannel_score": bc_score}
        payload_hash = hashlib.sha256(
            json.dumps(payload_inline, sort_keys=True).encode()
        ).hexdigest()[:16]
        evt = Event(
            event_id=event_id,
            session_id=self._session_id,
            schema_version=_SCHEMA_VERSION,
            seq_no=seq,
            event_type="barge_in_suppressed_backchannel",
            timestamp_mono_ms=now_ms,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source=_SOURCE,
            caused_by=[caused_by_evt_id],
            payload_hash=payload_hash,
            payload_ref=None,
            payload_kind="signal",
            subject_class="self",
            sensitivity="safe",
            retention_policy_id="signal_default_30d",
            payload_inline=payload_inline,
        )
        self._logger.log(evt)

    def _emit_background_think_injected(self, *, n_chars: int, caused_by_evt_id: str) -> None:
        now_ms = int(time.monotonic() * 1000)
        seq = self._next_seq()
        event_id = f"{self._session_id}-bti-{seq}-{now_ms}"
        payload_inline = {"role": "background-think", "n_chars": n_chars}
        payload_hash = hashlib.sha256(
            json.dumps(payload_inline, sort_keys=True).encode()
        ).hexdigest()[:16]
        evt = Event(
            event_id=event_id,
            session_id=self._session_id,
            schema_version=_SCHEMA_VERSION,
            seq_no=seq,
            event_type="background_think_injected",
            timestamp_mono_ms=now_ms,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source=_SOURCE,
            caused_by=[caused_by_evt_id],
            payload_hash=payload_hash,
            payload_ref=None,
            payload_kind="signal",
            subject_class="self",
            sensitivity="safe",
            retention_policy_id="signal_default_30d",
            payload_inline=payload_inline,
        )
        self._logger.log(evt)

    def _emit_policy_decision(self, decision: SpeakDecision, caused_by_evt_id: str) -> None:
        now_ms = int(time.monotonic() * 1000)
        seq = self._next_seq()
        event_id = f"{self._session_id}-pd-{seq}-{now_ms}"
        payload_inline = {
            "action_type": decision.action_type,
            "primary_reason_code": decision.primary_reason_code.value,
        }
        payload_hash = hashlib.sha256(
            json.dumps(payload_inline, sort_keys=True).encode()
        ).hexdigest()[:16]
        evt = Event(
            event_id=event_id,
            session_id=self._session_id,
            schema_version=_SCHEMA_VERSION,
            seq_no=seq,
            event_type="policy_decision",
            timestamp_mono_ms=now_ms,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source=_SOURCE,
            caused_by=[caused_by_evt_id],
            payload_hash=payload_hash,
            payload_ref=None,
            payload_kind="signal",
            subject_class="self",
            sensitivity="safe",
            # matches RealtimeOrchestrator's policy_decision retention (shared event type)
            retention_policy_id="decision_trace_30d",
            payload_inline=payload_inline,
        )
        self._logger.log(evt)
