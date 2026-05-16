# Eval subsystem quickstart

## Overview

The eval subsystem runs benchmark suites against the companion harness and
produces a **replayable artifact tree** for every run. Each run generates:

- A structured `run.json` manifest (run ID, adapter, timing mode, split).
- Per-case `metrics.json` with typed `MetricValue` results.
- `failure_slices.json` — causal subgraph fragments for every failing case.
- A human-readable `report.md` rendered by the configured `Reporter`.
- `event_logs/` — one `EventLog` JSONL file per case, satisfying the
  "no unlogged behavior" invariant (CLAUDE.md §Core invariants 1).

The event logs feed the Tier-B replay pipeline: given the same recorded
signals the policy layer produces bit-identical decisions (invariant 5).
Text similarity is advisory only; the pass gate uses the behavioral tuple
`same_action_class + same_timing_bucket ± 200 ms + same_interaction_intent +
same_safety_class`.

Eval code lives entirely in `companion_harness/evals/`.  
**The runtime must never import from `companion_harness.evals`** — the
import direction is one-way (evals → runtime schemas, never the reverse).

---

## Install

The eval dependencies are optional. Install them alongside the core package:

```
pip install -e .[eval]
```

The base install (`pip install -e .`) stays eval-free so realtime paths never
pull in `datasets`, `pandas`, `matplotlib`, etc.

---

## Run a benchmark (harness\_native)

```
python -m companion_harness.evals run \
    --adapter harness_native \
    --output  reports/
```

Optional flags:

| Flag | Default | Notes |
|---|---|---|
| `--split SPLIT` | `test` | Dataset split passed to `CaseSource.iter_cases()`. |
| `--timing-mode` | `wall_clock` | `synthetic_clock` available in Phase A.5. |

Phase A supports `harness_native` only; other adapter names exit with code 2.

---

## Output tree

```
reports/
└── <run_id>/
    ├── run.json            # adapter name, timing mode, split, start/end time
    ├── metrics.json        # list[MetricValue] across all cases
    ├── failure_slices.json # list[FailureSlice] for failing cases
    ├── report.md           # rendered by the adapter's Reporter list
    └── event_logs/
        ├── <case_id_0>.jsonl
        ├── <case_id_1>.jsonl
        └── ...
```

`<run_id>` is a deterministic slug derived from the adapter name, split, and
wall-clock start timestamp.

---

## Add a new adapter

`companion_harness/evals/adapters/harness_native.py` is the reference
implementation. A new adapter composes the six protocols from
`companion_harness/evals/protocols.py` into a `BenchmarkAdapter` frozen
dataclass:

```python
from companion_harness.evals.protocols import BenchmarkAdapter

my_adapter = BenchmarkAdapter(
    name="my_benchmark",
    version="0.1",
    case_source=MyCaseSource(),       # or None for harness-native runs
    scenario_driver=MyScenarioDriver(),
    examiner=None,                    # or MyExaminer() for interactive probes
    metrics=[MyMetric()],
    failure_slicer=NoOpFailureSliceExtractor(),
    reporters=[MyReporter()],
)
```

Each protocol is a single-method interface; implement only the ones your
benchmark needs. Static benchmarks typically omit `examiner`; fully
interactive probes omit `case_source`.

Register the adapter name in `companion_harness/evals/runners.py` alongside
the existing `harness_native` branch.

---

## Anchors checklist

| Anchor | Rule |
|---|---|
| Import direction | `companion_harness.evals` imports from `companion_harness.schemas`; the runtime **never** imports from `companion_harness.evals`. |
| Optional `[eval]` extra | Eval deps (`datasets`, `pandas`, etc.) must not appear in the base install or `requirements-dev.txt`. |
| Tier-B replay determinism | Policy-layer replay is bit-identical given the same recorded signals. Non-determinism is a bug, not variance. |
| Schema split | Types used by both runtime and evals live in `companion_harness.schemas`. Eval-only types (`MetricValue`, `BenchmarkResult`, `FailureSlice`, etc.) live in `companion_harness.evals.schemas` and are promoted only when the orchestrator needs them. |
