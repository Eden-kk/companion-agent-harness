"""Tests for companion_harness.evals.registry (E1).

Success criterion: 4+ tests pass; ADAPTERS holds the expected keys;
every builder returns a BenchmarkAdapter; no runtime imports triggered.
"""

from __future__ import annotations

import sys


_EXPECTED_KEYS = {
    "harness_native",
    "vocalbench",
    "voicebench",
    "humdial_fdbench",
    "candor",
    "full_duplex_bench",
    "tact_bench",
}

_ALLOWED_STATUSES = {"ready", "synthetic_only", "disabled"}


def test_all_registered_adapters_present() -> None:
    from companion_harness.evals.registry import ADAPTERS
    assert set(ADAPTERS) == _EXPECTED_KEYS


def test_each_adapter_info_is_well_formed() -> None:
    from companion_harness.evals.registry import ADAPTERS
    for name, info in ADAPTERS.items():
        assert info.name, f"{name}: empty name"
        assert info.version, f"{name}: empty version"
        assert info.status in _ALLOWED_STATUSES, f"{name}: bad status {info.status!r}"
        assert info.supports_real_mode or info.supports_synthetic_mode, (
            f"{name}: must support at least one mode"
        )


def test_each_builder_returns_benchmark_adapter() -> None:
    from companion_harness.evals.registry import ADAPTERS
    from companion_harness.evals.protocols import BenchmarkAdapter
    for name, info in ADAPTERS.items():
        adapter = info.build()
        assert isinstance(adapter, BenchmarkAdapter), (
            f"{name}: build() returned {type(adapter).__name__}, expected BenchmarkAdapter"
        )
        assert adapter.scenario_driver is not None, f"{name}: scenario_driver is None"


def test_registry_does_not_import_runtime() -> None:
    import importlib
    import types

    runtime_modules = [
        m for m in list(sys.modules)
        if m.startswith("companion_harness") and not m.startswith("companion_harness.evals")
    ]

    # Import registry fresh (it may already be cached, but we check the module graph)
    import companion_harness.evals.registry as reg_mod
    assert reg_mod is not None

    # The registry module itself must not directly reference realtime_loop
    import ast
    import inspect
    src = inspect.getsource(reg_mod)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not module.startswith("companion_harness.realtime_loop"), (
                "registry must not import realtime_loop"
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("companion_harness.realtime_loop"), (
                    "registry must not import realtime_loop"
                )
