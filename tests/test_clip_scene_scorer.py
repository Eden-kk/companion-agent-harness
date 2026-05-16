"""Targeted tests for CLIPSceneChangeScorer (issue #166).

Uses the real ~600MB CLIP-ViT-B-32 model on CPU; first run downloads weights.
"""

from __future__ import annotations

import io

from PIL import Image

from companion_harness.clip_scene_scorer import CLIPSceneChangeScorer
from companion_harness.vision_sidecar import SceneScorer


def _png_bytes(color: tuple[int, int, int], size: tuple[int, int] = (64, 64)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _gradient_png_bytes(size: tuple[int, int] = (64, 64)) -> bytes:
    img = Image.new("RGB", size)
    w, h = size
    pixels = img.load()
    for x in range(w):
        for y in range(h):
            pixels[x, y] = (x * 4 % 256, y * 4 % 256, (x + y) * 2 % 256)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_clip_scene_scorer_satisfies_protocol() -> None:
    scorer = CLIPSceneChangeScorer()
    assert isinstance(scorer, SceneScorer)


def test_clip_scene_scorer_first_frame_returns_zero() -> None:
    # First-frame semantics: VisionSidecar passes prev=curr=first when no prior frame.
    # Score for identical frames should be ~0.0 (clipped, near-zero numerical drift OK).
    scorer = CLIPSceneChangeScorer()
    frame = _png_bytes((128, 64, 192))
    score = scorer(frame, frame)
    assert 0.0 <= score < 0.01


def test_clip_scene_scorer_same_frame_returns_low_score() -> None:
    scorer = CLIPSceneChangeScorer()
    frame = _gradient_png_bytes()
    score = scorer(frame, frame)
    assert 0.0 <= score < 0.01


def test_clip_scene_scorer_different_frame_returns_high_score() -> None:
    scorer = CLIPSceneChangeScorer()
    # Two visually very different frames: solid red vs. gradient pattern.
    frame_a = _png_bytes((255, 0, 0))
    frame_b = _gradient_png_bytes()
    score_diff = scorer(frame_a, frame_b)
    score_same = scorer(frame_a, frame_a)
    assert score_diff > score_same
    assert score_diff > 0.05
    assert score_diff <= 1.0
