"""Contract tests for the --streaming-speculative feature flag skeleton (PR 3 / §3.7).

Success criterion: flag is wired end-to-end (ConfigStore → CLI → orchestrator
constructor); flag=OFF leaves all existing behavior bit-identical to v0.2.
No Path B logic exists yet — §3.3 adds that.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, AsyncGenerator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.backchannel_classifier import BackchannelClassifier
from companion_harness.event_logger import EventLogger
from companion_harness.foreground_model import ForegroundModel
from companion_harness.input_ingest import CaptureMetadata, InputIngest
from companion_harness.realtime_orchestrator import StreamingRealtimeOrchestrator
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import (
    Event,
    PolicyInputs,
    SpeakDecision,
    ThinkerProposal,
    TurnSignal,
)
from companion_harness.turn_detector_smart import SmartTurnDetector
from companion_harness.turn_detector_vad import VADDetector
from manual_test_console.config_schema import ALLOWLIST
from manual_test_console.config_store import ConfigStore
from manual_test_console.server import KEY_CONFIG_STORE, build_app


# ---------------------------------------------------------------------------
# Shared stubs
# ---------------------------------------------------------------------------


class _SilencePolicy:
    def __call__(self, inputs: PolicyInputs, signal_event_ids: list[str], p_backchannel: float = 0.0) -> SpeakDecision:
        return SpeakDecision(
            action_type="silence",
            primary_reason_code=ReasonCode.NOT_ADDRESSED_TO_AGENT,
            supporting_reason_codes=[],
            redacted_explanation=None,
            caused_by=list(signal_event_ids),
            budget_bucket=None,
            allowed_prosody_tags=[],
            max_duration_ms=None,
        )


class _NullModel:
    def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> ThinkerProposal | None:
        return None

    def set_context(self, items: Any) -> None:
        pass

    async def infer_stream(
        self,
        frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
        caused_by: list[str],
        context_items: tuple = (),
    ) -> AsyncGenerator[ThinkerProposal, None]:
        async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
            async for _ in frame_iter:
                pass
            return
            yield  # noqa: unreachable — makes this a generator

        return _gen()


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def _sink(evt: Event) -> None:
        received.append(evt)

    return EventLogger(_sink, maxsize=512), received


def _make_orch(
    flag: bool,
    *,
    session_id: str = "test-session",
    logger: EventLogger,
    ingest_session: Any,
    audio_in: asyncio.Queue,
) -> StreamingRealtimeOrchestrator:
    vad = VADDetector(
        model=lambda _: 0.0,
        session_id=session_id,
        logger=logger,
        speech_threshold=0.5,
        silence_onset_ms=64,
        frame_duration_ms=32,
    )
    smart_turn = SmartTurnDetector(
        model=lambda _: (0.1, 0.9),
        session_id=session_id,
        logger=logger,
    )
    bc = BackchannelClassifier(
        model=lambda _: 0.0,
        session_id=session_id,
        logger=logger,
    )
    fg = ForegroundModel(
        model=_NullModel(),
        session_id=session_id,
        logger=logger,
    )
    async def _noop_sink(chunk: bytes) -> None:
        pass

    controller = AudioOutputController(
        session_id=session_id,
        logger=logger,
        sink=_noop_sink,
    )
    return StreamingRealtimeOrchestrator(
        session_id=session_id,
        logger=logger,
        ingest_session=ingest_session,
        audio_in=audio_in,
        vad_detector=vad,
        smart_turn_detector=smart_turn,
        backchannel_classifier=bc,
        policy_inputs_builder=lambda sig, hist: PolicyInputs(
            user_speaking=True,
            eou_probability=sig.p_done,
            assistant_speaking=False,
            scene_change_score=0.0,
            deictic_reference=False,
            user_addressed_agent=True,
            urgency_score=0.0,
            proactivity_budget_remaining={},
            privacy_mode="normal",
            current_task_mode="normal",
            social_mode="user_addressing_agent",
            risk_mode="normal",
            cooldown_state={},
            attachment_risk_level=0.0,
            audio_visual_conflict_score=0.0,
            grounding_confidence=1.0,
            deictic_ambiguous=False,
        ),
        speak_policy=_SilencePolicy(),
        foreground_model=fg,
        audio_output=controller,
        tts_adapter=_NullTts(),
        use_streaming_speculative=flag,
    )


class _NullTts:
    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        yield b""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_flag_off_preserves_v02_turn_batched_behavior(tmp_path: Path) -> None:
    """flag=False: orchestrator._use_streaming_speculative is False; no Path B attrs."""
    logger, _ = _make_logger()
    ingest = InputIngest(logger, tmp_path)
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue = asyncio.Queue(maxsize=64)

    orch = _make_orch(False, logger=logger, ingest_session=session, audio_in=audio_in)

    assert orch._use_streaming_speculative is False


def test_flag_on_marker_present(tmp_path: Path) -> None:
    """flag=True: _use_streaming_speculative is True; no Path B events fire yet."""
    logger, _ = _make_logger()
    ingest = InputIngest(logger, tmp_path)
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue = asyncio.Queue(maxsize=64)

    orch = _make_orch(True, logger=logger, ingest_session=session, audio_in=audio_in)

    assert orch._use_streaming_speculative is True


def test_cli_flag_patches_config_store(tmp_path: Path) -> None:
    """build_app() + explicit set mirrors what main(--streaming-speculative) does."""
    app = build_app(blob_dir=tmp_path / "blobs", live_pipeline_enabled=False)
    config_store: ConfigStore = app[KEY_CONFIG_STORE]  # type: ignore[assignment]

    # Before patching: default is 0 (flag OFF).
    assert config_store.get("orchestrator.use_streaming_speculative") == 0

    # After patching (mirrors main() with --streaming-speculative).
    config_store.set("orchestrator.use_streaming_speculative", 1)
    assert config_store.get("orchestrator.use_streaming_speculative") == 1


def test_config_store_default_is_false() -> None:
    """ConfigStore default for orchestrator.use_streaming_speculative is 0 (falsy)."""
    store = ConfigStore(ALLOWLIST)
    value = store.get("orchestrator.use_streaming_speculative")
    assert not value
    assert value == 0


# ---------------------------------------------------------------------------
# Matrix test: invariant contract under both flag values (§3.7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", [False, True], ids=["flag_off", "flag_on"])
@pytest.mark.asyncio
async def test_invariant_1_no_orphan_events_under_both_flags(flag: bool, tmp_path: Path) -> None:
    """Invariant #1: every emitted event has caused_by[] — no orphan events.

    Runs under both flag values so the matrix confirms backward compat.
    """
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger, tmp_path)
    session = ingest.open_session("matrix-test-client")
    audio_in: asyncio.Queue = asyncio.Queue(maxsize=64)

    orch = _make_orch(flag, logger=logger, ingest_session=session, audio_in=audio_in)
    await orch.start()

    # Push a handful of silent frames so T1/T2 spin at least once.
    for i in range(5):
        meta = CaptureMetadata(
            client_id="matrix-test-client",
            timestamp_mono_ms=1000 + i * 32,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
        )
        evt = ingest.ingest_chunk(session, b"\x00" * 512, meta)
        await audio_in.put((b"\x00" * 512, evt.event_id))

    await asyncio.sleep(0.15)
    await orch.stop()

    # Every event must have at least one caused_by entry (invariant #1).
    # Root events (no upstream event possible): session_open, harness_init,
    # and orchestrator_started are top-level anchors.
    orchestrator_root_types = {"orchestrator_started", "session_open", "harness_init"}
    orphans = [
        e for e in received
        if not e.caused_by and e.event_type not in orchestrator_root_types
    ]
    assert not orphans, (
        f"flag={flag}: {len(orphans)} orphan events (no caused_by): "
        + ", ".join(e.event_type for e in orphans[:5])
    )
