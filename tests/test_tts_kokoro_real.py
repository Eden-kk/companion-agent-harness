"""KokoroTtsAdapter (real backend) — contract test (live-loop Task 3).

Exercises the *real* concrete TtsAdapter implementation against
AudioOutputController.  The fixture-driven Protocol contract lives in
``test_tts_adapter.py``; this file is the b200-only counterpart that proves
real audio bytes flow when the canonical backend is loaded.

Skipped automatically when:
  * the Kokoro model files are not present on disk (local-dev environments
    without the canonical b200 download), or
  * ``kokoro_onnx`` is not importable in this venv.

Run on b200 with:
  /raid/yid042/venvs/companion-harness/bin/python3 -m pytest \\
      tests/test_tts_kokoro_real.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.gpu

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.causal_graph import CausalGraph
from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event

# ---------------------------------------------------------------------------
# Skip-if-unavailable guards
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = "/raid/yid042/models/kokoro/kokoro-v0_19.onnx"
_DEFAULT_VOICES = "/raid/yid042/models/kokoro/voices.json"

_MODEL_PATH = os.environ.get("KOKORO_MODEL_PATH", _DEFAULT_MODEL)
_VOICES_PATH = os.environ.get("KOKORO_VOICES_PATH", _DEFAULT_VOICES)

_kokoro_available = pytest.importorskip(
    "kokoro_onnx",
    reason="kokoro-onnx not installed (install with: pip install 'kokoro-onnx' 'numpy<2')",
)

if not Path(_MODEL_PATH).exists() or not Path(_VOICES_PATH).exists():
    pytest.skip(
        f"Kokoro model files not found at {_MODEL_PATH} / {_VOICES_PATH} — "
        "this test is b200-only. Set KOKORO_MODEL_PATH / KOKORO_VOICES_PATH "
        "if your install location differs.",
        allow_module_level=True,
    )

from companion_harness.tts_kokoro import KokoroTtsAdapter  # noqa: E402
from companion_harness.tts_adapter import TtsAdapter  # noqa: E402

# ---------------------------------------------------------------------------
# Shared adapter — load once per session (warmup is ~1.7s)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def kokoro_adapter() -> KokoroTtsAdapter:
    return KokoroTtsAdapter(
        model_path=_MODEL_PATH,
        voices_path=_VOICES_PATH,
        warmup=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=256), received


# ---------------------------------------------------------------------------
# Protocol satisfaction
# ---------------------------------------------------------------------------


def test_kokoro_adapter_satisfies_protocol(kokoro_adapter: KokoroTtsAdapter) -> None:
    assert isinstance(kokoro_adapter, TtsAdapter)


# ---------------------------------------------------------------------------
# Core contract: real audio bytes flow through AudioOutputController
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kokoro_bytes_flow_through_controller(
    kokoro_adapter: KokoroTtsAdapter,
) -> None:
    """The real KokoroTtsAdapter produces PCM16 bytes that reach the AudioSink."""
    logger, received = _make_logger()
    await logger.start()

    delivered: list[bytes] = []

    async def audio_sink(chunk: bytes) -> None:
        delivered.append(chunk)

    ctrl = AudioOutputController(
        session_id="test-tts-kokoro-flow",
        logger=logger,
        sink=audio_sink,
    )

    # Simulate: policy approves speech
    speak_decision_evt_id = "fake-speak-decision-001"
    gen_id = ctrl.start_generation(caused_by=[speak_decision_evt_id])

    await ctrl.play(
        kokoro_adapter.synthesize("Hello world.", []),
        generation_event_id=gen_id,
    )

    await logger.stop()

    # At least one chunk arrived, non-empty, even length (PCM16 = 2 bytes/sample)
    assert len(delivered) >= 1
    total = sum(len(c) for c in delivered)
    assert total > 0
    assert total % 2 == 0, "PCM16 byte count must be a multiple of 2"

    # Buffer-flushed event was emitted (full stream consumed)
    event_types = [e.event_type for e in received]
    assert "assistant_generation_start" in event_types
    assert "assistant_audio_buffer_flushed" in event_types


# ---------------------------------------------------------------------------
# Causal-graph invariant: no synthesis without upstream SpeakDecision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kokoro_no_synthesis_without_speak_decision(
    kokoro_adapter: KokoroTtsAdapter,
) -> None:
    """Every audio-output event from the real adapter traces to a SpeakDecision.

    Mirrors ``test_no_synthesis_without_speak_decision_in_causal_chain`` from
    ``test_tts_adapter.py`` but with the real Kokoro backend driving real
    audio bytes.  This enforces invariants #2 and #4: synthesis is only ever
    invoked downstream of a policy-approved SpeakDecision; there is no path
    for the adapter to speak on its own.
    """
    logger, received = _make_logger()
    await logger.start()

    # ---- Fabricate the upstream causal chain ----
    user_input_evt = Event(
        event_id="user-input-real-001",
        session_id="test-causal-real",
        schema_version="0.1",
        seq_no=0,
        event_type="user_speech_final",
        timestamp_mono_ms=1000,
        timestamp_wall="2026-01-01T00:00:01Z",
        source="input_ingest",
        caused_by=[],
        payload_hash="abc123",
        payload_ref=None,
        payload_kind="transcript",
        subject_class="third_party",
        sensitivity="sensitive",
        retention_policy_id="default",
    )
    logger.log(user_input_evt)

    speak_decision_evt = Event(
        event_id="speak-decision-real-001",
        session_id="test-causal-real",
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

    async def audio_sink(chunk: bytes) -> None:
        pass

    ctrl = AudioOutputController(
        session_id="test-causal-real",
        logger=logger,
        sink=audio_sink,
    )
    gen_id = ctrl.start_generation(caused_by=[speak_decision_evt.event_id])
    await ctrl.play(
        kokoro_adapter.synthesize("hi", []),
        generation_event_id=gen_id,
    )

    await logger.stop()

    # Every audio-output event must trace back through the SpeakDecision
    graph = CausalGraph(received)
    report = graph.find_orphans()

    assert report.orphan_count == 0, (
        f"Causal orphans detected — real synthesis ran without upstream "
        f"SpeakDecision: {report.dangling_refs}"
    )
