"""Tests for the §4.6 event-ledger scorer.

Covers: oracle ceiling, always-silent floor, always-deliver cried-wolf,
supersession arbitration (TC8/TC32), +RA partial credit (TC14),
h-state non-seam semantics (TC17), suppress_until (TC26/TC27).
"""
from companion_harness.evals.adapters.tact_bench_layer3 import (
    load_layer3,
    oracle_run,
    always_silent_run,
    always_deliver_run,
    score_run,
    aggregate_runs,
    preregistered_config,
)
from companion_harness.evals.adapters.tact_bench_stream_types import Emission


def _cases():
    return load_layer3()


def test_oracle_ceiling():
    cases = _cases()
    m = score_run(cases, oracle_run(cases))
    assert m["cost_weighted_score"] == 1.0, m["cost_weighted_score"]
    assert m["urgent_miss"] == 0.0 or m["urgent_miss"] is None
    assert m["cried_wolf"] == 0.0


def test_always_silent_floor():
    cases = _cases()
    m = score_run(cases, always_silent_run(cases))
    assert m["never_spoke_rate"] == 1.0
    assert m["urgent_miss"] == 1.0
    assert m["cost_weighted_score"] < 0.5  # clearly below oracle


def test_always_deliver_high_cried_wolf():
    cases = _cases()
    m = score_run(cases, always_deliver_run(cases))
    # delivering DROP items and premature deliveries pushes cried_wolf high
    assert m["cried_wolf"] > 0.0


def test_baselines_ranked():
    cases = _cases()
    oracle = score_run(cases, oracle_run(cases))["cost_weighted_score"]
    always = score_run(cases, always_deliver_run(cases))["cost_weighted_score"]
    silent = score_run(cases, always_silent_run(cases))["cost_weighted_score"]
    assert oracle > always > silent, f"oracle={oracle:.4f} always={always:.4f} silent={silent:.4f}"


def test_supersession_tc8_arbitration_violation():
    """TC8: eta_old superseded by eta_new at t8. Delivering eta_old at t9 triggers:
    (b) superseded item delivered after superseder's t_avail → arbitration violation."""
    cases = _cases()
    case_map = {c.id: c for c in cases}
    tc8 = case_map["TC8"]
    # deliver eta_old at t9 (after eta_new available at t8) — arbitration violation
    bad_run = {
        tc8.id: [
            Emission(tick=9, spoke=True, text="old eta", detected_item="eta_old",
                     form="SPEAK_BRIEF", reanchored=False, confidence=0.9),
        ]
    }
    m = score_run([tc8], bad_run)
    assert m["outcomes"].get("cried_wolf", 0) >= 1
    # arbitration violation must be detected (score < 1.0 for multi-item case)
    assert m["arbitration_accuracy"] is not None and m["arbitration_accuracy"] < 1.0


def test_supersession_tc32_arbitration_violation():
    """TC32: eta_old superseded by eta_new at t5. Delivering eta_old at t6 (>= t5) triggers
    supersession violation → arbitration_accuracy < 1.0."""
    cases = _cases()
    case_map = {c.id: c for c in cases}
    tc32 = case_map["TC32"]
    bad_run = {
        tc32.id: [
            Emission(tick=6, spoke=True, text="old eta", detected_item="eta_old",
                     form="SPEAK_BRIEF", reanchored=False, confidence=0.9),
        ]
    }
    m = score_run([tc32], bad_run)
    assert m["outcomes"].get("cried_wolf", 0) >= 1
    assert m["arbitration_accuracy"] is not None and m["arbitration_accuracy"] < 1.0


def test_ra_tc14_no_reanchor_is_wrong_form():
    """TC14: answer requires +RA. Delivering without reanchored=True → wrong_form class."""
    cases = _cases()
    case_map = {c.id: c for c in cases}
    tc14 = case_map["TC14"]
    # reanchored=False at decisive tick → wrong_form
    bad_run = {
        tc14.id: [
            Emission(tick=22, spoke=True, text="here is the answer", detected_item="answer",
                     form="SPEAK_BRIEF", reanchored=False, confidence=0.9),
        ]
    }
    m = score_run([tc14], bad_run)
    assert m["outcomes"].get("wrong_form", 0) >= 1
    assert m["outcomes"].get("correct", 0) == 0


def test_ra_tc14_with_reanchor_is_correct():
    """TC14: delivering with reanchored=True at decisive tick = correct."""
    cases = _cases()
    case_map = {c.id: c for c in cases}
    tc14 = case_map["TC14"]
    good_run = {
        tc14.id: [
            Emission(tick=22, spoke=True, text="to revisit your earlier question — here is the answer",
                     detected_item="answer", form="SPEAK_BRIEF", reanchored=True, confidence=0.9),
        ]
    }
    m = score_run([tc14], good_run)
    assert m["outcomes"].get("correct", 0) >= 1


def test_h_state_tc17_not_a_seam():
    """TC17: h:4-5 is NOT a breakpoint. Delivering at tick 4 (h) is premature → cried_wolf.
    Delivering at tick 7 (b) is correct."""
    cases = _cases()
    case_map = {c.id: c for c in cases}
    tc17 = case_map["TC17"]
    assert tc17.user_state[4] == "h"
    assert tc17.user_state[7] == "b"

    # delivery at h-tick → premature (before window opens at b-tick 7)
    bad_run = {
        tc17.id: [
            Emission(tick=4, spoke=True, text="note delivery", detected_item="note",
                     form="SPEAK_BRIEF", reanchored=False, confidence=0.9),
        ]
    }
    m_bad = score_run([tc17], bad_run)
    assert m_bad["outcomes"].get("cried_wolf", 0) >= 1

    # delivery at b-tick 7 → correct
    good_run = {
        tc17.id: [
            Emission(tick=7, spoke=True, text="note delivery", detected_item="note",
                     form="SPEAK_BRIEF", reanchored=False, confidence=0.9),
        ]
    }
    m_good = score_run([tc17], good_run)
    assert m_good["outcomes"].get("correct", 0) >= 1


def test_aggregate_runs_mean_spread():
    cases = _cases()
    # two identical oracle runs → spread == 0
    runs = [oracle_run(cases), oracle_run(cases)]
    agg = aggregate_runs(cases, runs)
    assert agg["cost_weighted_score"]["mean"] == 1.0
    assert agg["cost_weighted_score"]["spread"] == 0.0


def test_preregistered_config_keys():
    cfg = preregistered_config()
    assert "cost_matrix" in cfg
    assert "delta_urgent" in cfg
    assert cfg["delta_urgent"] == 2
    assert "tick_tolerance" in cfg


def test_suppress_until_tc26_window():
    """TC26: research available at t6, suppress_until tick=20. Window must be (20,21)."""
    from companion_harness.evals.adapters.tact_bench_layer3 import _delivery_window
    cases = _cases()
    case_map = {c.id: c for c in cases}
    tc26 = case_map["TC26"]
    assert tc26.suppress_until_tick == 20
    item = next(it for it in tc26.items if it.id == "research")
    win = _delivery_window(tc26, item)
    assert win == (20, 21), f"expected (20,21), got {win}"

    # delivery at tick 6 (a pause during suppression) → cried_wolf
    bad_run = {tc26.id: [
        Emission(tick=6, spoke=True, text="research result", detected_item="research",
                 form="SPEAK_BRIEF", reanchored=False, confidence=0.9),
    ]}
    m = score_run([tc26], bad_run)
    assert m["outcomes"].get("cried_wolf", 0) >= 1


def test_suppress_until_tc27_urgent_overrides():
    """TC27: fire is urgent — suppress_until does NOT apply; window = (6, 6+delta)."""
    from companion_harness.evals.adapters.tact_bench_layer3 import _delivery_window
    cases = _cases()
    case_map = {c.id: c for c in cases}
    tc27 = case_map["TC27"]
    assert tc27.suppress_until_tick == 20
    item = next(it for it in tc27.items if it.id == "fire")
    assert item.u == "high"
    win = _delivery_window(tc27, item)
    # urgent overrides suppress → window starts at t_avail=6
    assert win is not None and win[0] == 6
