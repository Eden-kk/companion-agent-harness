"""PR3a: decide_chunk wired into ContinuousOrchestrator; starts speech on a speak decision.

CPU-only (no GPU / no weights). Stubs the foreground model (a scripted is_listen
sequence; logs a raw_audio_chunk event per chunk so caused_by closes) and the
audio output. Validates the wiring + start-on-speak + no-double-start + closure.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from companion_harness.continuous_orchestrator import ContinuousOrchestrator
from companion_harness.schemas import Event


def _raw_audio_chunk_event(i: int, session_id: str) -> Event:
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
    """Yields scripted (is_listen, text, audio_kv_len) per chunk, logging a
    raw_audio_chunk event per chunk so the orchestrator's caused_by closes."""

    def __init__(self, script, logger, session_id) -> None:
        self._script = script
        self._logger = logger
        self._sid = session_id

    async def stream_chunks(self, audio_in):
        for i, (is_listen, text, kv) in enumerate(self._script):
            evt = _raw_audio_chunk_event(i, self._sid)
            self._logger.log(evt)
            yield (is_listen, text, kv, evt.event_id)


class _FakeAudioOutput:
    def __init__(self) -> None:
        self._playing = False
        self.start_calls: list[list[str]] = []

    @property
    def is_playing(self) -> bool:
        return self._playing

    def start_generation(self, *, caused_by: list[str]) -> str:
        self.start_calls.append(list(caused_by))
        self._playing = True
        return "gen-1"


def test_decide_chunk_wired_and_starts_speech() -> None:
    sid = "pr3a-test"
    logger = _FakeLogger()
    ao = _FakeAudioOutput()
    # chunk 0: listen → silence (not playing → no start)
    # chunk 1: speak  → full_response (not playing → start, is_playing→True)
    # chunk 2: speak  → full_response (already playing → no double-start)
    script = [(True, "", None), (False, "hi", None), (False, "more", None)]
    fg = _FakeForegroundModel(script, logger, sid)
    orch = ContinuousOrchestrator(
        session_id=sid,
        logger=logger,
        audio_in=asyncio.Queue(),
        foreground_model=fg,
        audio_output=ao,
    )
    asyncio.run(orch.run())

    # start_generation called exactly once, on chunk 1, caused_by that chunk's raw_audio_chunk
    assert ao.start_calls == [["rac-1"]]

    pds = [e for e in logger.events if e.event_type == "policy_decision"]
    assert [e.payload_inline["action_type"] for e in pds] == [
        "silence", "full_response", "full_response",
    ]

    ccp = [e for e in logger.events if e.event_type == "continuous_chunk_processed"]
    assert len(ccp) == 3

    # caused_by closure (invariant #1): every orchestrator event resolves to a logged id.
    logged_ids = {e.event_id for e in logger.events}
    for e in logger.events:
        if e.source == "continuous_orchestrator":
            assert e.caused_by and e.caused_by[0] in logged_ids

    # PR3a is start-only via the stub; no real TTS/assistant_audio events.
    assert not any(
        e.event_type.startswith(("assistant_audio", "tts_")) for e in logger.events
    )


def test_silence_when_model_listens() -> None:
    sid = "pr3a-listen"
    logger = _FakeLogger()
    ao = _FakeAudioOutput()
    fg = _FakeForegroundModel([(True, "", None), (True, "", None)], logger, sid)
    orch = ContinuousOrchestrator(
        session_id=sid, logger=logger, audio_in=asyncio.Queue(),
        foreground_model=fg, audio_output=ao,
    )
    asyncio.run(orch.run())
    assert ao.start_calls == []  # model never tried to speak → no synthesis
    pds = [e for e in logger.events if e.event_type == "policy_decision"]
    assert all(e.payload_inline["action_type"] == "silence" for e in pds)
