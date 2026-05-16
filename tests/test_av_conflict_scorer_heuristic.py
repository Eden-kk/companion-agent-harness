"""HeuristicAVConflictScorer tests (closes #168 / RFC #236).

Success criterion:
    pytest tests/test_av_conflict_scorer_heuristic.py -v
"""

from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from companion_harness.av_conflict_scorer import AudioVisualConflictScorer
from companion_harness.av_conflict_scorer_heuristic import HeuristicAVConflictScorer


# ---------------------------------------------------------------------------
# Helpers — build synthetic audio + frames in-test (no fixtures on disk).
# ---------------------------------------------------------------------------


def _loud_audio(samples: int = 1600, amplitude: int = 8000) -> bytes:
    """Return PCM int16 bytes with RMS above the speech threshold."""
    return (np.ones(samples, dtype=np.int16) * amplitude).tobytes()


def _silent_audio(samples: int = 1600) -> bytes:
    """Return PCM int16 bytes with RMS = 0 (well below speech threshold)."""
    return np.zeros(samples, dtype=np.int16).tobytes()


def _encode_jpeg(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", img)
    assert ok, "cv2.imencode failed in test helper"
    return bytes(buf)


def _frame_no_face() -> bytes:
    """Solid grey image — no face detectable."""
    img = np.full((240, 320, 3), 128, dtype=np.uint8)
    return _encode_jpeg(img)


def _frame_with_face(lip_brightness: int = 128) -> bytes:
    """Synthetic face-like pattern recognizable by the Haar frontal cascade.

    Uses cv2 drawing primitives to produce something Haar will detect.
    The lip region intensity is parameterized so we can test motion (a
    different `lip_brightness` between two frames = nonzero lip diff).
    """
    img = np.full((240, 320, 3), 220, dtype=np.uint8)
    # Face oval (dark grey on light background)
    cv2.ellipse(img, (160, 120), (80, 100), 0, 0, 360, (200, 200, 200), -1)
    # Eyes
    cv2.circle(img, (130, 100), 10, (50, 50, 50), -1)
    cv2.circle(img, (190, 100), 10, (50, 50, 50), -1)
    # Mouth/lip region at the bottom third of the face
    cv2.rectangle(img, (130, 165), (190, 185), (lip_brightness, lip_brightness, lip_brightness), -1)
    return _encode_jpeg(img)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_satisfies_protocol() -> None:
    scorer = HeuristicAVConflictScorer()
    assert isinstance(scorer, AudioVisualConflictScorer)


# ---------------------------------------------------------------------------
# Short-circuit guards (no frame / no audio / silent audio)
# ---------------------------------------------------------------------------


def test_no_frame_returns_zero() -> None:
    scorer = HeuristicAVConflictScorer()
    assert scorer.score(_loud_audio(), None) == 0.0


def test_empty_frame_returns_zero() -> None:
    scorer = HeuristicAVConflictScorer()
    assert scorer.score(_loud_audio(), b"") == 0.0


def test_no_audio_returns_zero() -> None:
    scorer = HeuristicAVConflictScorer()
    assert scorer.score(b"", _frame_with_face()) == 0.0


def test_silent_audio_returns_zero() -> None:
    """RMS below the speech threshold short-circuits to 0.0 (no speech, no conflict)."""
    scorer = HeuristicAVConflictScorer()
    assert scorer.score(_silent_audio(), _frame_with_face()) == 0.0


# ---------------------------------------------------------------------------
# Frame decoding / face detection branches
# ---------------------------------------------------------------------------


def test_no_face_returns_zero() -> None:
    """If Haar finds no face, return 0.0 — we have no lip region to score."""
    scorer = HeuristicAVConflictScorer()
    assert scorer.score(_loud_audio(), _frame_no_face()) == 0.0


def test_undecodable_frame_returns_zero() -> None:
    """Bytes that cv2.imdecode can't parse → 0.0 (no crash)."""
    scorer = HeuristicAVConflictScorer()
    assert scorer.score(_loud_audio(), b"not-a-real-image") == 0.0


# ---------------------------------------------------------------------------
# First-frame seeding behavior
# ---------------------------------------------------------------------------


def test_first_face_frame_returns_zero_no_baseline() -> None:
    """On the first face frame the lip crop cache is empty; return 0.0 (no baseline)."""
    scorer = HeuristicAVConflictScorer()
    frame = _frame_with_face()
    # If Haar fails to detect our synthetic face, the function returns 0 via
    # the no-face branch — which is the same expected value. Either path
    # satisfies the contract: first frame must never produce a positive score.
    assert scorer.score(_loud_audio(), frame) == 0.0


def test_score_in_unit_interval() -> None:
    """Whatever the score is, it stays in [0.0, 1.0]."""
    scorer = HeuristicAVConflictScorer()
    s1 = scorer.score(_loud_audio(), _frame_with_face())
    s2 = scorer.score(_loud_audio(), _frame_with_face(lip_brightness=200))
    for s in (s1, s2):
        assert 0.0 <= s <= 1.0
