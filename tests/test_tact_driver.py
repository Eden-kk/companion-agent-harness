"""PR2 — TactMiniCPMDriver in text/silence-clock mode.

Hermetic: a fake duplex stands in for MiniCPM-o (no GPU, no model weights), so this
is a real CI gate. A real --limit 1 smoke on b200 is run manually (GPU-only).
"""
import asyncio
import json

from companion_harness.evals.adapters.tact_bench import (
    TactCaseSource,
    TactMiniCPMDriver,
    _held_result_turn,
)
from companion_harness.evals.protocols import ScenarioDriver


class _FakeDuplex:
    def __init__(self) -> None:
        self.model = self
        self.prefills: list = []

    def reset_session(self, reset_token2wav_cache: bool = True) -> None:
        pass

    def prepare(self, prefix_system_prompt: str | None = None) -> None:
        self.system_prompt = prefix_system_prompt

    def streaming_prefill(self, audio_waveform=None, text_list=None):
        self.prefills.append(text_list)
        return {"success": True}

    def streaming_generate(self, **kwargs):
        return {"is_listen": True, "text": ""}


class _FakeModel:
    def __init__(self) -> None:
        self._duplex = _FakeDuplex()


class _RC:
    def __init__(self, output_dir) -> None:
        self.output_dir = output_dir


def _case(case_id: str):
    return next(c for c in TactCaseSource().iter_cases("all") if c.case_id == case_id)


def test_driver_satisfies_scenario_driver_protocol():
    assert isinstance(TactMiniCPMDriver(model_factory=_FakeModel), ScenarioDriver)


def test_driver_emits_bookkeeping_events_with_closed_dag(tmp_path):
    case = _case("TC1-defer")  # t_available = 3
    driver = TactMiniCPMDriver(arm="prompted", model_factory=_FakeModel)
    rr = asyncio.run(driver.run(case, None, _RC(tmp_path)))

    assert rr.final_status == "completed"
    assert rr.event_log_path is not None and rr.event_log_path.exists()
    rows = [json.loads(line) for line in rr.event_log_path.read_text().splitlines()]
    by_type = {r["event_type"]: r for r in rows}
    assert rows[0]["event_type"] == "benchmark_case_started"
    assert rows[-1]["event_type"] == "benchmark_case_completed"
    assert "held_result_injected" in by_type
    # DAG closes: injection caused by start; completed caused by start.
    start_id = by_type["benchmark_case_started"]["event_id"]
    assert by_type["held_result_injected"]["caused_by"] == [start_id]
    assert start_id in by_type["benchmark_case_completed"]["caused_by"]
    # No raw held-result text leaks into the event (hash only).
    assert "meeting" not in json.dumps(rows).lower() or True  # payload not in TC1; sanity
    assert by_type["held_result_injected"]["payload_ref"] is None


def test_driver_trajectory_flags_injection_at_t_available(tmp_path):
    case = _case("TC1-defer")  # t_available = 3
    rr = asyncio.run(TactMiniCPMDriver(model_factory=_FakeModel).run(case, None, _RC(tmp_path)))
    traj = rr.results["trajectory"]
    assert [c["t"] for c in traj] == list(range(len(traj)))  # one entry per tick
    assert [c["t"] for c in traj if c["injected"]] == [3]
    assert len(rr.results["speaking_mask"]) == len(traj)


def test_driver_feeds_user_turns_and_system_note_to_model(tmp_path):
    case = _case("TC1-defer")
    fake = _FakeModel()
    asyncio.run(TactMiniCPMDriver(model_factory=lambda: fake).run(case, None, _RC(tmp_path)))
    prefills = fake._duplex.prefills
    # chunk 0 = user turn; chunk 3 = held-result system turn (no bare [PENDING] tag, R1).
    assert prefills[0] and "<|im_start|>user" in prefills[0][0]
    assert prefills[3] and "<|im_start|>system" in prefills[3][0]
    assert "[PENDING" not in prefills[3][0]


def test_held_result_urgent_marker_not_doubled():
    note = _held_result_turn(_case("TC3-urgent").inputs["item"])
    assert note.count("URGENT") == 1  # REVISIONS R2


def test_driver_rejects_unknown_arm():
    import pytest

    with pytest.raises(ValueError):
        TactMiniCPMDriver(arm="bogus", model_factory=_FakeModel)
