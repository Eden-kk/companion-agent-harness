"""Stage 1 — closed-question prompt gets a full_response promptly (positive responsiveness).

See docs/architecture-v0.1.md §Part 8 — this test exists specifically to
prevent "passes by being sluggish." Acceptance gates:
  direct_question_latency_p50 < 800 ms
  direct_question_latency_p95 < 1500 ms
Fixture: direct_question_001 in §Part 6c.

Skips cleanly under system python (no torch/CUDA) via pytest.importorskip.
Runs and must pass under the b200 venv with CUDA_VISIBLE_DEVICES set.
"""

import statistics
import time

import pytest

torch = pytest.importorskip("torch", reason="torch not available — b200 venv required")

if not torch.cuda.is_available():
    pytest.skip("CUDA not available — b200 required", allow_module_level=True)

from companion_harness.fixtures.loader import load_fixture
from companion_harness.foreground_model_minicpm import MiniCPMDuplexModel


def test_direct_question_latency():
    """Measures real MiniCPM-o 4.5 text-inference latency on closed questions.

    Gates: direct_question_latency_p50 < 800ms, p95 < 1500ms.
    """
    fixture = load_fixture("direct_question_001")
    assert fixture["case_id"] == "direct_question_001"
    assert fixture["expected_metrics"]["direct_question_latency_ms_p50"] == "<800"
    assert fixture["expected_metrics"]["direct_question_latency_ms_p95"] == "<1500"

    prompts = fixture["text_prompts"]
    model = MiniCPMDuplexModel()

    latencies_ms: list[float] = []
    for prompt in prompts:
        t0 = time.monotonic()
        response = model.chat(prompt)
        t1 = time.monotonic()
        assert response, f"empty response for prompt: {prompt!r}"  # non-emptiness suffices: fixture is closed yes/no; any non-empty reply is complete; this is a latency gate, not a quality gate
        latencies_ms.append((t1 - t0) * 1000)

    latencies_ms.sort()
    p50_ms = statistics.median(latencies_ms)
    p95_idx = min(int(0.95 * len(latencies_ms)), len(latencies_ms) - 1)  # at n=20, idx=19 (the max element); gate has ample margin so this is fine
    p95_ms = latencies_ms[p95_idx]

    assert p50_ms < 800, (
        f"direct_question_latency_p50 = {p50_ms:.1f}ms >= 800ms gate\n"
        f"  p95={p95_ms:.1f}ms  max={max(latencies_ms):.1f}ms  n={len(latencies_ms)}"
    )
    assert p95_ms < 1500, (
        f"direct_question_latency_p95 = {p95_ms:.1f}ms >= 1500ms gate\n"
        f"  p50={p50_ms:.1f}ms  max={max(latencies_ms):.1f}ms  n={len(latencies_ms)}"
    )
