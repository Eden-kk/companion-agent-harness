"""Smoke tests for the real-detector wrapper modules.

These tests verify the import path and Protocol conformance of the three
real-model adapters without actually loading any model. The full model
loads (Silero ONNX, Pipecat Smart Turn v3 ONNX, faster-whisper tiny) are
exercised only on b200 — the manual_test_console.server's startup path
loads them lazily.

Determinism (invariant #5) is checked at the module surface: the same
PCM bytes through the same model instance must yield the same probability.
The actual model loads are skipped here when onnxruntime / faster_whisper
are unavailable in the local dev venv.
"""

from __future__ import annotations

import importlib

import pytest

from companion_harness.backchannel_classifier import BackchannelModel
from companion_harness.turn_detector_smart import SmartTurnModel
from companion_harness.turn_detector_vad import VADModel


def test_vad_silero_module_importable_without_load() -> None:
    """Importing the wrapper module must not load any model.

    The constructor lazy-imports onnxruntime; the module-level import is
    cheap (just numpy + stdlib).
    """
    mod = importlib.import_module("companion_harness.vad_silero")
    assert hasattr(mod, "SileroVADModel")


def test_smart_turn_pipecat_module_importable_without_load() -> None:
    mod = importlib.import_module("companion_harness.smart_turn_pipecat")
    assert hasattr(mod, "PipecatSmartTurnModel")


def test_backchannel_asr_lexicon_module_importable_without_load() -> None:
    mod = importlib.import_module("companion_harness.backchannel_asr_lexicon")
    assert hasattr(mod, "ASRLexiconBackchannelModel")
    assert hasattr(mod, "BACKCHANNEL_PHRASES")


def test_backchannel_lexicon_matches_expected_phrases() -> None:
    """Verify the curated lexicon recognises canonical backchannels.

    Pure-Python; no model load required. Pins the matcher behaviour so a
    refactor cannot silently break the lexicon.
    """
    from companion_harness.backchannel_asr_lexicon import _is_backchannel

    for phrase in ("yeah", "Yeah.", "mm-hmm", "uh-huh", "right", "I see", "OK", "okay", "got it"):
        assert _is_backchannel(phrase), f"expected {phrase!r} to be a backchannel"

    for phrase in (
        "what time is it",
        "tell me about the project",
        "I want to ask you a question",
    ):
        assert not _is_backchannel(phrase), f"unexpected backchannel match: {phrase!r}"


def test_real_detectors_satisfy_protocols_when_constructed() -> None:
    """If onnxruntime / faster_whisper are installed, real detectors must
    structurally satisfy their Protocols. Otherwise skip — local dev venvs
    typically lack these.
    """
    try:
        import onnxruntime  # noqa: F401
    except Exception:
        pytest.skip("onnxruntime not installed in this env")
    # silero_vad package must be installed (we don't import it — torchaudio
    # incompatibility — but the ONNX file ships inside its data/ dir).
    import importlib.util

    if importlib.util.find_spec("silero_vad") is None:
        pytest.skip("silero_vad package not installed in this env")

    from companion_harness.vad_silero import SileroVADModel

    m = SileroVADModel()
    assert isinstance(m, VADModel)
    # Determinism: same bytes → same probability after reset.
    frame = b"\x00\x00" * 512
    m.reset_states()
    p1 = m(frame)
    m.reset_states()
    p2 = m(frame)
    assert abs(p1 - p2) < 1e-6


def test_live_pipeline_factory_uses_stubs_by_default(tmp_path) -> None:
    """build_live_pipeline must accept use_stubs=True without touching the
    real detector classes (no torch / onnxruntime import).

    This guards the contract that the test suite never loads a real model.
    """
    import asyncio

    from companion_harness.event_logger import EventLogger
    from companion_harness.input_ingest import InputIngest
    from companion_harness.schemas import Event
    from manual_test_console.live_pipeline import (
        EnergyVADModel,
        SilenceSmartTurnModel,
        ZeroBackchannelModel,
        build_live_pipeline,
    )

    async def _noop_sink(_event: Event) -> None:
        return None

    async def _run() -> None:
        logger = EventLogger(_noop_sink, maxsize=64)
        await logger.start()
        ingest = InputIngest(logger, tmp_path / "blobs")
        session = ingest.open_session("client")

        class _FakeForeground:
            def infer(self, audio_frame, video_frame=None):
                return None

            def set_context(self, items):
                return None

            async def infer_stream(self, frame_iter, caused_by):
                async def _gen():
                    async for _ in frame_iter:
                        pass
                    return
                    yield  # pragma: no cover

                return _gen()

        pipeline = build_live_pipeline(
            session_id=session.session_id,
            logger=logger,
            ingest_session=session,
            foreground_duplex_model=_FakeForeground(),
            use_stubs=True,
        )
        # Inspect the detector model instances via the orchestrator wiring —
        # `vad_detector._model` etc. The factory must have picked the stubs.
        orch = pipeline.orchestrator
        assert isinstance(orch._vad_detector._model, EnergyVADModel)
        assert isinstance(orch._smart_turn_detector._model, SilenceSmartTurnModel)
        assert isinstance(orch._backchannel_classifier._model, ZeroBackchannelModel)

        await logger.stop()

    asyncio.run(_run())
