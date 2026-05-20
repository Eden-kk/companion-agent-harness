"""PR5a-gaps tests: listen_prob_scale knob + gate precedence + proactivity-soundness.

These cover the parts of the PR5a tuning gate that #363 (chunk_ms resize) omitted:
  (a) the configurable listen_prob_scale override (_effective_listen_prob_scale);
  (b) precedence — the policy gate beats the model's speak preference (invariant #9):
      model_is_listen=False (model wants to speak) is still silenced by any hard block;
  (c) proactivity-soundness — in a blocking social mode the ContinuousOrchestrator
      emits zero proactive speech even when the model pushes to speak (invariant #8).

The chunk_samples derivation is already covered by tests/test_continuous_chunk_ms.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import pytest

from companion_harness.continuous_orchestrator import ContinuousOrchestrator
from companion_harness.continuous_speak_policy import decide_chunk
from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event, PerChunkPolicyInputs

_CHUNK_SAMPLES = 16_000
_BYTES_PER_SAMPLE = 2


# ---------------------------------------------------------------------------
# (a) listen_prob_scale selection (torch-guarded — importing the adapter pulls torch)
# ---------------------------------------------------------------------------

def test_effective_listen_prob_scale_override_and_fallback() -> None:
    """Set value → returns it; None → falls back to the duplex's own value."""
    pytest.importorskip("torch")
    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel  # noqa: PLC0415

    m = object.__new__(MiniCPMStreamingModel)  # bypass __init__ (loads the model)

    class _FakeDuplex:
        listen_prob_scale = 0.3

    dup = _FakeDuplex()

    m._listen_prob_scale = 1.0
    assert m._effective_listen_prob_scale(dup) == 1.0  # explicit override wins

    m._listen_prob_scale = None
    assert m._effective_listen_prob_scale(dup) == 0.3  # falls back to duplex (current behavior)


# ---------------------------------------------------------------------------
# (b) precedence: gate beats model preference (model wants to speak → still silenced)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "overrides",
    [
        {"social_mode": "group_conversation"},
        {"privacy_mode": "sensitive_conversation"},
        {"user_addressed_agent": False},
        {"budget_full_response_remaining": 0},
    ],
)
def test_gate_silences_despite_model_speak_preference(overrides: dict) -> None:
    """model_is_listen=False (model wants to speak) is overridden to silence by any hard block."""
    base = dict(
        chunk_index=0,
        model_is_listen=False,  # model wants to speak
        backchannel_score=0.0,
        user_addressed_agent=True,
        privacy_mode="normal",
        social_mode="user_addressing_agent",
        budget_full_response_remaining=1,
    )
    base.update(overrides)
    decision = decide_chunk(PerChunkPolicyInputs(**base), caused_by_evt_id="evt-1")
    assert decision.action_type == "silence", (
        f"gate should silence under {overrides}, got {decision.action_type}"
    )


def test_gate_speaks_when_unblocked() -> None:
    """Control: model wants to speak + addressed + budget + no block → full_response (not vacuous)."""
    decision = decide_chunk(
        PerChunkPolicyInputs(
            chunk_index=0,
            model_is_listen=False,
            backchannel_score=0.0,
            user_addressed_agent=True,
            privacy_mode="normal",
            social_mode="user_addressing_agent",
            budget_full_response_remaining=1,
        ),
        caused_by_evt_id="evt-1",
    )
    assert decision.action_type == "full_response"


# ---------------------------------------------------------------------------
# (c) proactivity-soundness at the orchestrator level
# ---------------------------------------------------------------------------

class _FakeForegroundModel:
    """stream_chunks mirror: yields one (is_listen=False) record per full chunk."""

    async def stream_chunks(
        self,
        audio_in: "asyncio.Queue[tuple[bytes, str]]",
    ) -> AsyncGenerator[tuple[bool, str, None, str], None]:
        buf_samples = 0
        latest_evt_id = ""
        while True:
            pcm_bytes, evt_id = await audio_in.get()
            if pcm_bytes == b"" and evt_id == "":
                return
            latest_evt_id = evt_id
            buf_samples += len(pcm_bytes) // _BYTES_PER_SAMPLE
            while buf_samples >= _CHUNK_SAMPLES:
                buf_samples -= _CHUNK_SAMPLES
                yield (False, "", None, latest_evt_id)  # model wants to speak every chunk


class _RecordingAudioOutput:
    def __init__(self) -> None:
        self.start_calls = 0
        self.stop_calls = 0

    @property
    def is_playing(self) -> bool:
        return False

    def start_generation(self, *, caused_by: list[str]) -> str:
        self.start_calls += 1
        return "gen"

    def request_stop(self, *, caused_by: list[str]) -> str:
        self.stop_calls += 1
        return "stop"


@pytest.mark.asyncio
async def test_blocking_social_mode_emits_zero_proactive_speech() -> None:
    """In background_presence the orchestrator never starts speech, even though the model pushes to speak."""
    received: list[Event] = []

    async def _sink(evt: Event) -> None:
        received.append(evt)

    logger = EventLogger(_sink, maxsize=4096)
    await logger.start()

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=256)
    audio_output = _RecordingAudioOutput()
    orch = ContinuousOrchestrator(
        session_id="pr5a-test",
        logger=logger,
        audio_in=audio_in,
        foreground_model=_FakeForegroundModel(),
        audio_output=audio_output,
        social_mode="background_presence",  # a _BLOCKING_SOCIAL_MODES member
    )

    n_chunks = 4
    chunk_bytes = b"\x00" * (_CHUNK_SAMPLES * _BYTES_PER_SAMPLE)
    for i in range(n_chunks):
        audio_in.put_nowait((chunk_bytes, f"audio-{i}"))
    audio_in.put_nowait((b"", ""))

    await orch.run()
    await logger.stop()

    # The model wanted to speak on every chunk, but the social-mode hard block keeps silence.
    assert audio_output.start_calls == 0, "proactive speech started in a blocking social mode"
    full_response_decisions = [
        e for e in received
        if e.event_type == "policy_decision"
        and (e.payload_inline or {}).get("action_type") == "full_response"
    ]
    assert full_response_decisions == [], "gate emitted a full_response in a blocking social mode"
    # Sanity: the orchestrator did run the chunks (non-vacuous).
    chunk_events = [e for e in received if e.event_type == "continuous_chunk_processed"]
    assert len(chunk_events) == n_chunks
