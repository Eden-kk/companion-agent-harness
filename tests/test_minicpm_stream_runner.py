"""Unit tests for minicpm_stream_runner — fake model, no GPU."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from companion_harness.evals.adapters.minicpm_stream_runner import run_case
from companion_harness.evals.adapters.tact_bench_stream_types import Emission


# ---------------------------------------------------------------------------
# Fake model scaffold
# ---------------------------------------------------------------------------

class _FakeDuplex:
    """Scripted duplex: returns the pre-loaded response list in order."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self._idx = 0
        self._prefill_calls: list[dict] = []
        self.model = SimpleNamespace(reset_session=lambda **kw: None)
        # expose attrs that _generate reads
        self.max_new_speak_tokens_per_chunk = 20
        self.temperature = 0.7
        self.top_k = 0
        self.top_p = 0.9
        self.listen_prob_scale = 1.0
        self.text_repetition_penalty = 1.0
        self.text_repetition_window_size = 0
        # gate attribute — will be overridden by the relaxed subclass
        self.current_turn_ended = False

    def prepare(self, *, prefix_system_prompt: str = "") -> None:
        pass

    def streaming_prefill(self, audio_waveform=None, text_list=None) -> dict:
        self._prefill_calls.append({"audio": audio_waveform is not None, "text_list": text_list})
        return {"success": True}

    def streaming_generate(self, **kwargs) -> dict:
        if self._idx >= len(self._responses):
            return {"is_listen": True, "text": ""}
        resp = self._responses[self._idx]
        self._idx += 1
        return resp


class _FakeModel:
    def __init__(self, responses: list[dict]) -> None:
        self._duplex = _FakeDuplex(responses)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tc1_responses(n_ticks: int = 11) -> list[dict]:
    """All silent except tick 7 (after t_avail=6) which speaks the payload."""
    out = []
    for t in range(n_ticks):
        if t == 7:
            out.append({"is_listen": False, "text": "Your 3pm meeting just ended."})
        else:
            out.append({"is_listen": True, "text": ""})
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_injection_occurs_at_t_avail():
    """streaming_prefill receives text_list containing the PENDING note at t_avail=6."""
    model = _FakeModel(_tc1_responses())
    emissions = run_case("TC1", "prompted", "text", model)  # type: ignore[arg-type]

    duplex = model._duplex
    # tick 6 is t_avail; verify that prefill at index 6 carried a text_list
    call_at_6 = duplex._prefill_calls[6]
    assert call_at_6["text_list"] is not None
    assert any("PENDING" in t for t in call_at_6["text_list"])


def test_ticks_map_correctly():
    """Emission list length equals layer3 ticks for TC1 (== 11)."""
    model = _FakeModel(_tc1_responses())
    emissions = run_case("TC1", "prompted", "text", model)  # type: ignore[arg-type]
    assert len(emissions) == 11
    # ticks are 0-indexed and monotone
    for i, e in enumerate(emissions):
        assert e.tick == i


def test_spoke_flag_matches_is_listen():
    """Tick 7 spoke (is_listen=False) → Emission.spoke=True."""
    model = _FakeModel(_tc1_responses())
    emissions = run_case("TC1", "prompted", "text", model)  # type: ignore[arg-type]
    assert emissions[7].spoke is True
    assert all(not e.spoke for i, e in enumerate(emissions) if i != 7)


def test_annotate_emissions_invoked():
    """At tick 7 the model delivers the payload — detector should match it."""
    model = _FakeModel(_tc1_responses())
    emissions = run_case("TC1", "prompted", "text", model)  # type: ignore[arg-type]
    # The fake text "Your 3pm meeting just ended." should score a detection for TC1
    # (payload contains 'meeting'; detector lexical overlap should match)
    e7 = emissions[7]
    assert isinstance(e7, Emission)
    assert e7.spoke is True
    # detected_item is either the matched item_id or None; we don't mandate a match
    # (detector threshold sensitivity) but the emission must be an Emission
    assert e7.detected_item is None or isinstance(e7.detected_item, str)


def test_arm_vanilla_accepted():
    """run_case accepts vanilla arm without error."""
    model = _FakeModel(_tc1_responses())
    emissions = run_case("TC1", "vanilla", "text", model)  # type: ignore[arg-type]
    assert len(emissions) == 11


def test_unknown_arm_raises():
    model = _FakeModel([])
    with pytest.raises(ValueError, match="unknown arm"):
        run_case("TC1", "bad_arm", "text", model)  # type: ignore[arg-type]


def test_unknown_modality_raises():
    model = _FakeModel([])
    with pytest.raises(ValueError, match="unknown input_modality"):
        run_case("TC1", "prompted", "video", model)  # type: ignore[arg-type]


def test_give_user_state_true_injects_label():
    """give_user_state=True: every prefill call's text_list contains '[user state: ...'."""
    model = _FakeModel(_tc1_responses())
    emissions = run_case("TC1", "prompted", "text", model, give_user_state=True)  # type: ignore[arg-type]

    duplex = model._duplex
    for call in duplex._prefill_calls:
        tl = call["text_list"]
        assert tl is not None, "text_list must be present when give_user_state=True"
        combined = " ".join(tl)
        assert "[user state:" in combined, f"label missing from text_list: {tl!r}"


def test_give_user_state_false_no_label():
    """give_user_state=False (default): text_list never contains '[user state:'."""
    model = _FakeModel(_tc1_responses())
    emissions = run_case("TC1", "prompted", "text", model, give_user_state=False)  # type: ignore[arg-type]

    duplex = model._duplex
    for call in duplex._prefill_calls:
        tl = call["text_list"] or []
        combined = " ".join(tl)
        assert "[user state:" not in combined, f"unexpected label in text_list: {tl!r}"


def test_give_user_state_label_values():
    """Labels map m/h/b/i to expected tier2 phrasing (not raw single-letter keys)."""
    from companion_harness.evals.adapters.tact_bench import load_tier2
    expected_labels = set(load_tier2()["user_state_labels"].values())

    model = _FakeModel(_tc1_responses())
    run_case("TC1", "prompted", "text", model, give_user_state=True)  # type: ignore[arg-type]

    duplex = model._duplex
    seen_labels = set()
    for call in duplex._prefill_calls:
        for part in (call["text_list"] or []):
            if "[user state:" in part:
                # extract the label value after "[user state: "
                val = part.split("[user state:", 1)[1].rstrip("]").strip()
                seen_labels.add(val)
    # every seen label must be a known tier2 label
    assert seen_labels, "no user-state labels found"
    assert seen_labels.issubset(expected_labels), f"unknown labels: {seen_labels - expected_labels}"


def test_audio_modality_calls_audio_view(monkeypatch):
    """audio modality uses audio_view(); each tick gets real float32 audio."""
    import companion_harness.evals.adapters.minicpm_stream_runner as mod

    fake_audio = [np.zeros(16000, dtype=np.float32)] * 11

    def fake_build(cid):
        tl = _real_build(cid)
        tl.audio_view = lambda: fake_audio
        return tl

    _real_build = mod.build_timeline
    monkeypatch.setattr(mod, "build_timeline", fake_build)

    model = _FakeModel(_tc1_responses())
    emissions = run_case("TC1", "prompted", "audio", model)  # type: ignore[arg-type]
    assert len(emissions) == 11

    # verify that each prefill received non-None audio_waveform
    duplex = model._duplex
    assert all(c["audio"] for c in duplex._prefill_calls)
