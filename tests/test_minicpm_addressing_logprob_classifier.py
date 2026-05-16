"""Calibration test for MiniCPMStreamingModel.classify_yes_no() + MiniCPMAddressingClassifierImpl.

Requires real MiniCPM weights on b200.  Gate: @pytest.mark.gpu.

Fixture corpus: 20 manually-verified (transcript, expected_addressing_bool) pairs.
  - 10 clear "yes" (user addressing the AI assistant)
  - 10 clear "no" (user talking to someone else / non-conversational)

Asserts accuracy >= 80% at threshold 0.5.
Prints per-case (prob_yes, expected, actual, correct) for operator review.
"""

from __future__ import annotations

import pytest

torch = pytest.importorskip("torch", reason="torch not available — b200 venv required")

if not torch.cuda.is_available():
    pytest.skip("CUDA not available — b200 required", allow_module_level=True)


_FIXTURE_CORPUS: list[tuple[str, bool]] = [
    # --- yes: user is addressing the AI assistant ---
    ("hey assistant, what's the weather like today?", True),
    ("can you help me with this math problem?", True),
    ("what time is it right now?", True),
    ("assistant, set a timer for ten minutes", True),
    ("play some relaxing music for me", True),
    ("can you summarize this article?", True),
    ("translate this sentence to French", True),
    ("what's the capital of France?", True),
    ("remind me to call mom tomorrow morning", True),
    ("tell me a joke", True),
    # --- no: user is not addressing the AI assistant ---
    ("did you see the game last night?", False),
    ("ugh I'm so tired today", False),
    ("I can't believe how hot it is outside", False),
    ("she said she would call back later", False),
    ("the meeting is at three o'clock", False),
    ("I need to pick up groceries on the way home", False),
    ("this coffee is way too strong", False),
    ("the kids have soccer practice today", False),
    ("I wonder if it's going to rain", False),
    ("let's grab lunch at that new place downtown", False),
]


@pytest.mark.gpu
def test_logprob_addressing_classifier_calibration() -> None:
    """classify_yes_no() achieves >= 80% accuracy on held-out fixture corpus."""
    from companion_harness.addressing_classifier import MiniCPMAddressingClassifierImpl
    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel

    model = MiniCPMStreamingModel()
    clf = MiniCPMAddressingClassifierImpl(model)

    correct = 0
    total = len(_FIXTURE_CORPUS)
    print()
    print(f"{'transcript':<55} {'prob_yes':>8} {'expected':>8} {'actual':>6} {'ok':>3}")
    print("-" * 85)
    for transcript, expected in _FIXTURE_CORPUS:
        is_yes, prob_yes = model.classify_yes_no(
            f"Transcript: '{transcript}'. "
            "Is the user addressing an AI assistant? "
            "Answer with only 'yes' or 'no'."
        )
        ok = is_yes == expected
        if ok:
            correct += 1
        truncated = transcript[:52] + "..." if len(transcript) > 55 else transcript
        print(
            f"{truncated:<55} {prob_yes:>8.3f} {str(expected):>8} {str(is_yes):>6} {'Y' if ok else 'N':>3}"
        )

    accuracy = correct / total
    print()
    print(f"Accuracy: {correct}/{total} = {accuracy:.1%}")
    assert accuracy >= 0.80, (
        f"classify_yes_no calibration: {accuracy:.1%} accuracy on fixture corpus "
        f"({correct}/{total} correct). Minimum required: 80%."
    )
