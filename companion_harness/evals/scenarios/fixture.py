"""FixtureScenarioDriver — wires SyntheticClock + DirectAudioInputFeeder → ReplayRun.

eval-subsystem-spec.md Anchor 6 (synthetic_clock timing mode).
Phase A.5 Tasks A.5-3.

Implements the ScenarioDriver Protocol.  Emits:
  fixture_audio_chunk_injected  — per chunk, per spec event table
  synthetic_clock_tick          — once per feed() call, per spec event table
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from companion_harness.schemas import Event, EvaluationCase, ReplayRun

if TYPE_CHECKING:
    from companion_harness.evals.scenarios.audio_feeder import DirectAudioInputFeeder
    from companion_harness.evals.scenarios.synthetic_clock import SyntheticClock

_SOURCE = "fixture_scenario_driver"
_SCHEMA_VERSION = "0.1"


def _make_event(
    event_id: str,
    event_type: str,
    caused_by: list[str],
    payload_kind: str,
    session_id: str,
    seq_no: int,
    mono_ms: int,
    sensitivity: str = "safe",
    retention_policy_id: str = "eval_run_30d",
    extra_hash: str = "",
) -> Event:
    payload_hash = hashlib.sha256(
        f"{event_type}:{event_id}:{extra_hash}".encode()
    ).hexdigest()[:16]
    return Event(
        event_id=event_id,
        session_id=session_id,
        schema_version=_SCHEMA_VERSION,
        seq_no=seq_no,
        event_type=event_type,
        timestamp_mono_ms=mono_ms,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
        source=_SOURCE,
        caused_by=caused_by,
        payload_hash=payload_hash,
        payload_ref=None,
        payload_kind=payload_kind,  # type: ignore[arg-type]
        subject_class="self",
        sensitivity=sensitivity,  # type: ignore[arg-type]
        retention_policy_id=retention_policy_id,
    )


class FixtureScenarioDriver:
    """Drives one EvaluationCase through SyntheticClock + DirectAudioInputFeeder.

    ``event_sink`` receives every eval bookkeeping event emitted by this driver
    (fixture_audio_chunk_injected, synthetic_clock_tick).  Pass a list or any
    callable accepting an Event.  If None, events are silently discarded.

    The audio_in queue is shared with the orchestrator under test; the driver
    puts (chunk_bytes, event_id) tuples on it directly, bypassing WebSocket.
    """

    def __init__(
        self,
        audio_in: "asyncio.Queue[tuple[bytes, str]]",
        clock: "SyntheticClock",
        feeder: "DirectAudioInputFeeder",
        event_sink: list[Event] | None = None,
    ) -> None:
        self._audio_in = audio_in
        self._clock = clock
        self._feeder = feeder
        self._event_sink = event_sink
        self._seq = 0

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _log(self, event: Event) -> None:
        if self._event_sink is not None:
            self._event_sink.append(event)

    async def run(
        self,
        case: EvaluationCase,
        harness_factory: object,
        run_config: object,
    ) -> ReplayRun:
        """Feed each fixture audio chunk, emitting eval bookkeeping events."""
        session_id = f"fixture-{case.case_id}-{uuid.uuid4().hex[:8]}"
        started_ms = self._clock.now_ms()
        started_wall = datetime.now(timezone.utc).isoformat()
        run_id = f"fsd-{case.case_id}-{started_ms}"

        # Emit benchmark_case_started event
        start_evt_id = f"{session_id}-start"
        start_evt = _make_event(
            event_id=start_evt_id,
            event_type="benchmark_case_started",
            caused_by=[],
            payload_kind="signal",
            session_id=session_id,
            seq_no=self._next_seq(),
            mono_ms=started_ms,
        )
        self._log(start_evt)

        # Load audio chunks from the fixture path (fixtures field lists paths)
        audio_chunks = _load_audio_chunks(case)

        # Emit synthetic_clock_tick before injection
        tick_evt_id = f"{session_id}-tick-{self._clock.now_ms()}"
        tick_evt = _make_event(
            event_id=tick_evt_id,
            event_type="synthetic_clock_tick",
            caused_by=[start_evt_id],
            payload_kind="signal",
            session_id=session_id,
            seq_no=self._next_seq(),
            mono_ms=self._clock.now_ms(),
        )
        self._log(tick_evt)

        # Feed chunks; emit fixture_audio_chunk_injected per chunk
        inject_event_ids: list[str] = []
        for i, chunk in enumerate(audio_chunks):
            chunk_ms = self._clock.now_ms()
            chunk_evt_id = f"{session_id}-chunk-{chunk_ms}-{i}"
            chunk_evt = _make_event(
                event_id=chunk_evt_id,
                event_type="fixture_audio_chunk_injected",
                caused_by=[tick_evt_id],
                payload_kind="raw_audio",
                session_id=session_id,
                seq_no=self._next_seq(),
                mono_ms=chunk_ms,
                sensitivity="sensitive",
                retention_policy_id="raw_media_default_300s",
                extra_hash=str(len(chunk)),
            )
            self._log(chunk_evt)
            inject_event_ids.append(chunk_evt_id)
            self._audio_in.put_nowait((chunk, chunk_evt_id))
            self._clock.advance_ms(20)

        finished_ms = self._clock.now_ms()
        finished_wall = datetime.now(timezone.utc).isoformat()

        # Emit benchmark_case_completed
        done_evt = _make_event(
            event_id=f"{session_id}-done",
            event_type="benchmark_case_completed",
            caused_by=[start_evt_id] + inject_event_ids[-1:],
            payload_kind="signal",
            session_id=session_id,
            seq_no=self._next_seq(),
            mono_ms=finished_ms,
        )
        self._log(done_evt)

        return ReplayRun(
            run_id=run_id,
            case_id=case.case_id,
            implementation_config_version="fixture_driver_v1",
            policy_version="fixture_driver_v1",
            started_at=started_wall,
            finished_at=finished_wall,
            results={"chunks_injected": len(audio_chunks)},
            failures=[],
            timing_mode="synthetic_clock",
            started_at_mono_ms=started_ms,
            completed_at_mono_ms=finished_ms,
            final_status="completed",
        )


def _load_audio_chunks(case: EvaluationCase) -> list[bytes]:
    """Load audio chunks from fixture files listed in case.fixtures.

    Each fixture path that ends in .raw or .pcm is read as raw bytes and
    treated as a single chunk.  .wav files are read and the header (44 bytes)
    is stripped.  Missing fixture paths produce an empty chunk list.

    This is intentionally minimal — eval fixtures control their own format.
    """
    chunks: list[bytes] = []
    for fixture_path in case.fixtures:
        path = Path(fixture_path)
        if not path.exists():
            continue
        raw = path.read_bytes()
        if path.suffix in (".wav",):
            # Strip 44-byte WAV header (standard PCM WAV).
            chunks.append(raw[44:] if len(raw) > 44 else raw)
        else:
            chunks.append(raw)
    return chunks
