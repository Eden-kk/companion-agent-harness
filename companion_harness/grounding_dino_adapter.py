"""GroundingDINOAdapter — real GroundingModel impl (issue #172).

Uses IDEA-Research/grounding-dino-tiny (~700MB) for open-vocabulary
zero-shot object detection. Given a frame (PNG/JPEG bytes) and a free-form
query string, returns (label, max_confidence). Empty query short-circuits
to ("", 0.0) without loading the model.

Lazy-loads on first call. Defaults to CPU to avoid contending for GPU
memory with the realtime audio/vision models on the shared b200 host;
pass device="cuda" explicitly to override.

Matches the `GroundingModel` Protocol in companion_harness/vision_sidecar.py
exactly: `__call__(frame: bytes, query: str) -> tuple[str, float]`.
"""

from __future__ import annotations

import io

from companion_harness.vision_sidecar import GroundingModel

__all__ = ["GroundingDINOAdapter"]


class GroundingDINOAdapter(GroundingModel):
    def __init__(
        self,
        model_id: str = "IDEA-Research/grounding-dino-tiny",
        device: str = "cpu",
        box_threshold: float = 0.25,
        text_threshold: float = 0.25,
    ) -> None:
        self._model_id = model_id
        self._device = device
        self._box_threshold = box_threshold
        self._text_threshold = text_threshold
        self._model = None
        self._processor = None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

        self._processor = AutoProcessor.from_pretrained(self._model_id)
        self._model = (
            AutoModelForZeroShotObjectDetection.from_pretrained(self._model_id)
            .eval()
            .to(self._device)
        )

    def __call__(self, frame: bytes, query: str) -> tuple[str, float]:
        if not query:
            return "", 0.0

        import torch
        from PIL import Image

        self._ensure_loaded()

        img = Image.open(io.BytesIO(frame)).convert("RGB")
        # GroundingDINO expects lowercase queries terminated with a period.
        text_query = query.strip().lower()
        if not text_query.endswith("."):
            text_query = text_query + "."

        inputs = self._processor(images=img, text=text_query, return_tensors="pt").to(
            self._device
        )
        with torch.no_grad():
            outputs = self._model(**inputs)

        results = self._processor.post_process_grounded_object_detection(
            outputs,
            inputs["input_ids"],
            box_threshold=self._box_threshold,
            text_threshold=self._text_threshold,
            target_sizes=[img.size[::-1]],
        )
        if not results or len(results[0]["scores"]) == 0:
            return "", 0.0

        scores = results[0]["scores"]
        # transformers>=4.51 deprecates "labels" string-name semantics in
        # favor of "text_labels"; prefer the new key when present.
        labels = results[0].get("text_labels") or results[0].get("labels") or []
        idx = int(scores.argmax().item())
        confidence = float(scores[idx].item())
        label = str(labels[idx]) if idx < len(labels) else query
        return label, confidence
