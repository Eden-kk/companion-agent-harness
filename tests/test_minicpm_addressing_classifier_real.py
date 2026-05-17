"""MiniCPMAddressingClassifierImpl — unit + GPU integration tests.

Tests:
  test_minicpm_addressing_classifier_returns_signal_or_none
      — fixture model + transcript; asserts return type contract.
  test_falls_back_to_wake_word_when_minicpm_none
      — _NullMiniCPMAddressingClassifier returns None; WakeWord safety-net fires.
  test_no_unavailable_157_marker_in_real_impl
      — MiniCPMAddressingClassifierImpl source has no stale UNAVAILABLE: #157 marker.

GPU test (requires b200 venv):
  test_minicpm_addressing_classifier_real_inference
      — real MiniCPMDuplexModel.chat() is called; return is AddressingSignal or None.
"""

from __future__ import annotations

import inspect

import pytest

import companion_harness.addressing_classifier as _acmod
from companion_harness.addressing_classifier import (
    AddressingSignal,
    MiniCPMAddressingClassifierImpl,
    WakeWordAddressingClassifier,
    _NullMiniCPMAddressingClassifier,
    derive_user_addressed_agent,
)


# ---------------------------------------------------------------------------
# Fake model for CPU-only tests (logprob interface)
# ---------------------------------------------------------------------------


class _FakeYesModel:
    """Fake model returning (True, 0.9) from classify_yes_no."""

    def classify_yes_no(self, prompt: str) -> tuple[bool, float]:
        return True, 0.9


class _FakeNoModel:
    """Fake model returning (False, 0.1) from classify_yes_no."""

    def classify_yes_no(self, prompt: str) -> tuple[bool, float]:
        return False, 0.1


class _FakeAmbivalentModel:
    """Fake model returning ambivalent prob_yes=0.5 from classify_yes_no."""

    def classify_yes_no(self, prompt: str) -> tuple[bool, float]:
        return False, 0.5


class _FakeRaisingModel:
    """Fake model that raises on classify_yes_no."""

    def classify_yes_no(self, prompt: str) -> tuple[bool, float]:
        raise RuntimeError("model unavailable")


# ---------------------------------------------------------------------------
# CPU tests
# ---------------------------------------------------------------------------


def test_minicpm_addressing_classifier_returns_signal_or_none() -> None:
    """classify() returns AddressingSignal or None — never raises."""
    clf = MiniCPMAddressingClassifierImpl(_FakeYesModel())  # type: ignore[arg-type]
    result = clf("tell me the weather", speaker_count=None, social_mode="user_addressing_agent")
    assert result is None or isinstance(result, AddressingSignal)


def test_minicpm_classifier_yes_returns_explicit_signal() -> None:
    clf = MiniCPMAddressingClassifierImpl(_FakeYesModel())  # type: ignore[arg-type]
    result = clf("what time is it", speaker_count=None, social_mode="user_addressing_agent")
    assert isinstance(result, AddressingSignal)
    assert result.confidence == "explicit"
    assert result.evidence.startswith("minicpm_logprob:yes:")


def test_minicpm_classifier_no_returns_implicit_signal() -> None:
    clf = MiniCPMAddressingClassifierImpl(_FakeNoModel())  # type: ignore[arg-type]
    result = clf("tell him about it", speaker_count=None, social_mode="user_addressing_other")
    assert isinstance(result, AddressingSignal)
    assert result.confidence == "implicit"
    assert result.evidence.startswith("minicpm_logprob:no:")


def test_minicpm_classifier_raising_model_returns_none() -> None:
    clf = MiniCPMAddressingClassifierImpl(_FakeRaisingModel())  # type: ignore[arg-type]
    result = clf("something", speaker_count=None, social_mode="user_addressing_agent")
    assert result is None


def test_minicpm_classifier_empty_transcript_returns_none() -> None:
    clf = MiniCPMAddressingClassifierImpl(_FakeYesModel())  # type: ignore[arg-type]
    result = clf("", speaker_count=None, social_mode="user_addressing_agent")
    assert result is None


def test_falls_back_to_wake_word_when_minicpm_none() -> None:
    """_NullMiniCPMAddressingClassifier returns None; WakeWord safety-net fires correctly."""
    null_clf = _NullMiniCPMAddressingClassifier()
    wake_word_clf = WakeWordAddressingClassifier()

    # Simulate orchestrator routing: try MiniCPM first, fall back to WakeWord
    transcript = "hey companion what time is it"
    speaker_count = None
    social_mode = "user_addressing_agent"

    minicpm_result = null_clf(transcript, speaker_count, social_mode)
    assert minicpm_result is None

    signal = wake_word_clf(transcript, speaker_count, social_mode)
    assert signal.confidence == "explicit"
    assert derive_user_addressed_agent(signal, social_mode) is True


def test_no_unavailable_157_marker_in_real_impl() -> None:
    """MiniCPMAddressingClassifierImpl source has no stale UNAVAILABLE: #157 marker."""
    source = inspect.getsource(MiniCPMAddressingClassifierImpl)
    assert "UNAVAILABLE: #157" not in source


def test_minicpm_addressing_classifier_calls_classify_yes_no() -> None:
    """Mock contract: real implementation calls model.classify_yes_no(prompt)
    with a single positional str argument and unpacks (is_yes, prob_yes).

    Guards against drift between the real call signature and what fakes mock.
    """
    calls: list[str] = []

    class _RecordingModel:
        def classify_yes_no(self, prompt: str) -> tuple[bool, float]:
            calls.append(prompt)
            return True, 0.8

    clf = MiniCPMAddressingClassifierImpl(_RecordingModel())  # type: ignore[arg-type]
    result = clf("hello world", speaker_count=None, social_mode="single_user_voice_only")
    assert len(calls) == 1
    assert isinstance(calls[0], str)
    assert "hello world" in calls[0]
    assert result is not None


# ---------------------------------------------------------------------------
# GPU test (b200 venv required)
# ---------------------------------------------------------------------------


torch = pytest.importorskip("torch", reason="torch not available — b200 venv required")

if not torch.cuda.is_available():
    pytest.skip("CUDA not available — b200 required", allow_module_level=True)


@pytest.mark.gpu
def test_minicpm_addressing_classifier_real_inference() -> None:
    """Real MiniCPMStreamingModel.classify_yes_no() is called; return is AddressingSignal or None."""
    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel

    model = MiniCPMStreamingModel()
    clf = MiniCPMAddressingClassifierImpl(model)

    result = clf("hey assistant what time is it", speaker_count=None, social_mode="user_addressing_agent")
    assert result is None or isinstance(result, AddressingSignal), (
        f"Expected AddressingSignal or None, got {type(result)}"
    )


@pytest.mark.gpu
def test_classify_yes_no_runs_without_typeerror() -> None:
    """Regression for 2026-05-17 TypeError: MiniCPMO.forward() missing 'data' arg.

    Real-model exercise of the fix. Prior to PR #340, this raised
    `TypeError: MiniCPMO.forward() missing 1 required positional argument: 'data'`
    on every call.
    """
    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel

    model = MiniCPMStreamingModel()
    is_yes, prob_yes = model.classify_yes_no(
        "Transcript: 'what time is it?'. "
        "Is the user addressing an AI assistant? "
        "Answer with only 'yes' or 'no'."
    )
    assert is_yes is True, f"clearly-addressed prompt classified no (prob_yes={prob_yes:.3f})"
    assert 0.0 <= prob_yes <= 1.0
