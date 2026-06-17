"""PR6 — Mode-A structured NOW/WAIT/DROP probe.

Hermetic: a fake model whose chat() returns WAIT while the user is speaking and
NOW at a pause, so we can assert the driver emits per-decision-point labels on a
DEFER case and that a deferral trajectory is representable.
"""
from companion_harness.evals.adapters.tact_bench import (
    TactCaseSource,
    TactMiniCPMDriver,
    _parse_now_wait_drop,
)


class _FakeDeferModel:
    """chat() defers while the user speaks, then delivers at the first pause."""

    def __init__(self):
        self.prompts: list[str] = []

    def chat(self, text: str, max_new_tokens: int = 8) -> str:
        self.prompts.append(text)
        return "WAIT" if "mid-utterance" in text else "NOW"


def _case(case_id: str):
    return next(c for c in TactCaseSource().iter_cases("all") if c.case_id == case_id)


def test_parse_now_wait_drop():
    assert _parse_now_wait_drop("WAIT") == "WAIT"
    assert _parse_now_wait_drop("i think NOW") == "NOW"
    assert _parse_now_wait_drop("drop it") == "DROP"
    assert _parse_now_wait_drop("uh") == "UNKNOWN"


def test_mode_a_emits_per_decision_point_labels_on_defer_case():
    case = _case("TC5-defer")  # t_available=3 lands mid-utterance; breakpoint later
    driver = TactMiniCPMDriver(model_factory=_FakeDeferModel)
    labels = driver.mode_a_labels(case)

    assert labels, "expected at least one decision point"
    assert all(set(d) == {"t", "decision", "user_speaking"} for d in labels)
    assert labels[0]["t"] == 3  # starts at t_available
    # deferral trajectory: WAIT while the user is mid-utterance, then NOW at the pause
    assert labels[0]["decision"] == "WAIT"
    assert labels[-1]["decision"] == "NOW"
    assert not labels[-1]["user_speaking"]  # delivered at a breakpoint
    # stops once resolved (NOW), so only the final label is NOW
    assert [d["decision"] for d in labels[:-1]] == ["WAIT"] * (len(labels) - 1)


def test_mode_a_stops_after_resolution():
    case = _case("TC5-defer")
    driver = TactMiniCPMDriver(model_factory=_FakeDeferModel)
    labels = driver.mode_a_labels(case)
    decisions = [d["decision"] for d in labels]
    assert decisions.count("NOW") == 1 and decisions[-1] == "NOW"
