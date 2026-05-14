"""Minimal fixture loader for EvaluationCase fixtures.

Each fixture lives in companion_harness/fixtures/<case_id>/case.json.
Returns the raw dict; callers may construct an EvaluationCase from it.
"""

import json
from pathlib import Path

_FIXTURES_DIR = Path(__file__).parent


def load_fixture(case_id: str) -> dict:
    path = _FIXTURES_DIR / case_id / "case.json"
    with path.open() as f:
        return json.load(f)
