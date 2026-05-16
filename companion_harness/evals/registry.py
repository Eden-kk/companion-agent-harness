from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

from companion_harness.evals.protocols import BenchmarkAdapter


@dataclass(frozen=True)
class AdapterInfo:
    name: str
    version: str
    status: Literal["ready", "synthetic_only", "disabled"]
    case_count: int | None
    supports_real_mode: bool
    supports_synthetic_mode: bool
    build: Callable[[], BenchmarkAdapter]
    notes: str | None = None


def _build_harness_native() -> BenchmarkAdapter:
    from companion_harness.evals.adapters import harness_native
    import pathlib
    repo_root = pathlib.Path(__file__).parent.parent.parent
    return harness_native.build(repo_root=repo_root)


def _build_vocalbench() -> BenchmarkAdapter:
    from companion_harness.evals.adapters.vocalbench import build_vocalbench
    return build_vocalbench(synthetic=True)


def _build_voicebench() -> BenchmarkAdapter:
    from companion_harness.evals.adapters.voicebench import build_voicebench
    return build_voicebench(synthetic=True)


def _build_humdial_fdbench() -> BenchmarkAdapter:
    from companion_harness.evals.adapters.humdial_fdbench import build_humdial_fdbench
    return build_humdial_fdbench(synthetic=True)


def _build_candor() -> BenchmarkAdapter:
    from companion_harness.evals.adapters.candor import build
    return build(synthetic=True)


def _build_full_duplex_bench_v1() -> BenchmarkAdapter:
    from companion_harness.evals.adapters.full_duplex_bench import build_v1
    return build_v1()


ADAPTERS: dict[str, AdapterInfo] = {
    "harness_native":    AdapterInfo("harness_native", "v1", "ready",          None, True,  False, _build_harness_native),
    "vocalbench":        AdapterInfo("vocalbench", "synthetic-v1", "synthetic_only", None, False, True, _build_vocalbench,       notes="real ingestion requires datasets extra"),
    "voicebench":        AdapterInfo("voicebench", "synthetic-v1", "synthetic_only", None, False, True, _build_voicebench,       notes="real ingestion deferred"),
    "humdial_fdbench":   AdapterInfo("humdial_fdbench", "synthetic-v1", "synthetic_only", None, False, True, _build_humdial_fdbench, notes="real ingestion deferred"),
    "candor":            AdapterInfo("candor", "synthetic-v1", "synthetic_only", None, False, True, _build_candor,             notes="real CANDOR data deferred"),
    "full_duplex_bench": AdapterInfo("full_duplex_bench", "v1", "synthetic_only", None, False, True, _build_full_duplex_bench_v1, notes="real FDB data deferred"),
}
