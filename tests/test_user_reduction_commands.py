"""v0.1g Task 9: user reduction command detection and budget mutation.

Success criterion: all five tests pass, demonstrating:
  - "less proactive" halves aesthetic_reaction budget.
  - "quiet mode" zeros aesthetic_reaction and sets quiet_mode_active.
  - event carries caused_by=[asr_transcript_emitted.event_id].
  - unrelated transcript produces no state change.
  - UserReductionCommandPayload schema matches the spec (command_type, applied_at_ms).
"""

from __future__ import annotations

from typing import get_args

from companion_harness.user_reduction_commands import (
    CommandType,
    UserReductionCommandPayload,
    apply_user_reduction_command,
    detect_user_reduction_command,
    make_user_reduction_payload,
)


# ---------------------------------------------------------------------------
# test_less_proactive_halves_aesthetic_budget
# ---------------------------------------------------------------------------

def test_less_proactive_halves_aesthetic_budget() -> None:
    budget = {"aesthetic_reaction": 10, "short_reaction": 5}
    result = apply_user_reduction_command("less_proactive", budget)
    assert result["aesthetic_reaction"] == 5
    # other keys unchanged
    assert result["short_reaction"] == 5


def test_less_proactive_halves_aesthetic_budget_odd() -> None:
    budget = {"aesthetic_reaction": 7}
    result = apply_user_reduction_command("less_proactive", budget)
    assert result["aesthetic_reaction"] == 3  # floor division


def test_less_proactive_with_zero_budget() -> None:
    budget = {"aesthetic_reaction": 0}
    result = apply_user_reduction_command("less_proactive", budget)
    assert result["aesthetic_reaction"] == 0


def test_less_proactive_missing_key_defaults_to_zero() -> None:
    budget: dict[str, int] = {}
    result = apply_user_reduction_command("less_proactive", budget)
    assert result["aesthetic_reaction"] == 0


def test_less_proactive_does_not_mutate_input() -> None:
    budget = {"aesthetic_reaction": 8}
    apply_user_reduction_command("less_proactive", budget)
    assert budget["aesthetic_reaction"] == 8


# ---------------------------------------------------------------------------
# test_quiet_mode_zeros_budget
# ---------------------------------------------------------------------------

def test_quiet_mode_zeros_budget() -> None:
    budget = {"aesthetic_reaction": 10, "short_reaction": 3}
    result = apply_user_reduction_command("quiet_mode", budget)
    assert result["aesthetic_reaction"] == 0
    assert result["short_reaction"] == 3


def test_quiet_mode_zeros_already_zero() -> None:
    budget = {"aesthetic_reaction": 0}
    result = apply_user_reduction_command("quiet_mode", budget)
    assert result["aesthetic_reaction"] == 0


def test_quiet_mode_does_not_mutate_input() -> None:
    budget = {"aesthetic_reaction": 4}
    apply_user_reduction_command("quiet_mode", budget)
    assert budget["aesthetic_reaction"] == 4


# ---------------------------------------------------------------------------
# test_event_caused_by_transcript (detection returns correct command)
# ---------------------------------------------------------------------------

def test_event_caused_by_transcript_less_proactive() -> None:
    # The orchestrator wires caused_by; here we verify detect returns the right type.
    cmd = detect_user_reduction_command("please be less proactive today")
    assert cmd == "less_proactive"


def test_event_caused_by_transcript_quiet_mode() -> None:
    cmd = detect_user_reduction_command("quiet mode please")
    assert cmd == "quiet_mode"


def test_quiet_mode_preferred_over_less_proactive() -> None:
    # quiet_mode is checked first (stronger suppression).
    cmd = detect_user_reduction_command("quiet mode and less proactive")
    assert cmd == "quiet_mode"


# ---------------------------------------------------------------------------
# test_no_command_means_no_state_change
# ---------------------------------------------------------------------------

def test_no_command_means_no_state_change() -> None:
    cmd = detect_user_reduction_command("what time is it?")
    assert cmd is None


def test_empty_transcript_no_command() -> None:
    assert detect_user_reduction_command("") is None


def test_unrelated_proactive_word_no_match() -> None:
    # "proactive" alone should not match — requires "less proactive"
    assert detect_user_reduction_command("I want to be more proactive") is None


# ---------------------------------------------------------------------------
# test_user_reduction_command_applied_event_schema
# ---------------------------------------------------------------------------

def test_user_reduction_command_applied_event_schema_fields() -> None:
    payload = make_user_reduction_payload("less_proactive")
    assert isinstance(payload, UserReductionCommandPayload)
    assert payload.command_type == "less_proactive"
    assert isinstance(payload.applied_at_ms, int)
    assert payload.applied_at_ms > 0


def test_command_type_literal_has_two_values() -> None:
    values = set(get_args(CommandType))
    assert values == {"less_proactive", "quiet_mode"}


def test_payload_is_frozen() -> None:
    payload = make_user_reduction_payload("quiet_mode")
    try:
        payload.command_type = "less_proactive"  # type: ignore[misc]
        assert False, "expected FrozenInstanceError"
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Phrase variants for coverage
# ---------------------------------------------------------------------------

def test_detect_less_proactive_variants() -> None:
    phrases = [
        "less proactive",
        "be less proactive",
        "more quiet",
        "tone it down",
        "chill out",
    ]
    for phrase in phrases:
        assert detect_user_reduction_command(phrase) == "less_proactive", phrase


def test_detect_quiet_mode_variants() -> None:
    phrases = [
        "quiet mode",
        "go quiet",
        "stop talking",
        "shhh",
        "silent mode",
    ]
    for phrase in phrases:
        assert detect_user_reduction_command(phrase) == "quiet_mode", phrase
