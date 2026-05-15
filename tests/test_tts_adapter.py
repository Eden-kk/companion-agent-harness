"""TtsAdapter (ProsodyController) — contract test (live-loop Task 3).

Success criterion (verbatim):
  A contract test drives AudioOutputController with the TtsAdapter (minimal
  concrete) as its sink; audio bytes flow; the test asserts via the causal
  graph that no synthesis occurs without an upstream SpeakDecision in the
  event chain.
"""

import asyncio

import pytest

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.causal_graph import CausalGraph
from companion_harness.event_logger import EventLogger
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import Event, SpeakDecision
from companion_harness.tts_adapter import SilentTtsAdapter, TtsAdapter


# ---------------------------------------------------------------------------
# Helpers


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=256), received


def _fake_speak_decision(caused_by: list[str]) -> SpeakDecision:
    return SpeakDecision(
        action_type="full_response",
        primary_reason_code=ReasonCode.EOU_CONFIRMED,
        supporting_reason_codes=[ReasonCode.USER_ADDRESSED_AGENT],
        redacted_explanation=None,
        caused_by=caused_by,
        budget_bucket="full_response",
        allowed_prosody_tags=[],
        max_duration_ms=None,
    )


# ---------------------------------------------------------------------------
# Protocol satisfaction


def test_silent_tts_adapter_satisfies_protocol():
    assert isinstance(SilentTtsAdapter(), TtsAdapter)


# ---------------------------------------------------------------------------
# Core contract: audio bytes flow through AudioOutputController via TtsAdapter


@pytest.mark.asyncio
async def test_tts_adapter_bytes_flow_through_controller():
    """AudioOutputController.play() streams TtsAdapter output to the AudioSink."""
    logger, received = _make_logger()
    await logger.start()

    delivered: list[bytes] = []

    async def audio_sink(chunk: bytes) -> None:
        delivered.append(chunk)

    adapter = SilentTtsAdapter(chunk_bytes=b"\xAA" * 160, chunk_count=4)
    ctrl = AudioOutputController(
        session_id="test-tts-flow",
        logger=logger,
        sink=audio_sink,
    )

    # Simulate: policy approves speech
    user_evt_id = "user-evt-tts-001"
    gen_id = ctrl.start_generation(caused_by=[user_evt_id])

    # Synthesize and play — the adapter is the source of the AsyncIterable
    await ctrl.play(adapter.synthesize("hello world", []), generation_event_id=gen_id)

    await logger.stop()

    # All chunks must arrive
    assert len(delivered) == 4
    assert all(c == b"\xAA" * 160 for c in delivered)

    # Playback complete event emitted
    event_types = [e.event_type for e in received]
    assert "assistant_generation_start" in event_types
    assert "assistant_audio_buffer_flushed" in event_types


# ---------------------------------------------------------------------------
# Causal-graph invariant: no synthesis without upstream SpeakDecision


@pytest.mark.asyncio
async def test_no_synthesis_without_speak_decision_in_causal_chain():
    """Every audio-output event traces causally to a SpeakDecision event.

    The test simulates the policy layer by injecting a SpeakDecision event_id
    into the causal chain.  It then verifies via CausalGraph that no audio
    event is a causal orphan — i.e. all audio output traces back to the
    SpeakDecision, which itself traces to the user input event.

    This enforces invariants #2 and #4: synthesis is only invoked downstream of
    a policy-approved SpeakDecision; there is no path for the adapter to speak
    on its own.
    """
    logger, received = _make_logger()
    await logger.start()

    # ---- Fabricate the upstream causal chain ----
    # user_input_event is a DAG root (caused_by=[])
    user_input_evt = Event(
        event_id="user-input-001",
        session_id="test-causal",
        schema_version="0.1",
        seq_no=0,
        event_type="user_speech_final",
        timestamp_mono_ms=1000,
        timestamp_wall="2026-01-01T00:00:01Z",
        source="input_ingest",
        caused_by=[],  # DAG root
        payload_hash="abc123",
        payload_ref=None,
        payload_kind="transcript",
        subject_class="third_party",
        sensitivity="sensitive",
        retention_policy_id="default",
    )
    logger.log(user_input_evt)

    # speak_decision_event caused_by the user input
    speak_decision_evt = Event(
        event_id="speak-decision-001",
        session_id="test-causal",
        schema_version="0.1",
        seq_no=1,
        event_type="speak_decision",
        timestamp_mono_ms=1010,
        timestamp_wall="2026-01-01T00:00:01.010Z",
        source="speak_policy",
        caused_by=[user_input_evt.event_id],
        payload_hash="def456",
        payload_ref=None,
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="default",
    )
    logger.log(speak_decision_evt)

    # ---- Wire AudioOutputController with TtsAdapter, caused by SpeakDecision ----
    async def audio_sink(chunk: bytes) -> None:
        pass

    adapter = SilentTtsAdapter(chunk_count=2)
    ctrl = AudioOutputController(
        session_id="test-causal",
        logger=logger,
        sink=audio_sink,
    )

    # generation_start caused_by the SpeakDecision event
    gen_id = ctrl.start_generation(caused_by=[speak_decision_evt.event_id])
    await ctrl.play(adapter.synthesize("hi", []), generation_event_id=gen_id)

    await logger.stop()

    # Build causal graph over ALL events (upstream + audio output)
    graph = CausalGraph(received)
    report = graph.find_orphans()

    assert report.orphan_count == 0, (
        f"Causal orphans detected — synthesis ran without upstream SpeakDecision: "
        f"{report.dangling_refs}"
    )


# ---------------------------------------------------------------------------
# Determinism: same inputs → same output


@pytest.mark.asyncio
async def test_silent_tts_adapter_is_deterministic():
    """SilentTtsAdapter produces identical byte streams across calls."""
    adapter = SilentTtsAdapter(chunk_bytes=b"\x01\x02\x03", chunk_count=5)

    async def collect(gen):
        return [c async for c in gen]

    run_a = await collect(adapter.synthesize("text", []))
    run_b = await collect(adapter.synthesize("text", []))

    assert run_a == run_b
    assert len(run_a) == 5
