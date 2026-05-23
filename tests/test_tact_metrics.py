"""PR4 — TACT metrics: per-case Metric.compute + cross-case aggregate.

Verifies the headline rates against hand-computed values, the form split
(conditional_form conditions on a valid delivery, so it stays 1.0 even when a
form case never delivers), the phantom guard, n/a, and PENDING propagation.
"""
from companion_harness.evals.adapters.tact_bench import (
    CriedWolf,
    aggregate_tact_metrics,
    tact_metrics,
)
from companion_harness.evals.schemas import MetricValue
from companion_harness.schemas import ReplayRun


def _label(delivered, fdc=None, form=None):
    return {"delivered": delivered, "first_delivery_chunk": fdc, "form": form,
            "echo_only": False, "rationale": ""}


def _run(case_id, behavior, exposes, t_available, label, mask=None, stale=None):
    return ReplayRun(
        run_id="r", case_id=case_id, implementation_config_version="v", policy_version="v",
        started_at="t", finished_at="t",
        results={
            "expected_behavior": {
                "behavior": behavior, "exposes": exposes,
                "t_available": t_available, "becomes_stale_at": stale,
            },
            "speaking_mask": mask or [],
            "judge_label": label,
        },
        failures=[],
    )


# Speaking 0..8, breakpoint from 9 on.
_DEFER_MASK = [True] * 9 + [False] * 5

_A = _run("A-drop", "DROP", ["cried-wolf-drop"], 1, _label(True, 2, "BRIEF"))
_B = _run("B-defer", "DEFER", ["cried-wolf", "breakpoint-hit"], 3, _label(True, 9, "BRIEF"), mask=_DEFER_MASK)
_C = _run("C-urgent", "DELIVER_NOW", ["urgent-miss", "form"], 6, _label(True, 6, "BRIEF"), stale=9)
_D = _run("D-form", "FORM", ["form-accuracy"], 7, _label(False))


def test_aggregate_reproduces_hand_computed_rates():
    m = aggregate_tact_metrics([_A, _B, _C, _D])
    assert m["cried_wolf"] == 0.5          # A unwanted(drop) + B on-breakpoint(ok) over 2 deliveries
    assert m["urgent_miss"] == 0.0         # C delivered by deadline
    assert m["breakpoint_hit"] == 1.0      # B landed on a breakpoint
    assert abs(m["delivery_rate"] - 2 / 3) < 1e-9  # B,C delivered of B,C,D (A is DROP, excluded)
    assert m["conditional_form"] == 1.0    # C delivered BRIEF; D didn't deliver → excluded


def test_conditional_form_beats_unconditional():
    # Of the 2 form cases (C,D), only C delivered (brief) → an "over all form cases"
    # number would be 0.5; conditioning on a valid delivery gives 1.0.
    m = aggregate_tact_metrics([_C, _D])
    assert m["conditional_form"] == 1.0
    assert m["delivery_rate"] == 0.5  # C delivered, D didn't (both non-DROP)


def test_breakpoint_hit_na_when_nothing_delivered():
    nodeliver = _run("B2", "DEFER", ["cried-wolf", "breakpoint-hit"], 3, _label(False), mask=_DEFER_MASK)
    m = aggregate_tact_metrics([nodeliver])
    assert m["breakpoint_hit"] is None     # no delivery to place
    assert m["cried_wolf"] == 0.0          # no deliveries → can't cry wolf


def test_phantom_delivery_before_availability_excluded():
    # delivered at t=1 but result not available until t=7 → not a valid delivery.
    phantom = _run("ph", "FORM", ["form-accuracy"], 7, _label(True, 1, "BRIEF"))
    m = aggregate_tact_metrics([phantom])
    assert m["delivery_rate"] == 0.0
    assert m["conditional_form"] is None   # no valid delivery → n/a


def test_pending_when_judge_missing():
    pend = _run("p", "DELIVER_NOW", ["urgent-miss", "form"], 6, None, stale=9)
    m = aggregate_tact_metrics([pend])
    assert m["urgent_miss"] is None
    assert m["conditional_form"] is None


def test_metric_class_emits_per_case_contribution():
    mv = CriedWolf().compute(_A)
    assert isinstance(mv, MetricValue)
    assert mv.name == "cried_wolf"
    assert mv.value == {"in_scope": True, "num": 1, "den": 1}  # DROP delivery = unwanted
    # out-of-scope case → in_scope False
    assert CriedWolf().compute(_C).value == {"in_scope": False}


def test_tact_metrics_returns_five_named_metrics():
    names = [m.name for m in tact_metrics()]
    assert names == ["cried_wolf", "urgent_miss", "breakpoint_hit", "delivery_rate", "conditional_form"]
