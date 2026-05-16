"""v0.1j Task 16 — contract: every # UNAVAILABLE: #N marker cites a known issue.

To add a new # UNAVAILABLE: #N marker in source, add N to KNOWN_UNAVAILABLE_ISSUES
in the same PR. This forces a code-review touch to acknowledge the new external
dependency.

Closed issues do NOT fail this test — that is a stewardship concern enforced in the
closing PR, not a contract gate (per OQ-16b).
"""

from __future__ import annotations

import re
from pathlib import Path

# Every issue number that appears in a # UNAVAILABLE: #N marker in the codebase.
# To add a new marker: add N here AND add the marker in source in the same PR.
KNOWN_UNAVAILABLE_ISSUES: frozenset[int] = frozenset({
    157,  # libcudart blocker (MiniCPM native duplex / addressing / EOU)
    161,  # real grounding model pending model wiring
    166,  # real CLIP cosine scorer pending model wiring
    168,  # real cross-modal conflict scorer pending spec decision
    169,  # real XLLM 2025 lightweight pending
    171,  # real safety-risk classifier pending model selection
    183,  # real embedding model pending selection
    188,  # LLM-driven confidence/salience scorer pending
})

_MARKER_RE = re.compile(r"#\s*UNAVAILABLE:\s*#(\d+)")

_SCAN_ROOTS = ("companion_harness", "manual_test_console", "tests")


def _repo_root() -> Path:
    return Path(__file__).parent.parent


def _collect_markers() -> list[tuple[Path, int, int]]:
    """Return (file, line_number, issue_number) for every UNAVAILABLE marker."""
    root = _repo_root()
    results: list[tuple[Path, int, int]] = []
    for rel in _SCAN_ROOTS:
        base = root / rel
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                m = _MARKER_RE.search(line)
                if m:
                    results.append((path, lineno, int(m.group(1))))
    return results


# ---------------------------------------------------------------------------
# Contract test
# ---------------------------------------------------------------------------

def test_unavailable_markers_have_issues() -> None:
    markers = _collect_markers()
    assert markers, "No # UNAVAILABLE: #N markers found — scan may be broken"
    unknown: list[str] = []
    for path, lineno, issue in markers:
        if issue not in KNOWN_UNAVAILABLE_ISSUES:
            unknown.append(
                f"  {path}:{lineno}  #UNAVAILABLE: #{issue}"
                f"  — add {issue} to KNOWN_UNAVAILABLE_ISSUES in this test file"
            )
    assert not unknown, (
        "Markers referencing unknown issue numbers:\n" + "\n".join(unknown)
    )


# ---------------------------------------------------------------------------
# Anti-self-test: verify the parser and the gate logic themselves
# ---------------------------------------------------------------------------

def test_marker_parser_extracts_issue_number() -> None:
    line = "    return 0.0  # UNAVAILABLE: #157 — libcudart blocker"
    m = _MARKER_RE.search(line)
    assert m is not None
    assert int(m.group(1)) == 157


def test_unknown_issue_number_would_be_caught() -> None:
    """Simulate a marker with an issue NOT in the allowlist — must fail the gate."""
    sentinel = 99999
    assert sentinel not in KNOWN_UNAVAILABLE_ISSUES
    fake_markers = [(_repo_root() / "companion_harness" / "fake.py", 1, sentinel)]
    unknown = [
        f"  {p}:{ln}  #UNAVAILABLE: #{iss}"
        for p, ln, iss in fake_markers
        if iss not in KNOWN_UNAVAILABLE_ISSUES
    ]
    assert len(unknown) == 1
    assert "99999" in unknown[0]
