"""PR3c: BackchannelClassifier confirmatory veto.

CPU-only (no GPU / no weights). Stubs foreground model, audio output, and
backchannel source. Verifies:
  - bc_score >= threshold → NO request_stop, barge_in_suppressed_backchannel
    emitted (caused_by closed), audio still playing (PR3c veto)
  - bc_score == 0.0 → request_stop + model_native_barge_in (PR3b preserved)
  - default null source (no backchannel_source arg) → PR3b stop behavior
  - decide_chunk unchanged: policy_decision is still "silence" on the yield chunk
    (blocker-2: bc_score is NOT fed into PerChunkPolicyInputs.backchannel_score)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from companion_harness.continuous_orchestrator import ContinuousOrchestrator
from companion_harness.schemas import Event


def _rac(i: int, session_id: str) -> Event:
    return Event(
        event_id=f"rac-{i}",
        session_id=session_id,
        schema_version="0.1",
        seq_no=i,
        event_type="raw_audio_chunk",
        timestamp_mono_ms=i,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
        source="test_ingest",
        caused_by=["session_root"],
        payload_hash="x",
        payload_ref=None,
        payload_kind="raw_audio",
        subject_class="self",
        sensitivity="sensitive",
        retention_policy_id="raw_media_default_300s",
    )


class _FakeLogger:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def log(self, evt: Event) -> None:
        self.events.append(evt)


class _FakeForegroundModel:
    def __init__(self, script, logger, session_id) -> None:
        self._script = script
        self._logger = logger
        self._sid = session_id

    async def stream_chunks(self, audio_in):
        for i, (is_listen, text, kv) in enumerate(self._script):
            evt = _rac(i, self._sid)
            self._logger.log(evt)
            yield (is_listen, text, kv, evt.event_id)


class _FakeAudioOutput:
    def __init__(self) -> None:
        self._playing = False
        self.start_calls: list[list[str]] = []
        self.stop_calls: list[list[str]] = []

    @property
    def is_playing(self) -> bool:
        return self._playing

    def start_generation(self, caused_by: list[str]) -> str:
        self.start_calls.append(list(caused_by))
        self._playing = True
        return "gen-1"

    def request_stop(self, caused_by: list[str]) -> str:
        self.stop_calls.append(list(caused_by))
        self._playing = False
        return "stop-1"


class _StubBackchannelSource:
    def __init__(self, score: float) -> None:
        self._score = score

    def latest_score(self) -> float:
        return self._score


# Script: chunk 0 speaks (start), chunk 1 yields is_listen while playing.
_SPEAK_THEN_YIELD = [
    (False, "hi", None),   # chunk 0: speak → start
    (True, "", None),      # chunk 1: is_listen while playing → veto path
]


def test_backchannel_vetoes_barge_in() -> None:
    """bc_score=0.9: no stop, barge_in_suppressed_backchannel emitted, still playing."""
    sid = "pr3c-veto"
    logger = _FakeLogger()
    ao = _FakeAudioOutput()
    fg = _FakeForegroundModel(_SPEAK_THEN_YIELD, logger, sid)
    orch = ContinuousOrchestrator(
        session_id=sid,
        logger=logger,
        audio_in=asyncio.Queue(),
        foreground_model=fg,
        audio_output=ao,
        backchannel_source=_StubBackchannelSource(0.9),
    )
    asyncio.run(orch.run())

    # No stop — veto suppressed it
    assert ao.stop_calls == [], "request_stop must not be called when backchannel veto fires"
    assert ao.is_playing, "audio must still be playing after veto"

    # Suppressed event emitted with closed caused_by
    suppressed = [e for e in logger.events if e.event_type == "barge_in_suppressed_backchannel"]
    assert len(suppressed) == 1
    assert suppressed[0].caused_by == ["rac-1"]

    # No model_native_barge_in
    assert not any(e.event_type == "model_native_barge_in" for e in logger.events)

    # decide_chunk still returns silence on the yield chunk (blocker-2)
    pds = [e for e in logger.events if e.event_type == "policy_decision"]
    assert pds[1].payload_inline["action_type"] == "silence"

    # caused_by closure (invariant #1)
    logged_ids = {e.event_id for e in logger.events}
    for e in logger.events:
        if e.source == "continuous_orchestrator":
            assert e.caused_by and e.caused_by[0] in logged_ids, (
                f"orphan event {e.event_id} caused_by={e.caused_by}"
            )


def test_barge_in_fires_when_score_zero() -> None:
    """bc_score=0.0: PR3b path preserved — request_stop + model_native_barge_in."""
    sid = "pr3c-bi"
    logger = _FakeLogger()
    ao = _FakeAudioOutput()
    fg = _FakeForegroundModel(_SPEAK_THEN_YIELD, logger, sid)
    orch = ContinuousOrchestrator(
        session_id=sid,
        logger=logger,
        audio_in=asyncio.Queue(),
        foreground_model=fg,
        audio_output=ao,
        backchannel_source=_StubBackchannelSource(0.0),
    )
    asyncio.run(orch.run())

    assert ao.stop_calls == [["rac-1"]], "request_stop must fire on chunk 1"
    barge_ins = [e for e in logger.events if e.event_type == "model_native_barge_in"]
    assert len(barge_ins) == 1
    assert barge_ins[0].caused_by == ["rac-1"]
    assert not any(e.event_type == "barge_in_suppressed_backchannel" for e in logger.events)


def test_default_null_source_gives_pr3b_behavior() -> None:
    """No backchannel_source arg → _NullBackchannelSource (0.0) → PR3b stop (regression)."""
    sid = "pr3c-null"
    logger = _FakeLogger()
    ao = _FakeAudioOutput()
    fg = _FakeForegroundModel(_SPEAK_THEN_YIELD, logger, sid)
    orch = ContinuousOrchestrator(
        session_id=sid,
        logger=logger,
        audio_in=asyncio.Queue(),
        foreground_model=fg,
        audio_output=ao,
        # backchannel_source omitted → _NullBackchannelSource()
    )
    asyncio.run(orch.run())

    assert ao.stop_calls == [["rac-1"]], "null source must fall through to PR3b stop"
    barge_ins = [e for e in logger.events if e.event_type == "model_native_barge_in"]
    assert len(barge_ins) == 1
    assert not any(e.event_type == "barge_in_suppressed_backchannel" for e in logger.events)
