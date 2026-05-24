"""Scenario timeline builder contract tests."""

import pytest
import numpy as np

from companion_harness.evals.adapters.scenario_timeline import (
    build_timeline,
    PENDING_TEMPLATE,
)


def test_tc1_length_equals_ticks():
    """Timeline length == layer3 ticks for TC1."""
    tl = build_timeline("TC1")
    assert len(tl.ticks) == 11


def test_tc1_user_turns_land_on_correct_ticks():
    """TC1 user speech at t=0, t=5, t=10 per scenarios.yaml."""
    tl = build_timeline("TC1")
    assert tl.ticks[0].kind == "speech"
    assert tl.ticks[5].kind == "speech"
    assert tl.ticks[10].kind == "speech"


def test_tc1_pending_note_at_t_avail():
    """TC1 item t_avail=6 → pending_note injected at tick 6 with non-empty payload."""
    tl = build_timeline("TC1")
    assert tl.ticks[6].pending_note is not None
    assert "PENDING" in tl.ticks[6].pending_note
    note = tl.ticks[6].pending_note
    # note carries source and payload, not urgency labels
    assert "urgent" not in note.lower()
    assert "high" not in note.lower()
    # payload must not be empty (B3 regression guard)
    assert not note.endswith(": ]"), f"Payload is empty in: {note!r}"


def test_tc1_pending_template_format():
    """PENDING note matches the pinned template."""
    tl = build_timeline("TC1")
    note = tl.ticks[6].pending_note
    assert note is not None
    assert note.startswith("[PENDING")
    assert "from " in note


def test_text_view_length_equals_ticks():
    tl = build_timeline("TC1")
    tv = tl.text_view()
    assert len(tv) == len(tl.ticks)


def test_text_view_none_for_idle_ticks():
    """Idle/pause ticks have None in text_view."""
    tl = build_timeline("TC1")
    tv = tl.text_view()
    # tick 1 is idle (no script entry at t=1 for TC1)
    assert tv[1] is None


def test_audio_view_raises_without_kokoro(monkeypatch):
    """audio_view() raises a clear error when Kokoro is not available."""
    import companion_harness.evals.adapters.scenario_timeline as stmod

    monkeypatch.setattr(stmod, "_load_kokoro", lambda: None)
    tl = build_timeline("TC1")
    with pytest.raises(RuntimeError, match="Kokoro TTS unavailable"):
        tl.audio_view()


def test_multi_item_case_pending_notes():
    """TC30 has two items; both get pending notes at their respective t_avail."""
    tl = build_timeline("TC30")
    notes = [tick.pending_note for tick in tl.ticks if tick.pending_note is not None]
    assert len(notes) == 2  # flight t_avail=3, hotel t_avail=5


def test_build_timeline_all_cases_no_error():
    """All 32 cases can be loaded without error."""
    import yaml
    from pathlib import Path

    l3_path = (
        Path(__file__).parent.parent
        / "companion_harness/evals/adapters/tact_bench_data/cases/layer3-formal-trajectories.yaml"
    )
    l3 = yaml.safe_load(l3_path.read_text())
    for case in l3["cases"]:
        tl = build_timeline(case["id"])
        assert len(tl.ticks) == case["ticks"]


# --- B3: non-empty payloads for all 32 cases ---

def test_all_cases_pending_notes_nonempty():
    """Every case yields at least one PENDING note with a non-empty payload."""
    import yaml
    from pathlib import Path

    l3_path = (
        Path(__file__).parent.parent
        / "companion_harness/evals/adapters/tact_bench_data/cases/layer3-formal-trajectories.yaml"
    )
    l3 = yaml.safe_load(l3_path.read_text())
    for case in l3["cases"]:
        tl = build_timeline(case["id"])
        notes = [t.pending_note for t in tl.ticks if t.pending_note is not None]
        assert notes, f"{case['id']}: no pending notes found"
        for note in notes:
            # note format: [PENDING — from <source>: <payload>]
            assert not note.endswith(": ]"), (
                f"{case['id']}: empty payload in pending note: {note!r}"
            )


def test_tc1_pending_note_has_nonempty_payload():
    """TC1 pending note carries the meeting payload (B3 regression guard)."""
    tl = build_timeline("TC1")
    note = tl.ticks[6].pending_note
    assert note is not None
    assert "3pm meeting" in note or "meeting" in note.lower()
    assert not note.endswith(": ]")


# --- C1: same-tick speech + context ---

def test_tc15_tick0_keeps_user_speech():
    """TC15 tick 0 has both context and user speech; kind must be 'speech'."""
    tl = build_timeline("TC15")
    tick0 = tl.ticks[0]
    assert tick0.kind == "speech", f"Expected speech, got {tick0.kind!r}"
    assert "yeah" in tick0.text.lower() or "hear you" in tick0.text.lower(), (
        f"User speech text not preserved: {tick0.text!r}"
    )
    assert tick0.context_note is not None, "context_note should carry the context text"
    assert "phone call" in tick0.context_note.lower()
