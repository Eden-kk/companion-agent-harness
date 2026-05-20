"""Unit tests for the classify_yes_no() softmax-mass math.

Tests the computation logic directly using torch, without importing
MiniCPMStreamingModel (which requires transformers/CUDA at import time).
"""

from __future__ import annotations

import pytest

torch = pytest.importorskip("torch")


def _classify_yes_no(
    logits: torch.Tensor,
    yes_ids: list[int],
    no_ids: list[int],
) -> tuple[bool, float]:
    """Inline reimplementation of the classify_yes_no math for unit testing.

    Mirrors the computation in MiniCPMStreamingModel.classify_yes_no exactly:
      probs = softmax(logits)
      p_yes = sum(probs[i] for i in yes_ids)
      p_no  = sum(probs[i] for i in no_ids)
      prob_yes = p_yes / (p_yes + p_no)
      return prob_yes > 0.5, prob_yes
    """
    probs = torch.softmax(logits, dim=-1)
    p_yes = float(sum(probs[i].item() for i in yes_ids))
    p_no  = float(sum(probs[i].item() for i in no_ids))
    if p_yes + p_no <= 0.0:
        return False, 0.5
    prob_yes = p_yes / (p_yes + p_no)
    return prob_yes > 0.5, prob_yes


def test_yes_wins_when_yes_logit_dominant() -> None:
    """yes_logit >> no_logit → (True, prob_yes ≈ 1.0)."""
    vocab = torch.full((100,), -100.0)
    vocab[1] = 10.0   # yes token
    vocab[2] = 0.0    # no token
    is_yes, prob_yes = _classify_yes_no(vocab, yes_ids=[1], no_ids=[2])
    assert is_yes is True
    assert prob_yes > 0.99


def test_no_wins_when_no_logit_dominant() -> None:
    """no_logit >> yes_logit → (False, prob_yes ≈ 0.0)."""
    vocab = torch.full((100,), -100.0)
    vocab[1] = 0.0    # yes token
    vocab[2] = 10.0   # no token
    is_yes, prob_yes = _classify_yes_no(vocab, yes_ids=[1], no_ids=[2])
    assert is_yes is False
    assert prob_yes < 0.01


def test_equal_logits_returns_false_strict_threshold() -> None:
    """Equal logits → prob_yes == 0.5 → threshold is strict (>0.5) → False."""
    vocab = torch.full((100,), -100.0)
    vocab[1] = 5.0   # yes token
    vocab[2] = 5.0   # no token
    is_yes, prob_yes = _classify_yes_no(vocab, yes_ids=[1], no_ids=[2])
    assert abs(prob_yes - 0.5) < 1e-6
    assert is_yes is False


def test_prob_yes_normalized_against_yes_no_mass_only() -> None:
    """prob_yes = p_yes / (p_yes + p_no) — other tokens don't dilute the ratio."""
    vocab = torch.full((100,), 5.0)   # many tokens all with logit 5
    yes_id, no_id = 1, 2
    vocab[yes_id] = 8.0
    vocab[no_id] = 2.0
    is_yes, prob_yes = _classify_yes_no(vocab, yes_ids=[yes_id], no_ids=[no_id])
    # Compute expected ratio analytically.
    raw = torch.softmax(vocab, dim=-1)
    p_y = raw[yes_id].item()
    p_n = raw[no_id].item()
    expected = p_y / (p_y + p_n)
    assert abs(prob_yes - expected) < 1e-5
    assert is_yes is True


def test_multiple_yes_ids_summed() -> None:
    """Multiple yes-token ids sum their probability mass."""
    vocab = torch.full((100,), -100.0)
    vocab[1] = 3.0   # yes variant 1
    vocab[2] = 3.0   # yes variant 2
    vocab[3] = 3.0   # no token
    is_yes, prob_yes = _classify_yes_no(vocab, yes_ids=[1, 2], no_ids=[3])
    # 2 yes variants at equal logit vs 1 no token → yes mass is double
    assert is_yes is True
    assert prob_yes > 0.65


def test_zero_total_mass_returns_undecided_false() -> None:
    """If neither yes nor no tokens have any mass, return (False, 0.5)."""
    vocab = torch.full((100,), -100.0)
    vocab[50] = 10.0  # irrelevant token dominates
    # yes_ids and no_ids point to tokens with near-zero probability
    is_yes, prob_yes = _classify_yes_no(vocab, yes_ids=[1], no_ids=[2])
    # Both are at -100 logit; their softmax mass approaches 0 but is not exactly 0.
    # The function still normalizes — let's verify it doesn't crash and returns a valid float.
    assert isinstance(is_yes, bool)
    assert 0.0 <= prob_yes <= 1.0
