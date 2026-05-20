"""Tier-B determinism for the continuous gate (PR2, invariant #5).

decide_chunk over a recorded sequence of PerChunkPolicyInputs must be
bit-identical across two runs (no wall-clock, no random, no dict iteration).
DecisionTrace production + live-signal construction are wired in the
orchestrator at PR3; PR2 gates the pure-function determinism of the decision.
"""

from __future__ import annotations

import dataclasses

from companion_harness.continuous_speak_policy import decide_chunk
from companion_harness.schemas import PerChunkPolicyInputs


def _base(**over) -> PerChunkPolicyInputs:
    kw = dict(
        chunk_index=0,
        model_is_listen=False,
        backchannel_score=0.0,
        user_addressed_agent=True,
        privacy_mode="normal",
        social_mode="user_addressing_agent",
    )
    kw.update(over)
    return PerChunkPolicyInputs(**kw)


def _seq() -> list[PerChunkPolicyInputs]:
    return [
        _base(chunk_index=0, model_is_listen=True),                                  # listen → silence
        _base(chunk_index=1, model_is_listen=False, user_addressed_agent=True),      # speak+addressed → full_response
        _base(chunk_index=2, model_is_listen=False, backchannel_score=0.9),          # → backchannel
        _base(chunk_index=3, model_is_listen=False, social_mode="group_conversation"),  # social block → silence
        _base(chunk_index=4, model_is_listen=False, privacy_mode="sensitive_conversation"),  # privacy block → silence
        _base(chunk_index=5, model_is_listen=False, user_addressed_agent=False),     # not addressed → silence
        _base(chunk_index=6, model_is_listen=False, budget_full_response_remaining=0),  # budget exhausted → silence
    ]


def test_policy_replay_exact_continuous() -> None:
    seq = _seq()
    run1 = [decide_chunk(i, caused_by_evt_id=f"e{i.chunk_index}") for i in seq]
    run2 = [decide_chunk(i, caused_by_evt_id=f"e{i.chunk_index}") for i in seq]
    # Bit-identical Tier-B replay = dataclass equality field-by-field.
    assert run1 == run2

    actions = [d.action_type for d in run1]
    # Non-trivial: multiple distinct branches are exercised (not all silence).
    assert actions == [
        "silence", "full_response", "backchannel",
        "silence", "silence", "silence", "silence",
    ]
    reasons = {d.primary_reason_code.name for d in run1}
    assert "PRIVACY_MODE_BLOCKED" in reasons
    assert "COOLDOWN_BLOCKED" in reasons


def test_perchunk_policy_inputs_has_no_dict_or_set_fields() -> None:
    """Determinism guard: the replay signal must be scalar/enum only (invariant #5)."""
    for f in dataclasses.fields(PerChunkPolicyInputs):
        t = str(f.type).lower()
        assert "dict" not in t and "set" not in t, f"{f.name} is non-scalar ({f.type})"


def test_caused_by_propagates() -> None:
    d = decide_chunk(_base(chunk_index=9), caused_by_evt_id="chunk-evt-9")
    assert d.caused_by == ["chunk-evt-9"]
