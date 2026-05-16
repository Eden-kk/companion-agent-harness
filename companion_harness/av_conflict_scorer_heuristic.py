"""HeuristicAVConflictScorer — VAD-presence x lip-region pixel-diff (closes #168 / RFC #236).

Per RFC #236 recommendation: start with a cheap, interpretable heuristic
(VAD-says-speech + lip-not-moving → conflict) before reaching for AudioCLIP.
The Protocol seam is unchanged; this slots in as a drop-in replacement for
`_NullAudioVisualConflictScorer` wherever a real signal is wanted.

Algorithm
---------
1. No frame, or empty audio → 0.0 (no signal).
2. Audio RMS over 16-bit PCM. If RMS < 800 → 0.0 (no speech, nothing to conflict).
3. Decode `frame_bytes` (encoded image bytes) to grayscale via cv2.imdecode.
4. Detect face via OpenCV Haar cascade. No face → 0.0.
5. Crop lip region (bottom third of the face bbox).
6. Compare against cached previous lip crop. Shape mismatch / first frame → cache, 0.0.
7. lip_motion_norm = min(1.0, mean_abs_diff / 30.0).
   conflict_score = 1.0 - lip_motion_norm, clipped to [0.0, 1.0].

No heavy model — only OpenCV Haar (~100KB, CPU). Conforms to
AudioVisualConflictScorer Protocol; safe to inject anywhere the null stub is used.
"""

from __future__ import annotations

import numpy as np

__all__ = ["HeuristicAVConflictScorer"]

_RMS_SPEECH_THRESHOLD: float = 800.0
_LIP_MOTION_DENOMINATOR: float = 30.0


class HeuristicAVConflictScorer:
    """VAD-presence x lip-region pixel-diff heuristic for AV conflict.

    See module docstring for the scoring pipeline.
    """

    def __init__(self) -> None:
        # Lazy-import cv2 so a torchless / cv2-less environment doesn't fail
        # at module import time. The class is only constructed where the
        # heuristic is actually wanted.
        import cv2

        self._cv2 = cv2
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        self._face_cascade = cv2.CascadeClassifier(cascade_path)
        self._last_lip_crop: np.ndarray | None = None

    def score(self, audio: bytes, frame: bytes | None) -> float:
        if frame is None or len(frame) == 0:
            return 0.0
        if len(audio) == 0:
            return 0.0

        samples = np.frombuffer(audio, dtype=np.int16)
        if samples.size == 0:
            return 0.0
        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
        if rms < _RMS_SPEECH_THRESHOLD:
            return 0.0

        cv2 = self._cv2
        buf = np.frombuffer(frame, dtype=np.uint8)
        gray = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
        if gray is None:
            return 0.0

        faces = self._face_cascade.detectMultiScale(gray, scaleFactor=1.3, minNeighbors=5)
        if len(faces) == 0:
            return 0.0

        x, y, w, h = faces[0]
        lip_y_start = y + (h * 2 // 3)
        lip_crop = gray[lip_y_start : y + h, x : x + w]
        if lip_crop.size == 0:
            return 0.0

        if self._last_lip_crop is None or self._last_lip_crop.shape != lip_crop.shape:
            self._last_lip_crop = lip_crop.copy()
            return 0.0

        diff = np.abs(lip_crop.astype(np.int16) - self._last_lip_crop.astype(np.int16))
        lip_diff = float(np.mean(diff))
        self._last_lip_crop = lip_crop.copy()

        lip_motion_norm = min(1.0, lip_diff / _LIP_MOTION_DENOMINATOR)
        conflict_score = 1.0 - lip_motion_norm
        return float(max(0.0, min(1.0, conflict_score)))
