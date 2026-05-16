"""Targeted tests for SentenceTransformerEmbedder (issue #183).

Uses the real ~90MB MiniLM-L6-v2 model; first run downloads weights.
"""

from __future__ import annotations

from companion_harness.embedder_sentence_transformer import (
    SentenceTransformerEmbedder,
)
from companion_harness.memory_manager import EmbeddingAdapter


def test_embedder_returns_384_dims() -> None:
    embedder = SentenceTransformerEmbedder()
    vec = embedder.embed("hello world")
    assert isinstance(vec, list)
    assert len(vec) == 384
    assert all(isinstance(x, float) for x in vec)


def test_embedder_similar_texts_have_high_similarity() -> None:
    embedder = SentenceTransformerEmbedder()
    a = embedder.embed("The cat sat on the mat.")
    b = embedder.embed("A cat is sitting on a mat.")
    c = embedder.embed("Quantum field theory describes subatomic particles.")
    # vectors are normalized → dot product is cosine similarity
    sim_ab = sum(x * y for x, y in zip(a, b))
    sim_ac = sum(x * y for x, y in zip(a, c))
    assert sim_ab > sim_ac
    assert sim_ab > 0.5


def test_embedder_satisfies_protocol() -> None:
    embedder = SentenceTransformerEmbedder()
    assert isinstance(embedder, EmbeddingAdapter)


def test_embedder_handles_empty_string() -> None:
    embedder = SentenceTransformerEmbedder()
    vec = embedder.embed("")
    assert isinstance(vec, list)
    assert len(vec) == 384
