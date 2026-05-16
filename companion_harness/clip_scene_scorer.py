"""CLIPSceneChangeScorer — real SceneScorer impl (issue #166).

Uses openai/clip-vit-base-patch32 (~600MB) image encoder. Scene-change score is
1 - cosine_similarity(clip_embed(curr_frame), clip_embed(prev_frame)), clipped
to [0.0, 1.0].

Lazy-loads the model on first __call__. Defaults to CPU to avoid contending
for GPU memory with the realtime audio/vision models on the shared b200 host;
pass device="cuda" explicitly to override.

Stateless to match the `SceneScorer` Protocol: callers pass both frames each
time. (VisionSidecar maintains the prev-frame reference; we re-encode both
frames per call. If profiling shows this matters, add a tiny LRU keyed on the
prev_frame bytes hash.)
"""

from __future__ import annotations

import io

from companion_harness.vision_sidecar import SceneScorer

__all__ = ["CLIPSceneChangeScorer"]


class CLIPSceneChangeScorer(SceneScorer):
    def __init__(
        self,
        model_id: str = "openai/clip-vit-base-patch32",
        device: str = "cpu",
    ) -> None:
        self._model_id = model_id
        self._device = device
        self._model = None
        self._processor = None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        from transformers import CLIPModel, CLIPProcessor

        self._model = CLIPModel.from_pretrained(self._model_id).eval().to(self._device)
        self._processor = CLIPProcessor.from_pretrained(self._model_id)

    def _embed(self, frame_bytes: bytes):
        import torch
        from PIL import Image

        img = Image.open(io.BytesIO(frame_bytes)).convert("RGB")
        inputs = self._processor(images=img, return_tensors="pt").to(self._device)
        with torch.no_grad():
            emb = self._model.get_image_features(**inputs)
            emb = emb / emb.norm(dim=-1, keepdim=True)
        return emb

    def __call__(self, prev_frame: bytes, curr_frame: bytes) -> float:
        self._ensure_loaded()
        prev_emb = self._embed(prev_frame)
        curr_emb = self._embed(curr_frame)
        sim = (prev_emb * curr_emb).sum(dim=-1).item()
        return max(0.0, min(1.0, 1.0 - sim))
