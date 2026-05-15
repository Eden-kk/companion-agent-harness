"""DecisionTraceStore — per-decision JSON file storage for DecisionTrace objects.

Pairs with the blob:// pattern in input_ingest.py:77-84.
Files at <trace_dir>/<decision_id>.json.
Atomic-rename writes (mirrors core_user_profile_store.py:64-67).
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import DecisionTrace

__all__ = ["DecisionTraceStore"]


def _trace_to_dict(trace: DecisionTrace) -> dict:
    d = dataclasses.asdict(trace)
    # dataclasses.asdict() leaves Enum instances as-is in nested structures;
    # convert ReasonCode values to strings for JSON serialization.
    d["primary_reason_code"] = trace.primary_reason_code.value
    d["supporting_reason_codes"] = [rc.value for rc in trace.supporting_reason_codes]
    return d


def _trace_from_dict(d: dict) -> DecisionTrace:
    d = dict(d)
    d["primary_reason_code"] = ReasonCode(d["primary_reason_code"])
    d["supporting_reason_codes"] = [ReasonCode(v) for v in d["supporting_reason_codes"]]
    return DecisionTrace(**d)


class DecisionTraceStore:
    """Per-decision JSON file storage for DecisionTrace objects."""

    def __init__(self, trace_dir: Path) -> None:
        self._dir = trace_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def write(self, trace: DecisionTrace) -> str:
        """Serialize trace to disk; return URI for use in payload_ref.

        redacted_explanation and sensitive_explanation_ref are forced null on
        disk (sensitive fields; spec line 379). Last-writer-wins on collision.
        """
        sanitized = dataclasses.replace(
            trace,
            redacted_explanation=None,
            sensitive_explanation_ref=None,
        )
        data = _trace_to_dict(sanitized)
        path = self._dir / f"{trace.decision_id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, sort_keys=True))
        os.rename(tmp, path)
        return f"decision_trace://{trace.decision_id}"

    def read(self, decision_id: str) -> DecisionTrace:
        """Read trace file; raises FileNotFoundError if missing."""
        path = self._dir / f"{decision_id}.json"
        data = json.loads(path.read_text())
        return _trace_from_dict(data)
