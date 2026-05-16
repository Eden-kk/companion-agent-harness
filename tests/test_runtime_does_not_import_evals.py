"""Stage-0 contract: no runtime file may import companion_harness.evals.*.

eval-subsystem-spec.md Anchor 1 (import-direction invariant).
Scan uses AST-level detection so commented-out imports don't false-positive.
"""

import ast
from pathlib import Path


def _runtime_py_files() -> list[Path]:
    harness_root = Path(__file__).parent.parent / "companion_harness"
    return [
        p for p in harness_root.rglob("*.py")
        if "/evals/" not in p.as_posix()
        and "/__pycache__/" not in p.as_posix()
    ]


def _evals_imports_in(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("companion_harness.evals"):
                    hits.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("companion_harness.evals"):
                hits.append(module)
    return hits


def test_runtime_does_not_import_evals():
    violations: list[str] = []
    for path in _runtime_py_files():
        hits = _evals_imports_in(path)
        for hit in hits:
            violations.append(f"{path}: imports {hit!r}")
    assert not violations, (
        "Anchor 1 violation — runtime files must not import companion_harness.evals.*:\n"
        + "\n".join(violations)
    )
