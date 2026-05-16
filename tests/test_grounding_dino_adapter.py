"""Targeted tests for GroundingDINOAdapter (issue #172).

Uses the real IDEA-Research/grounding-dino-tiny model (~700MB); first run
downloads weights. CPU-only by default to avoid contending with realtime
GPU jobs on the shared b200 host.
"""

from __future__ import annotations

import io

from PIL import Image

from companion_harness.grounding_dino_adapter import GroundingDINOAdapter
from companion_harness.vision_sidecar import GroundingModel


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _solid_color_png(size: tuple[int, int] = (64, 64), color: tuple[int, int, int] = (200, 50, 50)) -> bytes:
    return _png_bytes(Image.new("RGB", size, color))


def _noise_png(size: tuple[int, int] = (64, 64)) -> bytes:
    import random

    rnd = random.Random(0)
    img = Image.new("RGB", size)
    pixels = [(rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255)) for _ in range(size[0] * size[1])]
    img.putdata(pixels)
    return _png_bytes(img)


def test_grounding_dino_empty_query_returns_zero() -> None:
    # No model load: empty query short-circuits before _ensure_loaded.
    adapter = GroundingDINOAdapter()
    label, score = adapter(_solid_color_png(), "")
    assert label == ""
    assert score == 0.0
    # Confirm model was not loaded.
    assert adapter._model is None
    assert adapter._processor is None


def test_grounding_dino_satisfies_protocol() -> None:
    adapter = GroundingDINOAdapter()
    assert isinstance(adapter, GroundingModel)


def test_grounding_dino_returns_score_for_query() -> None:
    # Real model on CPU: detect on a tiny synthetic image. We don't assert
    # a high confidence (synthetic blobs are not realistic photos) — only
    # that the call returns a well-formed (str, float) pair in [0.0, 1.0].
    adapter = GroundingDINOAdapter()
    label, score = adapter(_solid_color_png(), "red square")
    assert isinstance(label, str)
    assert isinstance(score, float)
    assert 0.0 <= score <= 1.0


def test_grounding_dino_handles_no_detection() -> None:
    # Force the "no detection" branch deterministically by setting thresholds
    # above 1.0 — every candidate is filtered out and the adapter must return
    # the empty sentinel ("", 0.0). This exercises the post-processing
    # empty-results path without depending on model behavior on synthetic
    # images (which the model often labels eagerly).
    adapter = GroundingDINOAdapter(box_threshold=1.01, text_threshold=1.01)
    label, score = adapter(_noise_png(), "spaceship")
    assert label == ""
    assert score == 0.0
