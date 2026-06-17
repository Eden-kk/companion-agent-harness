"""Hermetic tests for scripts/run_tact_stream.py.

Monkeypatches the three run_case functions with canned Emission lists to verify:
- per-condition / per-run on-disk layout
- scores.json + aggregate.json written per condition
- baselines.json written at root
- meta.json contents (model, conditions, k, preregistered_config, detector_config, etc.)
- aggregation mean±spread correctness
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from companion_harness.evals.adapters.tact_bench_stream_types import Emission
from companion_harness.evals.adapters.tact_bench_layer3 import load_layer3


# ---------------------------------------------------------------------------
# Canned emissions
# ---------------------------------------------------------------------------
def _ok_emission(case_id: str) -> list[Emission]:
    """A single correct delivery for the first item of the given case."""
    cases = load_layer3()
    case = next(c for c in cases if c.id == case_id)
    item = case.items[0]
    return [Emission(
        tick=item.decisive_tick,
        spoke=True,
        text=f"delivering {item.id}",
        detected_item=item.id,
        form=item.expected.form or "SPEAK_BRIEF",
        reanchored=False,
        confidence=0.9,
    )]


def _silent_emissions() -> list[Emission]:
    return []


# ---------------------------------------------------------------------------
# Fixture: patch runners + disable model load
# ---------------------------------------------------------------------------
@pytest.fixture()
def patched_runners(monkeypatch, tmp_path):
    """Patch all three run_case functions; also patch model load."""
    import scripts.run_tact_stream as orch

    # patch _load_model_minicpm and _load_model_gpt to return a sentinel
    monkeypatch.setattr(orch, "_load_model_minicpm", lambda: object())
    monkeypatch.setattr(orch, "_load_model_gpt", lambda: None)

    # patch the runners at import time via monkeypatch on the modules themselves
    import companion_harness.evals.adapters.minicpm_stream_runner as msr
    import companion_harness.evals.adapters.gpt_stream_runner as gsr
    import companion_harness.evals.adapters.monitor_stream_runner as mon

    call_log: list[tuple] = []

    def fake_minicpm_run_case(case_id, arm, input_modality, model, *, give_user_state=False):
        call_log.append(("minicpm", case_id, arm, input_modality, give_user_state))
        return _ok_emission(case_id)

    def fake_gpt_run_case(case_id, arm, *, realtime=True, give_user_state=False, **kw):
        call_log.append(("gpt", case_id, arm, give_user_state))
        return _ok_emission(case_id)

    def fake_monitor_run_case(case_id, model_obj, model_kind):
        call_log.append(("monitor", case_id, model_kind))
        return _silent_emissions()

    monkeypatch.setattr(msr, "run_case", fake_minicpm_run_case)
    monkeypatch.setattr(gsr, "run_case", fake_gpt_run_case)
    monkeypatch.setattr(mon, "run_case", fake_monitor_run_case)

    return tmp_path, call_log


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _run(args: list[str]) -> None:
    import scripts.run_tact_stream as orch
    orch.main(args)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_minicpm_disk_layout(patched_runners):
    tmp, calls = patched_runners
    _run([
        "--model", "minicpm",
        "--arms", "vanilla_audio,prompted_audio",
        "--cases", "TC1",
        "--k", "2",
        "--out", str(tmp),
    ])

    for cond in ("vanilla_audio", "prompted_audio"):
        cond_dir = tmp / "minicpm" / cond
        assert cond_dir.is_dir(), f"missing {cond_dir}"
        for r in (1, 2):
            run_dir = cond_dir / f"run{r}"
            assert (run_dir / "TC1.json").exists(), f"missing {run_dir}/TC1.json"
        assert (cond_dir / "scores.json").exists()
        assert (cond_dir / "aggregate.json").exists()


def test_gpt_disk_layout(patched_runners):
    tmp, calls = patched_runners
    _run([
        "--model", "gpt-realtime-2",
        "--arms", "vanilla_audio",
        "--cases", "TC1",
        "--k", "2",
        "--out", str(tmp),
    ])

    cond_dir = tmp / "gpt-realtime-2" / "vanilla_audio"
    for r in (1, 2):
        assert (cond_dir / f"run{r}" / "TC1.json").exists()
    assert (cond_dir / "scores.json").exists()
    assert (cond_dir / "aggregate.json").exists()


def test_monitor_stream_condition(patched_runners):
    tmp, calls = patched_runners
    _run([
        "--model", "minicpm",
        "--arms", "monitor_stream",
        "--cases", "TC1",
        "--k", "1",
        "--out", str(tmp),
    ])
    monitor_calls = [c for c in calls if c[0] == "monitor"]
    assert len(monitor_calls) == 1
    assert monitor_calls[0][2] == "minicpm"
    assert (tmp / "minicpm" / "monitor_stream" / "run1" / "TC1.json").exists()


def test_baselines_json(patched_runners):
    tmp, _ = patched_runners
    _run([
        "--model", "minicpm",
        "--arms", "vanilla_audio",
        "--cases", "TC1",
        "--k", "1",
        "--out", str(tmp),
    ])
    baselines = json.loads((tmp / "baselines.json").read_text())
    assert "oracle" in baselines
    assert "always_silent" in baselines
    assert "always_deliver" in baselines
    # oracle must be best
    assert baselines["oracle"]["cost_weighted_score"] >= baselines["always_deliver"]["cost_weighted_score"]
    assert baselines["oracle"]["cost_weighted_score"] > baselines["always_silent"]["cost_weighted_score"]


def test_meta_json_contents(patched_runners):
    tmp, _ = patched_runners
    _run([
        "--model", "minicpm",
        "--arms", "vanilla_audio,prompted_audio",
        "--cases", "TC1",
        "--k", "2",
        "--out", str(tmp),
    ])
    meta = json.loads((tmp / "minicpm" / "meta.json").read_text())
    assert meta["model"] == "minicpm"
    assert meta["k"] == 2
    assert "vanilla_audio" in meta["conditions"]
    assert "prompted_audio" in meta["conditions"]
    assert "preregistered_config" in meta
    assert "cost_matrix" in meta["preregistered_config"]
    assert "detector_config" in meta
    assert "t_lex" in meta["detector_config"]
    assert "t_emb" in meta["detector_config"]
    assert "emb_model_id" in meta["detector_config"]
    assert "arm_prompts" in meta
    assert "vanilla" in meta["arm_prompts"]
    assert "prompted" in meta["arm_prompts"]
    assert "timestamp" in meta
    assert "TC1" in meta["cases"]


def test_aggregate_mean_spread(patched_runners):
    tmp, _ = patched_runners
    _run([
        "--model", "minicpm",
        "--arms", "vanilla_audio",
        "--cases", "TC1",
        "--k", "3",
        "--out", str(tmp),
    ])
    agg = json.loads((tmp / "minicpm" / "vanilla_audio" / "aggregate.json").read_text())
    assert "cost_weighted_score" in agg
    entry = agg["cost_weighted_score"]
    assert "mean" in entry and "spread" in entry
    # 3 identical runs → spread = 0
    assert entry["spread"] == pytest.approx(0.0)
    assert 0.0 <= entry["mean"] <= 1.0


def test_scores_json_has_k_entries(patched_runners):
    tmp, _ = patched_runners
    _run([
        "--model", "minicpm",
        "--arms", "vanilla_audio",
        "--cases", "TC1",
        "--k", "3",
        "--out", str(tmp),
    ])
    scores = json.loads((tmp / "minicpm" / "vanilla_audio" / "scores.json").read_text())
    assert len(scores) == 3
    for entry in scores:
        assert "run" in entry and "metrics" in entry


def test_run_json_has_emissions_and_score(patched_runners):
    tmp, _ = patched_runners
    _run([
        "--model", "minicpm",
        "--arms", "vanilla_audio",
        "--cases", "TC1",
        "--k", "1",
        "--out", str(tmp),
    ])
    data = json.loads((tmp / "minicpm" / "vanilla_audio" / "run1" / "TC1.json").read_text())
    assert data["case_id"] == "TC1"
    assert "emissions" in data
    assert "score" in data


def test_limit_flag(patched_runners):
    tmp, calls = patched_runners
    _run([
        "--model", "minicpm",
        "--arms", "vanilla_audio",
        "--limit", "2",
        "--k", "1",
        "--out", str(tmp),
    ])
    # should have called run_case exactly 2 times (limit=2, k=1)
    minicpm_calls = [c for c in calls if c[0] == "minicpm"]
    assert len(minicpm_calls) == 2


def test_prompted_audio_state_dispatches_give_user_state_true(patched_runners):
    """prompted_audio_state condition dispatches with give_user_state=True."""
    tmp, calls = patched_runners
    _run([
        "--model", "minicpm",
        "--arms", "prompted_audio_state",
        "--cases", "TC1",
        "--k", "1",
        "--out", str(tmp),
    ])
    minicpm_calls = [c for c in calls if c[0] == "minicpm"]
    assert len(minicpm_calls) == 1
    # give_user_state is the 5th element of the tuple
    assert minicpm_calls[0][4] is True, f"expected give_user_state=True, got: {minicpm_calls[0]}"


def test_prompted_audio_dispatches_give_user_state_false(patched_runners):
    """prompted_audio condition dispatches with give_user_state=False."""
    tmp, calls = patched_runners
    _run([
        "--model", "minicpm",
        "--arms", "prompted_audio",
        "--cases", "TC1",
        "--k", "1",
        "--out", str(tmp),
    ])
    minicpm_calls = [c for c in calls if c[0] == "minicpm"]
    assert len(minicpm_calls) == 1
    assert minicpm_calls[0][4] is False, f"expected give_user_state=False, got: {minicpm_calls[0]}"


def test_gpt_prompted_audio_state_dispatches_give_user_state_true(patched_runners):
    """gpt prompted_audio_state dispatches with give_user_state=True."""
    tmp, calls = patched_runners
    _run([
        "--model", "gpt-realtime-2",
        "--arms", "prompted_audio_state",
        "--cases", "TC1",
        "--k", "1",
        "--out", str(tmp),
    ])
    gpt_calls = [c for c in calls if c[0] == "gpt"]
    assert len(gpt_calls) == 1
    assert gpt_calls[0][3] is True, f"expected give_user_state=True, got: {gpt_calls[0]}"


def test_default_arms_include_prompted_audio_state_when_tier2_applies(patched_runners, monkeypatch):
    """Default arm list includes prompted_audio_state when tier2 apply_to has prompted_audio."""
    import scripts.run_tact_stream as orch
    from companion_harness.evals.adapters import tact_bench

    monkeypatch.setattr(tact_bench, "load_tier2", lambda: {
        "apply_to": ["prompted_audio"],
        "user_state_labels": {},
    })

    tmp, calls = patched_runners
    _run([
        "--model", "minicpm",
        "--cases", "TC1",
        "--k", "1",
        "--out", str(tmp),
    ])
    conditions_run = {c[2] for c in calls if c[0] == "minicpm"}
    assert "prompted" in conditions_run or any(
        c[1] == "TC1" and c[4] is True for c in calls if c[0] == "minicpm"
    ), f"prompted_audio_state not dispatched; calls={calls}"
    # More directly: check output directory exists
    assert (tmp / "minicpm" / "prompted_audio_state").is_dir(), (
        "prompted_audio_state condition directory missing"
    )


def test_render_only_skips_model_load(tmp_path, monkeypatch):
    """render-only: reads existing emissions, no model import."""
    import scripts.run_tact_stream as orch

    loaded = []
    monkeypatch.setattr(orch, "_load_model_minicpm", lambda: loaded.append("bad") or None)

    # Pre-seed a run1/TC1.json so render-only has something to read
    cond_dir = tmp_path / "minicpm" / "vanilla_audio"
    run_dir = cond_dir / "run1"
    run_dir.mkdir(parents=True)
    em = Emission(tick=5, spoke=True, text="deliver", detected_item=None,
                  form=None, reanchored=False, confidence=0.0)
    import dataclasses as dc
    payload = {"case_id": "TC1", "emissions": [dc.asdict(em)], "score": {}}
    (run_dir / "TC1.json").write_text(json.dumps(payload))

    _run = orch.main
    _run([
        "--model", "minicpm",
        "--arms", "vanilla_audio",
        "--cases", "TC1",
        "--k", "1",
        "--out", str(tmp_path),
        "--render-only",
    ])
    assert not loaded, "model should not be loaded in render-only mode"
