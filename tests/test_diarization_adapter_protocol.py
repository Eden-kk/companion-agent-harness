"""v0.2b T7 — DiarizationAdapter Protocol-shape + null-adapter + tie-breaker tests.

Tests 1, 2, 4, 5, 7, 8 from the T7 plan.  Tests 3 and 6 (b200-gated real-load
and mute-window trailing-edge) are in test_pyannote_diarization_adapter.py.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from companion_harness.addressing_classifier import derive_user_addressed_agent
from companion_harness.addressing_classifier import AddressingSignal
from companion_harness.diarization_adapter import (
    DiarizationAdapter,
    DiarizationFrame,
    _NullDiarizationAdapter,
)
from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event


# ---------------------------------------------------------------------------
# T7 test 1: Protocol runtime-checkable isinstance
# ---------------------------------------------------------------------------

def test_diarization_adapter_satisfies_protocol():
    """isinstance(_NullDiarizationAdapter(), DiarizationAdapter) must be True."""
    adapter = _NullDiarizationAdapter()
    assert isinstance(adapter, DiarizationAdapter)


# ---------------------------------------------------------------------------
# T7 test 2: null adapter returns DiarizationFrame not raw tuple (Anchor 6)
# ---------------------------------------------------------------------------

def test_null_diarization_adapter_returns_protocol_conformant_frame():
    adapter = _NullDiarizationAdapter()
    frame = adapter.process_chunk(b"\x00" * 320, ts_mono_ms=1000, muted=False)
    assert isinstance(frame, DiarizationFrame)
    assert frame.speaker_id is None
    assert frame.confidence == 0.0
    assert frame.is_new_speaker is False


def test_null_diarization_adapter_muted_returns_null_frame():
    adapter = _NullDiarizationAdapter()
    frame = adapter.process_chunk(b"\x00" * 320, ts_mono_ms=1000, muted=True)
    assert isinstance(frame, DiarizationFrame)
    assert frame.speaker_id is None


# ---------------------------------------------------------------------------
# T7 test 4: diarization_frame_produced events must have caused_by set
# (The NullAdapter does not emit events; this tests the contract shape.
#  PyannoteDiarizationAdapter tests are in test_pyannote_diarization_adapter.py.)
# ---------------------------------------------------------------------------

def test_diarization_frame_dataclass_is_frozen():
    frame = DiarizationFrame(speaker_id="spk-0", confidence=0.9, is_new_speaker=True)
    with pytest.raises((AttributeError, TypeError)):
        frame.speaker_id = "spk-1"  # type: ignore[misc]


def test_diarization_event_schema_has_required_payload_fields():
    """Contract: diarization_frame_produced schema lists required payload fields.

    The null adapter does not emit events, so this test validates the schema
    contract by inspecting the v0_1g_event_schema required_fields sentinel.
    Real-adapter caused_by propagation is tested in test_pyannote_diarization_adapter.py.
    """
    from companion_harness.v0_1g_event_schema import EVENT_TYPE_SCHEMAS
    assert "diarization_frame_produced" in EVENT_TYPE_SCHEMAS
    schema = EVENT_TYPE_SCHEMAS["diarization_frame_produced"]
    assert "speaker_id" in schema.required_fields
    assert "confidence" in schema.required_fields
    assert "is_new_speaker" in schema.required_fields
    assert "model_revision" in schema.required_fields


# ---------------------------------------------------------------------------
# caused_by propagation: fake adapter emits event with raw_audio_chunk_event_id
# ---------------------------------------------------------------------------


class _FakeEmittingAdapter:
    """Minimal adapter that emits diarization_frame_produced with caused_by wired."""

    SOURCE = "fake_diarization_adapter"
    SCHEMA_VERSION = "0.1"

    def __init__(self, session_id: str, logger: EventLogger) -> None:
        self._session_id = session_id
        self._logger = logger
        self._seq = 0

    def process_chunk(
        self,
        audio_bytes: bytes,
        ts_mono_ms: int,
        muted: bool,
        raw_audio_chunk_event_id: str = "",
    ) -> DiarizationFrame:
        if muted or not audio_bytes:
            return DiarizationFrame(speaker_id=None, confidence=0.0, is_new_speaker=False)
        self._seq += 1
        event_id = f"{self._session_id}-diar-{self._seq}-{ts_mono_ms}"
        payload_hash = hashlib.sha256(
            f"diarization_frame_produced:{event_id}:{ts_mono_ms}".encode()
        ).hexdigest()[:16]
        caused_by = [raw_audio_chunk_event_id] if raw_audio_chunk_event_id else []
        evt = Event(
            event_id=event_id,
            session_id=self._session_id,
            schema_version=self.SCHEMA_VERSION,
            seq_no=self._seq,
            event_type="diarization_frame_produced",
            timestamp_mono_ms=ts_mono_ms,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source=self.SOURCE,
            caused_by=caused_by,
            payload_hash=payload_hash,
            payload_ref=None,
            payload_kind="signal",
            subject_class="self",
            sensitivity="safe",
            retention_policy_id="signal_default_30d",
            payload_inline={
                "speaker_id": "spk-0",
                "confidence": 0.9,
                "is_new_speaker": False,
                "model_revision": "fake/model",
            },
        )
        self._logger.log(evt)
        return DiarizationFrame(speaker_id="spk-0", confidence=0.9, is_new_speaker=False)


@pytest.mark.asyncio
async def test_diarization_caused_by_propagates_raw_audio_event_id() -> None:
    """diarization_frame_produced.caused_by must contain the raw audio chunk event_id."""
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    logger = EventLogger(sink, maxsize=256)
    await logger.start()

    adapter = _FakeEmittingAdapter(session_id="test-session", logger=logger)
    adapter.process_chunk(
        b"\x00" * 320, ts_mono_ms=1000, muted=False,
        raw_audio_chunk_event_id="raw-audio-evt-42",
    )

    await logger.stop()

    diar_events = [e for e in received if e.event_type == "diarization_frame_produced"]
    assert len(diar_events) == 1
    assert diar_events[0].caused_by == ["raw-audio-evt-42"]


# ---------------------------------------------------------------------------
# T7 test 5: muted=True suppresses registry + event emission
# ---------------------------------------------------------------------------

def test_diarization_mute_window_suppresses_frame_emission():
    """Muted chunks return null frame (Anchor 3); no registry update."""
    adapter = _NullDiarizationAdapter()
    frame = adapter.process_chunk(b"\xff" * 320, ts_mono_ms=2000, muted=True)
    assert frame == DiarizationFrame(speaker_id=None, confidence=0.0, is_new_speaker=False)


# ---------------------------------------------------------------------------
# T7 test 7: speaker_continuity_anchor schema entry exists
# ---------------------------------------------------------------------------

def test_speaker_continuity_anchor_schema_entry_exists():
    from companion_harness.v0_1g_event_schema import EVENT_TYPE_SCHEMAS
    assert "speaker_continuity_anchor" in EVENT_TYPE_SCHEMAS
    schema = EVENT_TYPE_SCHEMAS["speaker_continuity_anchor"]
    assert "speaker_id" in schema.required_fields
    assert "wake_word_event_id" in schema.required_fields


# ---------------------------------------------------------------------------
# T7 test 8: speaker-continuity tie-breaker (Anchor 7)
# ---------------------------------------------------------------------------

def test_speaker_continuity_tie_breaker_flips_implicit_to_true():
    """derive_user_addressed_agent returns True when same speaker as wake-word anchor.

    Without the tie-breaker, implicit tier with no tokens returns False.
    With current_speaker_id == last_anchored_speaker_id, it returns True.
    """
    signal = AddressingSignal(confidence="implicit", evidence="implicit_fallback")
    # Without tie-breaker: short transcript -> False
    assert derive_user_addressed_agent(signal, social_mode="solo", transcript="ok") is False
    # With matching speaker-continuity: -> True
    assert derive_user_addressed_agent(
        signal,
        social_mode="solo",
        transcript="ok",
        current_speaker_id="spk-0",
        last_anchored_speaker_id="spk-0",
    ) is True


def test_speaker_continuity_tie_breaker_does_not_fire_when_speaker_mismatch():
    signal = AddressingSignal(confidence="implicit", evidence="implicit_fallback")
    result = derive_user_addressed_agent(
        signal,
        social_mode="solo",
        transcript="ok",
        current_speaker_id="spk-0",
        last_anchored_speaker_id="spk-1",
    )
    assert result is False


def test_speaker_continuity_tie_breaker_does_not_fire_when_ids_are_none():
    signal = AddressingSignal(confidence="implicit", evidence="implicit_fallback")
    # current_speaker_id=None -> backward-compat, no tie-breaker
    result = derive_user_addressed_agent(
        signal,
        social_mode="solo",
        transcript="ok",
        current_speaker_id=None,
        last_anchored_speaker_id="spk-0",
    )
    assert result is False


def test_speaker_continuity_tie_breaker_does_not_override_explicit():
    """Explicit tier always returns True regardless of speaker IDs."""
    signal = AddressingSignal(confidence="explicit", evidence="wake_word_match:companion")
    assert derive_user_addressed_agent(
        signal,
        social_mode="solo",
        transcript="hey companion",
        current_speaker_id="spk-0",
        last_anchored_speaker_id="spk-1",
    ) is True


def test_speaker_continuity_tie_breaker_fires_on_background_tier():
    """Background tier returns False by default; tie-breaker can override."""
    signal = AddressingSignal(confidence="background", evidence="multi_speaker:2")
    # Without tie-breaker: False
    assert derive_user_addressed_agent(signal, social_mode="solo") is False
    # With matching speaker: True
    assert derive_user_addressed_agent(
        signal,
        social_mode="solo",
        current_speaker_id="spk-0",
        last_anchored_speaker_id="spk-0",
    ) is True


def test_derive_user_addressed_agent_backward_compat_no_speaker_args():
    """Old callers that don't pass speaker args continue to work unchanged."""
    signal = AddressingSignal(confidence="implicit", evidence="implicit_fallback")
    # Substantive transcript -> True (unchanged)
    assert derive_user_addressed_agent(
        signal,
        social_mode="solo",
        transcript="what is the weather today",
    ) is True
    # Short transcript -> False (unchanged)
    assert derive_user_addressed_agent(
        signal,
        social_mode="solo",
        transcript="hi",
    ) is False
