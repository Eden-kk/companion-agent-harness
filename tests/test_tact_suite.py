"""PR5 — adapter assembly + suite orchestration + registry wiring.

Hermetic: fake model (never speaks) + fake judge client. Asserts the full
artifact tree is written and the registry exposes the adapter without loading
the model.
"""
import asyncio
import json
from types import SimpleNamespace

from companion_harness.evals.adapters.tact_bench import build_tact_bench, run_tact_suite
from companion_harness.evals.protocols import BenchmarkAdapter
from companion_harness.evals.registry import ADAPTERS

_VERDICT = json.dumps(
    {"delivered": False, "first_delivery_chunk": None, "form": None, "echo_only": False, "rationale": "silent"}
)


class _FakeDuplex:
    def __init__(self):
        self.model = self

    def reset_session(self, reset_token2wav_cache=True):
        pass

    def prepare(self, prefix_system_prompt=None):
        pass

    def streaming_prefill(self, audio_waveform=None, text_list=None):
        return {"success": True}

    def streaming_generate(self, **kwargs):
        return {"is_listen": True, "text": ""}


class _FakeModel:
    def __init__(self):
        self._duplex = _FakeDuplex()


def _fake_judge_client():
    def create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=_VERDICT))])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_run_tact_suite_writes_full_artifact_tree(tmp_path):
    adapter = build_tact_bench(
        input_mode="text", arm="prompted",
        model_factory=_FakeModel, judge_client_factory=_fake_judge_client,
    )
    summary = asyncio.run(run_tact_suite(adapter, tmp_path))

    assert (tmp_path / "run.json").exists()
    assert (tmp_path / "metrics.json").exists()
    assert (tmp_path / "report.md").exists()
    logs = list((tmp_path / "event_logs").glob("*.jsonl"))
    assert len(logs) == 14  # one per case

    metrics = json.loads((tmp_path / "metrics.json").read_text())
    assert set(metrics) == {"cried_wolf", "urgent_miss", "breakpoint_hit", "delivery_rate", "conditional_form"}
    # Fake model never delivers → judged (not PENDING), so urgent_miss is a number.
    assert metrics["urgent_miss"] == 1.0
    assert summary["n_cases"] == 14
    assert "TACT-Bench report" in (tmp_path / "report.md").read_text()


def test_build_tact_bench_composes_six_protocols():
    adapter = build_tact_bench(model_factory=_FakeModel)
    assert isinstance(adapter, BenchmarkAdapter)
    assert adapter.case_source is not None
    assert adapter.scenario_driver is not None
    assert adapter.examiner is not None
    assert len(adapter.metrics) == 5
    assert adapter.failure_slicer is not None


def test_registry_exposes_tact_bench_without_loading_model():
    assert "tact_bench" in ADAPTERS
    info = ADAPTERS["tact_bench"]
    assert info.case_count == 14
    adapter = info.build()  # must not load MiniCPM (lazy)
    assert isinstance(adapter, BenchmarkAdapter)


def test_judge_none_yields_pending_metrics(tmp_path):
    adapter = build_tact_bench(model_factory=_FakeModel, with_judge=False)
    asyncio.run(run_tact_suite(adapter, tmp_path))
    metrics = json.loads((tmp_path / "metrics.json").read_text())
    assert metrics["urgent_miss"] is None  # no judge → PENDING
