"""Stage 0 — every assistant_audio_start has a non-empty caused_by chain.

See docs/architecture-v0.1.md §Part 6 Stage 0 and §Part 8 v0.1a acceptance gate
(assistant_audio_start_with_cause = 100%).

Event-name note: the spec prose says "assistant_audio_start"; the AudioOutputController
emits "assistant_generation_start" (Part 5 event type list; barge_in_001 fixture).
These are the same event — "assistant_generation_start" is the audio-start event.

Methodology: drive AudioOutputController through N utterance lifecycles (no model
required — same adapter-only pattern as test_barge_in). Collect all events, filter
to assistant_generation_start, assert (a) at least one exists and (b) every one has
non-empty caused_by[]. Both checks are required; the "at least one" guard prevents
vacuous passes on empty traces.
"""

import asyncio

import pytest

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event

_AUDIO_START = "assistant_generation_start"


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=1024), received


@pytest.mark.asyncio
async def test_decision_provenance():
    """Every assistant_generation_start event must carry a non-empty caused_by[].

    Gate: assistant_audio_start_with_cause = 100%
    (docs/architecture-v0.1.md §Part 8 v0.1a acceptance gates).

    Drives 3 distinct utterance lifecycles so the "at least one event" guard
    has teeth — a trace with zero assistant_generation_start events would fail
    the non-vacuity assertion before the provenance check.
    """
    logger, received = _make_logger()
    await logger.start()

    async def audio_sink(chunk: bytes) -> None:
        pass  # no-op; we care about event provenance, not playback timing

    controller = AudioOutputController(
        session_id="provenance-test-session",
        logger=logger,
        sink=audio_sink,
    )

    # Three utterances, each causally grounded in a distinct upstream event.
    utterances = [
        {"policy_event": "policy-decision-utterance-1", "chunks": [b"hello"]},
        {"policy_event": "policy-decision-utterance-2", "chunks": [b"world", b"!"]},
        {"policy_event": "policy-decision-utterance-3", "chunks": [b"goodbye"]},
    ]

    for utt in utterances:
        # caused_by here uses synthetic sentinel ids — this test checks structural
        # non-emptiness of provenance only; full DAG closure is verified in
        # test_causal_graph_completeness.
        gen_id = controller.start_generation(caused_by=[utt["policy_event"]])
        for chunk in utt["chunks"]:
            controller.queue_buffer(chunk, caused_by=[gen_id])
        await controller.play(utt["chunks"], generation_event_id=gen_id)

    await logger.stop()

    audio_start_events = [e for e in received if e.event_type == _AUDIO_START]

    # Non-vacuity: the trace must contain at least one audio-start event.
    assert len(audio_start_events) >= 1, (
        f"trace contains no {_AUDIO_START!r} events — "
        "test would vacuously pass on an empty set; check controller lifecycle"
    )

    # Gate: every audio-start event must have a non-empty caused_by[].
    missing_cause = [e.event_id for e in audio_start_events if not e.caused_by]
    assert not missing_cause, (
        f"assistant_audio_start_with_cause gate violated: "
        f"events missing caused_by: {missing_cause}"
    )
