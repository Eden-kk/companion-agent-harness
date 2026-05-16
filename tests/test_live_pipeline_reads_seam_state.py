"""D4 contract tests: build_live_pipeline() reads ConfigStore.seam_state.

Verifies that when a seam is disabled in ConfigStore, build_live_pipeline()
wires the existing neutral fallback (per plan §F2), and that use_stubs=True
overrides seam state (per plan §Risks "D4 use_stubs precedence").
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from companion_harness.event_logger import EventLogger
from companion_harness.input_ingest import InputIngest
from companion_harness.schemas import MemoryItem, ThinkerProposal
from manual_test_console.config_schema import ALLOWLIST
from manual_test_console.config_store import ConfigStore
from manual_test_console.live_pipeline import (
    EmptyTranscriptASRModel,
    EnergyVADModel,
    NoopTtsAdapter,
    SilenceSmartTurnModel,
    ZeroBackchannelModel,
    build_live_pipeline,
)


class _FakeStreamingModel:
    def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> ThinkerProposal | None:
        return None

    def set_context(self, items: list[MemoryItem]) -> None:
        return None


async def _drain(_event: Any) -> None:
    return None


@pytest_asyncio.fixture
async def _logger():
    logger = EventLogger(_drain, maxsize=64)
    await logger.start()
    try:
        yield logger
    finally:
        await logger.stop()


def _make_session(logger: EventLogger, tmp_path: Path):
    ingest = InputIngest(logger, tmp_path / "blobs")
    return ingest.open_session("test-client")


def _store_with(**disabled_seams: bool) -> ConfigStore:
    store = ConfigStore(ALLOWLIST)
    for seam, enabled in disabled_seams.items():
        store.set_seam(seam, enabled)
    return store


# ---------------------------------------------------------------------------
# Seam-gating: disabled seam → neutral fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_asr_disabled_uses_empty_transcript(_logger: EventLogger, tmp_path: Path) -> None:
    store = _store_with(asr=False)
    session = _make_session(_logger, tmp_path)
    pipeline = build_live_pipeline(
        session_id=session.session_id,
        logger=_logger,
        ingest_session=session,
        foreground_duplex_model=_FakeStreamingModel(),
        use_stubs=False,
        config_store=store,
    )
    assert isinstance(pipeline.orchestrator._asr_model, EmptyTranscriptASRModel)


@pytest.mark.asyncio
async def test_vad_disabled_uses_energy_vad(_logger: EventLogger, tmp_path: Path) -> None:
    store = _store_with(vad=False)
    session = _make_session(_logger, tmp_path)
    pipeline = build_live_pipeline(
        session_id=session.session_id,
        logger=_logger,
        ingest_session=session,
        foreground_duplex_model=_FakeStreamingModel(),
        use_stubs=False,
        config_store=store,
    )
    assert isinstance(pipeline.orchestrator._vad_detector._model, EnergyVADModel)


@pytest.mark.asyncio
async def test_smart_turn_disabled_uses_silence_model(_logger: EventLogger, tmp_path: Path) -> None:
    store = _store_with(smart_turn=False)
    session = _make_session(_logger, tmp_path)
    pipeline = build_live_pipeline(
        session_id=session.session_id,
        logger=_logger,
        ingest_session=session,
        foreground_duplex_model=_FakeStreamingModel(),
        use_stubs=False,
        config_store=store,
    )
    assert isinstance(pipeline.orchestrator._smart_turn_detector._model, SilenceSmartTurnModel)


@pytest.mark.asyncio
async def test_backchannel_disabled_uses_zero_model(_logger: EventLogger, tmp_path: Path) -> None:
    store = _store_with(backchannel=False)
    session = _make_session(_logger, tmp_path)
    pipeline = build_live_pipeline(
        session_id=session.session_id,
        logger=_logger,
        ingest_session=session,
        foreground_duplex_model=_FakeStreamingModel(),
        use_stubs=False,
        config_store=store,
    )
    assert isinstance(pipeline.orchestrator._backchannel_classifier._model, ZeroBackchannelModel)


@pytest.mark.asyncio
async def test_tts_disabled_uses_noop(_logger: EventLogger, tmp_path: Path) -> None:
    store = _store_with(tts=False)
    session = _make_session(_logger, tmp_path)
    pipeline = build_live_pipeline(
        session_id=session.session_id,
        logger=_logger,
        ingest_session=session,
        foreground_duplex_model=_FakeStreamingModel(),
        use_stubs=False,
        config_store=store,
    )
    assert isinstance(pipeline.tts_adapter, NoopTtsAdapter)


@pytest.mark.asyncio
async def test_av_conflict_scorer_disabled_becomes_none(_logger: EventLogger, tmp_path: Path) -> None:
    store = _store_with(av_conflict_scorer=False)
    session = _make_session(_logger, tmp_path)
    pipeline = build_live_pipeline(
        session_id=session.session_id,
        logger=_logger,
        ingest_session=session,
        foreground_duplex_model=_FakeStreamingModel(),
        use_stubs=False,
        config_store=store,
    )
    assert pipeline.av_conflict_scorer is None


@pytest.mark.asyncio
async def test_urgency_scorer_disabled_becomes_none(_logger: EventLogger, tmp_path: Path) -> None:
    store = _store_with(urgency_scorer=False)
    session = _make_session(_logger, tmp_path)
    pipeline = build_live_pipeline(
        session_id=session.session_id,
        logger=_logger,
        ingest_session=session,
        foreground_duplex_model=_FakeStreamingModel(),
        use_stubs=False,
        config_store=store,
    )
    assert pipeline.urgency_scorer is None


@pytest.mark.asyncio
async def test_embedder_disabled_becomes_none(_logger: EventLogger, tmp_path: Path) -> None:
    store = _store_with(embedder=False)
    session = _make_session(_logger, tmp_path)
    pipeline = build_live_pipeline(
        session_id=session.session_id,
        logger=_logger,
        ingest_session=session,
        foreground_duplex_model=_FakeStreamingModel(),
        use_stubs=False,
        config_store=store,
    )
    assert pipeline.embedder is None


@pytest.mark.asyncio
async def test_attachment_risk_monitor_disabled_skips_late_subscribe(
    _logger: EventLogger, tmp_path: Path
) -> None:
    """When attachment_risk_monitor is disabled, late_subscribe is NOT called."""
    subscribed_callbacks: list = []
    original_late_subscribe = _logger.late_subscribe

    def _spy_late_subscribe(cb: Any) -> None:
        subscribed_callbacks.append(cb)
        original_late_subscribe(cb)

    _logger.late_subscribe = _spy_late_subscribe  # type: ignore[method-assign]

    store = _store_with(attachment_risk_monitor=False)
    session = _make_session(_logger, tmp_path)
    build_live_pipeline(
        session_id=session.session_id,
        logger=_logger,
        ingest_session=session,
        foreground_duplex_model=_FakeStreamingModel(),
        use_stubs=False,
        config_store=store,
    )
    # No ARM callback should have been subscribed.
    from companion_harness.attachment_risk_monitor import EventStreamAttachmentRiskMonitor
    arm_callbacks = [cb for cb in subscribed_callbacks if hasattr(cb, "__self__") and isinstance(cb.__self__, EventStreamAttachmentRiskMonitor)]
    assert not arm_callbacks


# ---------------------------------------------------------------------------
# Precedence: use_stubs=True overrides ConfigStore seam state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_use_stubs_overrides_seam_state(_logger: EventLogger, tmp_path: Path) -> None:
    """use_stubs=True must override ConfigStore: all seams effectively forced to stubs."""
    store = ConfigStore(ALLOWLIST)  # all seams enabled by default
    session = _make_session(_logger, tmp_path)
    pipeline = build_live_pipeline(
        session_id=session.session_id,
        logger=_logger,
        ingest_session=session,
        foreground_duplex_model=_FakeStreamingModel(),
        use_stubs=True,
        config_store=store,
    )
    # Stubs should be in place even though ConfigStore says all seams enabled.
    assert isinstance(pipeline.orchestrator._vad_detector._model, EnergyVADModel)
    assert isinstance(pipeline.orchestrator._asr_model, EmptyTranscriptASRModel)
    assert isinstance(pipeline.tts_adapter, NoopTtsAdapter)


# ---------------------------------------------------------------------------
# Default: all seams enabled → same behavior as config_store=None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_seams_enabled_no_regression(_logger: EventLogger, tmp_path: Path) -> None:
    """With all seams enabled, pipeline wiring is identical to config_store=None."""
    store = ConfigStore(ALLOWLIST)  # all seams enabled by default
    session = _make_session(_logger, tmp_path)
    pipeline = build_live_pipeline(
        session_id=session.session_id,
        logger=_logger,
        ingest_session=session,
        foreground_duplex_model=_FakeStreamingModel(),
        use_stubs=False,
        config_store=store,
    )
    # Default behavior: stub fallbacks are in place (no real adapters injected).
    assert isinstance(pipeline.orchestrator._vad_detector._model, EnergyVADModel)
    assert isinstance(pipeline.orchestrator._asr_model, EmptyTranscriptASRModel)
    assert isinstance(pipeline.tts_adapter, NoopTtsAdapter)
    assert pipeline.av_conflict_scorer is None
    assert pipeline.urgency_scorer is None
    assert pipeline.embedder is None
