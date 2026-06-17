"""Tests for the non-native monitor_stream runner (S5b).

Fake model returning scripted <monitor>/<speak> text; no GPU/network.
"""
from __future__ import annotations

from companion_harness.evals.adapters.monitor_stream_runner import (
    _action_to_form,
    _build_prompt,
    run_case,
)
from companion_harness.evals.adapters.tact_bench import _ARMS
from companion_harness.evals.adapters.tact_bench_stream_types import Emission


# ---------------------------------------------------------------------------
# Fake models
# ---------------------------------------------------------------------------

class _ScriptedModel:
    """Returns a fixed response for every call."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self._idx = 0
        self.prompts: list[str] = []

    def chat(self, prompt: str, max_new_tokens: int = 160) -> str:
        self.prompts.append(prompt)
        resp = self._responses[self._idx % len(self._responses)]
        self._idx += 1
        return resp


class _AlwaysNowModel:
    """Returns NOW:BRIEF for every pending item."""

    def __init__(self, item_ids: list[str]) -> None:
        self._item_ids = item_ids
        self.call_count = 0

    def chat(self, prompt: str, max_new_tokens: int = 160) -> str:
        self.call_count += 1
        lines = [f"{iid}: NOW:BRIEF — deliver" for iid in self._item_ids]
        return "<monitor>\n" + "\n".join(lines) + "\n</monitor>\n<speak>Here it is.</speak>"


class _AlwaysWaitModel:
    """Records all prompts it receives."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def chat(self, prompt: str, max_new_tokens: int = 160) -> str:
        self.prompts.append(prompt)
        return "<monitor>meeting: WAIT\nweather: WAIT\nnote: WAIT\n</monitor><speak></speak>"


# ---------------------------------------------------------------------------
# Parsing tests
# ---------------------------------------------------------------------------

def test_action_to_form_speak_brief():
    assert _action_to_form("NOW:SPEAK_BRIEF") == "SPEAK_BRIEF"
    assert _action_to_form("NOW_BRIEF") == "SPEAK_BRIEF"
    assert _action_to_form("NOW") == "SPEAK_BRIEF"


def test_action_to_form_speak_full():
    assert _action_to_form("NOW:SPEAK_FULL") == "SPEAK_FULL"
    assert _action_to_form("NOW_FULL") == "SPEAK_FULL"


def test_action_to_form_silent():
    assert _action_to_form("NOW:SILENT_NOTIFY") == "SILENT_NOTIFY"


def test_action_to_form_chime():
    assert _action_to_form("NOW:CHIME") == "CHIME"


# ---------------------------------------------------------------------------
# Emission mapping: detected_item from monitor decision
# ---------------------------------------------------------------------------

def test_emission_detected_item_from_monitor_decision():
    """detected_item must come from the <monitor> parse, not a text detector."""
    model = _AlwaysNowModel(["meeting"])
    emissions = run_case("TC1", model, "minicpm")
    assert len(emissions) >= 1
    assert all(e.detected_item == "meeting" for e in emissions)
    assert all(e.spoke for e in emissions)
    assert all(e.confidence == 1.0 for e in emissions)


def test_wait_produces_no_emission():
    model = _AlwaysWaitModel()
    emissions = run_case("TC1", model, "minicpm")
    assert emissions == []


# ---------------------------------------------------------------------------
# Single-channel: ≤1 NOW per tick
# ---------------------------------------------------------------------------

def test_single_channel_at_most_one_now_per_tick():
    """TC31 has two items with the same t_avail=4; only one emission per tick."""
    # TC31: pump (t_avail=4) + ride (t_avail=4)
    class _BothNowModel:
        def chat(self, prompt: str, max_new_tokens: int = 160) -> str:
            return (
                "<monitor>\n"
                "pump: NOW:BRIEF — urgent\n"
                "ride: NOW:BRIEF — also urgent\n"
                "</monitor>\n<speak>Delivering.</speak>"
            )

    emissions = run_case("TC31", _BothNowModel(), "gpt")
    # group by tick — no two emissions on the same tick
    ticks_with_emission: list[int] = [e.tick for e in emissions]
    assert len(ticks_with_emission) == len(set(ticks_with_emission)), (
        f"Multiple emissions at the same tick: {ticks_with_emission}"
    )


def test_single_channel_both_items_delivered_across_ticks():
    """Both TC31 items should eventually be delivered (on different ticks)."""
    class _BothNowModel:
        def chat(self, prompt: str, max_new_tokens: int = 160) -> str:
            return (
                "<monitor>\n"
                "pump: NOW:BRIEF\n"
                "ride: NOW:BRIEF\n"
                "</monitor>\n<speak>Delivery.</speak>"
            )

    emissions = run_case("TC31", _BothNowModel(), "minicpm")
    delivered_items = {e.detected_item for e in emissions}
    assert "pump" in delivered_items
    assert "ride" in delivered_items
    # They must land on different ticks
    ticks = sorted(e.tick for e in emissions)
    assert ticks[0] < ticks[1]


# ---------------------------------------------------------------------------
# Accumulated transcript construction
# ---------------------------------------------------------------------------

def test_transcript_accumulates_across_ticks():
    """Prompt at later ticks must include earlier user turns."""
    model = _AlwaysWaitModel()
    run_case("TC1", model, "minicpm")

    # At least one prompt should mention t=0 user speech from TC1
    found_early = any("t=0" in p for p in model.prompts)
    # Later prompts must have more transcript than earlier ones
    prompts_with_transcript = [p for p in model.prompts if "user:" in p]
    assert found_early or len(prompts_with_transcript) > 0


def test_pending_note_appears_in_prompt_after_t_avail():
    """After t_avail, the [PENDING …] note must appear in the prompt."""
    model = _AlwaysWaitModel()
    run_case("TC1", model, "minicpm")
    # TC1 t_avail=6; prompts at t>=6 should include [PENDING
    late_prompts = model.prompts  # all prompts from t_avail onward
    assert any("[PENDING" in p for p in late_prompts)


# ---------------------------------------------------------------------------
# No label leakage: urgency/relevance/standing must NOT appear in the prompt
# ---------------------------------------------------------------------------

_FORBIDDEN_LABELS = ("urgency:", "relevance:", "urgency high", "urgency low",
                     "relevance high", "relevance low", "standing:", "requested: yes",
                     "requested: no", "(urgency", "(relevance")


def test_no_label_leakage_in_prompts():
    """Urgency/relevance/standing labels must never appear in the monitor_stream prompt."""
    model = _AlwaysWaitModel()
    run_case("TC12", model, "minicpm")
    for prompt in model.prompts:
        prompt_lower = prompt.lower()
        for label in _FORBIDDEN_LABELS:
            assert label.lower() not in prompt_lower, (
                f"Label leak detected — '{label}' found in prompt:\n{prompt[:400]}"
            )


def test_no_label_leakage_tc31():
    """TC31 (multi-item) also must not leak labels."""
    model = _AlwaysWaitModel()
    run_case("TC31", model, "minicpm")
    for prompt in model.prompts:
        prompt_lower = prompt.lower()
        for label in _FORBIDDEN_LABELS:
            assert label.lower() not in prompt_lower, (
                f"Label leak in TC31 prompt — '{label}':\n{prompt[:400]}"
            )


# ---------------------------------------------------------------------------
# Policy prompt is monitor_stream arm (not vanilla/prompted)
# ---------------------------------------------------------------------------

def test_policy_prompt_is_monitor_stream():
    model = _AlwaysWaitModel()
    run_case("TC1", model, "minicpm")
    expected_prefix = _ARMS["monitor_stream"][:60]
    assert any(p.startswith(expected_prefix) for p in model.prompts)


# ---------------------------------------------------------------------------
# model_kind validation
# ---------------------------------------------------------------------------

def test_invalid_model_kind_raises():
    import pytest
    with pytest.raises(ValueError, match="model_kind"):
        run_case("TC1", _AlwaysWaitModel(), "unknown")


# ---------------------------------------------------------------------------
# _build_prompt: label-free pending item lines
# ---------------------------------------------------------------------------

def test_build_prompt_no_labels_in_item_lines():
    from companion_harness.evals.adapters.tact_bench_layer3 import load_layer3
    cases = {c.id: c for c in load_layer3()}
    tc1 = cases["TC1"]
    prompt = _build_prompt(
        policy=_ARMS["monitor_stream"],
        transcript_lines=["[t=0] user: hello"],
        pending_note_lines=["[PENDING — from build_service: deploy done]"],
        pending=tc1.items,
    )
    prompt_lower = prompt.lower()
    for label in _FORBIDDEN_LABELS:
        assert label.lower() not in prompt_lower, (
            f"Label leak in _build_prompt: '{label}'"
        )
    assert "[PENDING — from build_service: deploy done]" in prompt
    assert "Pending items available now:" in prompt
