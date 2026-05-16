"""Stage 0 — PolicyInputs construction sites must not have bare stub literals.

Every literal value (0.0, 0, False, True, 1.0, None) assigned to a PolicyInputs
field — either in a PolicyInputs(...) constructor call or in an `inputs.X = <literal>`
statement — must be accompanied by a `# UNAVAILABLE: #N` comment within 5 lines
above or on the same line as the literal. This enforces Anchor 1 marker discipline
(docs/plan-v0.1j-execution.md Task 15).

Scope: PolicyInputs CONSTRUCTION sites only.
  - manual_test_console/live_pipeline.py
  - companion_harness/realtime_orchestrator.py
Excluded: adapter Protocol implementations (their stub returns are covered by Task 16).

Two-axis boundary (plan-critic R2 Finding 4):
  This test catches undocumented stubs at the POLICY LAYER SEAM.
  Adapter Protocol implementations (e.g. _NullSceneScorer.score() returning 0.0)
  are excluded — their UNAVAILABLE-marker check is Task 16's job.
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Explicit (type, value) pairs — a set would conflate False/0/0.0 and True/1/1.0
# since they hash identically, causing False/True/1.0 to be skipped.
_STUB_CHECKS: tuple[tuple[type, object], ...] = (
    (bool, False),
    (bool, True),
    (int, 0),
    (float, 0.0),
    (float, 1.0),
    (type(None), None),
)
_UNAVAILABLE_RE = re.compile(r"#\s*UNAVAILABLE\s*:\s*#\d+")

_REPO_ROOT = Path(__file__).parent.parent

_SCAN_FILES = [
    _REPO_ROOT / "manual_test_console" / "live_pipeline.py",
    _REPO_ROOT / "companion_harness" / "realtime_orchestrator.py",
]


def _is_stub_literal(node: ast.expr) -> bool:
    """Return True iff node is a bare literal matching one of the stub checks."""
    if not isinstance(node, ast.Constant):
        return False
    for t, sv in _STUB_CHECKS:
        if type(node.value) is t and node.value == sv:
            return True
    return False


def _is_stub_literal_for_assign(node: ast.expr) -> bool:
    """Subset of stub-literal check for `inputs.X = <literal>` reassignments.

    Excludes `True` because in an Assign context (vs. a constructor kwarg),
    `inputs.X = True` is almost always intentional state activation in a
    command-handling branch (e.g. `inputs.quiet_mode_active = True` after the
    user says "quiet mode") — not a missing-model stub. Constructor defaults
    of `True` (rare, but possible) are still caught by `visit_Call`.
    """
    if not _is_stub_literal(node):
        return False
    # node is ast.Constant guaranteed by _is_stub_literal
    assert isinstance(node, ast.Constant)
    if type(node.value) is bool and node.value is True:
        return False
    return True


def _has_unavailable_marker(source_lines: list[str], lineno: int) -> bool:
    """Return True iff any of the 5 lines above or the same line has an UNAVAILABLE marker.

    lineno is 1-based (matching ast node .lineno).
    """
    first = max(0, lineno - 6)  # 5 lines above: indices lineno-6 .. lineno-2 (0-based)
    last = lineno  # same line: index lineno-1 (0-based), so slice [:lineno]
    window = source_lines[first:last]
    return any(_UNAVAILABLE_RE.search(line) for line in window)


def _collect_violations(filepath: Path) -> list[str]:
    """AST-scan filepath; return list of human-readable violation strings."""
    src = filepath.read_text(encoding="utf-8")
    source_lines = src.splitlines()
    tree = ast.parse(src, filename=str(filepath))

    violations: list[str] = []

    class _Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            # Match PolicyInputs(...) constructor calls.
            if isinstance(node.func, ast.Name) and node.func.id == "PolicyInputs":
                for kw in node.keywords:
                    if kw.arg is None:
                        continue  # **kwargs expansion
                    if _is_stub_literal(kw.value):
                        lineno = kw.value.lineno
                        if not _has_unavailable_marker(source_lines, lineno):
                            violations.append(
                                f"{filepath.name}:{lineno}: "
                                f"PolicyInputs({kw.arg}={kw.value.value!r}) "
                                f"is a stub literal with no UNAVAILABLE marker"
                            )
            self.generic_visit(node)

        def visit_Assign(self, node: ast.Assign) -> None:
            # Match `inputs.X = <literal>` assignments.
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "inputs"
                    and _is_stub_literal_for_assign(node.value)
                ):
                    lineno = node.value.lineno
                    if not _has_unavailable_marker(source_lines, lineno):
                        violations.append(
                            f"{filepath.name}:{lineno}: "
                            f"inputs.{target.attr} = {node.value.value!r} "
                            f"is a stub literal with no UNAVAILABLE marker"
                        )
            self.generic_visit(node)

    _Visitor().visit(tree)
    return violations


# ---------------------------------------------------------------------------
# Main gate
# ---------------------------------------------------------------------------


def test_no_stub_constants_in_policy_inputs_construction() -> None:
    """All stub literals in PolicyInputs construction sites must carry UNAVAILABLE markers."""
    all_violations: list[str] = []
    for filepath in _SCAN_FILES:
        assert filepath.exists(), f"Expected scan target not found: {filepath}"
        all_violations.extend(_collect_violations(filepath))

    assert not all_violations, (
        "Found stub literal(s) in PolicyInputs construction without "
        "# UNAVAILABLE: #N marker.\n"
        "Add an inline marker on the same line or within 5 lines above.\n"
        "Violations:\n" + "\n".join(f"  {v}" for v in all_violations)
    )


# ---------------------------------------------------------------------------
# Self-tests for the scan logic
# ---------------------------------------------------------------------------


def _scan_snippet(source: str) -> list[str]:
    """Run _collect_violations against an in-memory source string."""
    # Write to a temp-like Path object that works with ast.parse + read_text.
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(source)
        tmp = Path(f.name)
    try:
        return _collect_violations(tmp)
    finally:
        os.unlink(tmp)


def test_scan_passes_with_same_line_marker() -> None:
    """PolicyInputs(x=0.0)  # UNAVAILABLE: #157 → no violation."""
    src = textwrap.dedent("""\
        from companion_harness.schemas import PolicyInputs
        scene_var = compute()
        inputs = PolicyInputs(
            user_speaking=scene_var,
            scene_change_score=0.0,  # UNAVAILABLE: #157
        )
    """)
    assert _scan_snippet(src) == []


def test_scan_passes_with_marker_5_lines_above() -> None:
    """Marker 5 lines above the literal → no violation."""
    src = textwrap.dedent("""\
        from companion_harness.schemas import PolicyInputs
        # UNAVAILABLE: #157 — some blocker
        x = 1
        y = 2
        z = 3
        inputs = PolicyInputs(
            scene_change_score=0.0,
        )
    """)
    assert _scan_snippet(src) == []


def test_scan_fails_without_marker() -> None:
    """PolicyInputs(x=0.0) with no marker → one violation."""
    src = textwrap.dedent("""\
        from companion_harness.schemas import PolicyInputs
        inputs = PolicyInputs(
            scene_change_score=0.0,
        )
    """)
    violations = _scan_snippet(src)
    assert len(violations) == 1
    assert "scene_change_score" in violations[0]
    assert "0.0" in violations[0]


def test_scan_fails_marker_too_far_above() -> None:
    """Marker 6 lines above the literal → violation (out of window)."""
    src = textwrap.dedent("""\
        from companion_harness.schemas import PolicyInputs
        # UNAVAILABLE: #157 — some blocker
        x = 1
        y = 2
        z = 3
        w = 4
        inputs = PolicyInputs(
            scene_change_score=0.0,
        )
    """)
    violations = _scan_snippet(src)
    assert len(violations) == 1, f"expected 1 violation, got: {violations}"


def test_scan_skips_non_literal_kwarg() -> None:
    """PolicyInputs(x=some_var) is not a stub literal → no violation."""
    src = textwrap.dedent("""\
        from companion_harness.schemas import PolicyInputs
        some_var = compute()
        inputs = PolicyInputs(
            scene_change_score=some_var,
        )
    """)
    assert _scan_snippet(src) == []


def test_scan_catches_inputs_attribute_assignment() -> None:
    """`inputs.scene_change_score = 0.0` without marker → violation."""
    src = textwrap.dedent("""\
        inputs.scene_change_score = 0.0
    """)
    violations = _scan_snippet(src)
    assert len(violations) == 1
    assert "scene_change_score" in violations[0]


def test_scan_passes_inputs_attribute_with_marker() -> None:
    """`inputs.scene_change_score = 0.0  # UNAVAILABLE: #157` → no violation."""
    src = textwrap.dedent("""\
        inputs.scene_change_score = 0.0  # UNAVAILABLE: #157
    """)
    assert _scan_snippet(src) == []
