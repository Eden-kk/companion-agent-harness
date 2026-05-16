"""MiniCPMDeicticDetector — CPU contract tests (closes #169, RFC #237).

7 contract tests using a fake duplex model (no GPU):
  - test_empty_transcript_returns_false
  - test_parses_yes_with_confidence
  - test_parses_no
  - test_handles_malformed_response
  - test_handles_chat_exception
  - test_satisfies_protocol
  - test_handles_missing_frame
"""

from __future__ import annotations

from companion_harness.deictic_detector import DeicticModel
from companion_harness.deictic_detector_minicpm import MiniCPMDeicticDetector


# ---------------------------------------------------------------------------
# Fake duplex models
# ---------------------------------------------------------------------------


class _FakeYesModel:
    def chat(self, text: str, max_new_tokens: int = 16) -> str:
        return "yes 0.87"


class _FakeNoModel:
    def chat(self, text: str, max_new_tokens: int = 16) -> str:
        return "no 0.92"


class _FakeGarbageModel:
    def chat(self, text: str, max_new_tokens: int = 16) -> str:
        return "i am unsure about this"


class _FakeRaisingModel:
    def chat(self, text: str, max_new_tokens: int = 16) -> str:
        raise RuntimeError("model unavailable")


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------


def test_empty_transcript_returns_false() -> None:
    det = MiniCPMDeicticDetector(_FakeYesModel())  # type: ignore[arg-type]
    assert det("", None) == (False, 0.0)
    assert det("   ", None) == (False, 0.0)


def test_parses_yes_with_confidence() -> None:
    det = MiniCPMDeicticDetector(_FakeYesModel())  # type: ignore[arg-type]
    is_deictic, confidence = det("look at this", None)
    assert is_deictic is True
    assert confidence == 0.87


def test_parses_no() -> None:
    """A 'no' answer always yields confidence 0.0 regardless of model number."""
    det = MiniCPMDeicticDetector(_FakeNoModel())  # type: ignore[arg-type]
    is_deictic, confidence = det("tell me a story", None)
    assert is_deictic is False
    assert confidence == 0.0


def test_handles_malformed_response() -> None:
    det = MiniCPMDeicticDetector(_FakeGarbageModel())  # type: ignore[arg-type]
    assert det("what is that", None) == (False, 0.0)


def test_handles_chat_exception() -> None:
    det = MiniCPMDeicticDetector(_FakeRaisingModel())  # type: ignore[arg-type]
    assert det("look at this", None) == (False, 0.0)


def test_satisfies_protocol() -> None:
    det = MiniCPMDeicticDetector(_FakeYesModel())  # type: ignore[arg-type]
    assert isinstance(det, DeicticModel)


def test_handles_missing_frame() -> None:
    """frame_bytes / audio_buffer may be None or bytes; both are accepted."""
    det = MiniCPMDeicticDetector(_FakeYesModel())  # type: ignore[arg-type]
    assert det("look at this", None) == (True, 0.87)
    assert det("look at this", b"\x00\x01\x02") == (True, 0.87)
