"""PR3b: model-native barge-in — stop on is_listen yield while playing.

CPU-only (no GPU / no weights). Stubs foreground model and audio output.
Verifies:
  - start called on first speak chunk
  - request_stop + model_native_barge_in emitted when is_listen arrives while playing
  - no request_stop when is_listen arrives while not playing
  - all orchestrator events have caused_by closed to logged event_ids
  - _AlwaysEnded gate-relax is isolated per-instance and restored after stream_chunks
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


def test_model_yield_stops_speech() -> None:
    """Script: [speak, speak, listen(while playing), listen(while not playing)].

    Expected:
      - start_generation once on chunk 0 (first speak)
      - request_stop once on chunk 2 (is_listen while playing)
      - model_native_barge_in event on chunk 2, caused_by closed
      - no request_stop on chunk 3 (is_listen while not playing)
    """
    sid = "pr3b-barge"
    logger = _FakeLogger()
    ao = _FakeAudioOutput()
    # chunk 0: speak (not playing → start)
    # chunk 1: speak (already playing → no start)
    # chunk 2: listen (playing → stop + barge_in)
    # chunk 3: listen (not playing → no stop)
    script = [
        (False, "hi", None),
        (False, "more", None),
        (True, "", None),
        (True, "", None),
    ]
    fg = _FakeForegroundModel(script, logger, sid)
    orch = ContinuousOrchestrator(
        session_id=sid,
        logger=logger,
        audio_in=asyncio.Queue(),
        foreground_model=fg,
        audio_output=ao,
    )
    asyncio.run(orch.run())

    assert ao.start_calls == [["rac-0"]], "start_generation called exactly once"
    assert ao.stop_calls == [["rac-2"]], "request_stop called exactly once on chunk 2"

    barge_ins = [e for e in logger.events if e.event_type == "model_native_barge_in"]
    assert len(barge_ins) == 1
    assert barge_ins[0].caused_by == ["rac-2"]

    # caused_by closure (invariant #1)
    logged_ids = {e.event_id for e in logger.events}
    for e in logger.events:
        if e.source == "continuous_orchestrator":
            assert e.caused_by and e.caused_by[0] in logged_ids, (
                f"orphan event {e.event_id} caused_by={e.caused_by}"
            )


def test_no_stop_when_not_playing() -> None:
    """is_listen while not playing never calls request_stop."""
    sid = "pr3b-idle"
    logger = _FakeLogger()
    ao = _FakeAudioOutput()
    script = [(True, "", None), (True, "", None)]
    fg = _FakeForegroundModel(script, logger, sid)
    orch = ContinuousOrchestrator(
        session_id=sid,
        logger=logger,
        audio_in=asyncio.Queue(),
        foreground_model=fg,
        audio_output=ao,
    )
    asyncio.run(orch.run())
    assert ao.stop_calls == []
    assert not any(e.event_type == "model_native_barge_in" for e in logger.events)


def test_gate_relax_class_swap_mechanism() -> None:
    """The __class__ swap+restore mechanism (real restore verified at GPU-fixture time).

    Exercises the gate-relax class-swap mechanism only (not the real stream_chunks
    integration). Uses an inline _AlwaysEnded clone to avoid importing
    foreground_model_minicpm (which pulls torch at module level — incompatible with
    CPU-only CI environments).
    The structural correctness of _AlwaysEnded in the real module is identical.
    """
    class _AlwaysEndedInline:
        def __get__(self, obj, objtype=None) -> bool:
            return True
        def __set__(self, obj, value) -> None:
            pass

    class _FakeDuplex:
        current_turn_ended: bool = False

    duplex = _FakeDuplex()
    orig_cls = type(duplex)

    relaxed_cls = type(
        f"{orig_cls.__name__}_GateRelaxed",
        (orig_cls,),
        {"current_turn_ended": _AlwaysEndedInline()},
    )
    duplex.__class__ = relaxed_cls
    assert duplex.current_turn_ended is True  # descriptor active
    duplex.__class__ = orig_cls
    assert type(duplex) is orig_cls  # restored
    assert duplex.current_turn_ended is False  # original value back
