"""SentenceTransformerEmbedder — real EmbeddingAdapter impl (issue #183).

Uses sentence-transformers/all-MiniLM-L6-v2 (~90MB, CPU-friendly, 384-dim).
Embeddings are L2-normalized so cosine == dot product.

Lazy-loads the model on first embed() call. Defaults to CPU (the model is
small enough that CPU is fine and avoids contending for GPU memory with the
realtime audio/vision models on the shared b200 host); pass device="cuda"
explicitly to override.
"""

from __future__ import annotations

from companion_harness.memory_manager import EmbeddingAdapter

__all__ = ["SentenceTransformerEmbedder"]


class SentenceTransformerEmbedder(EmbeddingAdapter):
    def __init__(
        self,
        model_id: str = "sentence-transformers/all-MiniLM-L6-v2",
        device: str = "cpu",
    ) -> None:
        self._model_id = model_id
        self._device = device
        self._model = None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(self._model_id, device=self._device)

    def embed(self, text: str) -> list[float]:
        self._ensure_loaded()
        vec = self._model.encode(text, normalize_embeddings=True)
        return vec.tolist()
