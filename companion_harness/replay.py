"""Replay harness — Tier A (end-to-end behavioral) and Tier B (policy-layer exact).

See docs/architecture-v0.1.md §Part 2 invariants #5-#6 and §Part 6 Stage 0 for
the two replay tiers. Tier B replay is bit-identical; Tier A uses the behavioral
tuple (same_action_class + same_timing_bucket(+/-200ms) + same_interaction_intent
+ same_safety_class).
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import tarfile
import tempfile
from dataclasses import astuple
from datetime import datetime, timezone
from pathlib import Path

from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import SpeakDecision


@dataclasses.dataclass
class ReplayReportManifest:
    session_id: str
    policy_version: str
    schema_version: str
    event_count: int
    event_count_by_type: dict
    first_event_timestamp_mono_ms: int
    last_event_timestamp_mono_ms: int
    blob_refs: list
    manifest_schema_version: str
    generated_at_wall: str


def export_replay_report(
    session_id: str,
    source_log_path: Path,
    out_dir: Path,
    *,
    blob_source_dir: Path,
    generated_at_wall: str | None = None,
) -> Path:
    """Export a replay report for session_id to {out_dir}/{session_id}/.

    Reads source_log_path (JSONL, the existing blob-dir sink), filters to the
    given session_id, writes a deterministic directory tree and manifest.json.

    Returns the session output directory.
    """
    from companion_harness.speak_policy import POLICY_VERSION  # avoid circular at module level

    if generated_at_wall is None:
        generated_at_wall = datetime.now(timezone.utc).isoformat()

    session_dir = out_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    blobs_dir = session_dir / "blobs"
    blobs_dir.mkdir(exist_ok=True)

    lines: list[str] = []
    event_count_by_type: dict[str, int] = {}
    blob_refs: list[str] = []
    schema_version: str = ""
    first_ts: int | None = None
    last_ts: int | None = None

    with source_log_path.open(encoding="utf-8") as fh:
        for raw_line in fh:
            raw_line = raw_line.rstrip("\n")
            if not raw_line:
                continue
            obj = json.loads(raw_line)
            if obj.get("session_id") != session_id:
                continue
            lines.append(raw_line)
            etype = obj.get("event_type", "unknown")
            event_count_by_type[etype] = event_count_by_type.get(etype, 0) + 1
            if not schema_version and obj.get("schema_version"):
                schema_version = obj["schema_version"]
            ts = obj.get("timestamp_mono_ms")
            if ts is not None:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
            ref = obj.get("payload_ref")
            if ref:
                blob_refs.append(ref)

    events_out = session_dir / "events.jsonl"
    events_out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    for ref in blob_refs:
        src = blob_source_dir / Path(ref).name
        if src.exists():
            shutil.copy2(src, blobs_dir / Path(ref).name)

    manifest = ReplayReportManifest(
        session_id=session_id,
        policy_version=POLICY_VERSION,
        schema_version=schema_version,
        event_count=len(lines),
        event_count_by_type=event_count_by_type,
        first_event_timestamp_mono_ms=first_ts if first_ts is not None else 0,
        last_event_timestamp_mono_ms=last_ts if last_ts is not None else 0,
        blob_refs=sorted(blob_refs),
        manifest_schema_version="replay-report-manifest/v1",
        generated_at_wall=generated_at_wall,
    )
    manifest_path = session_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(dataclasses.asdict(manifest), sort_keys=True, indent=2),
        encoding="utf-8",
    )

    return session_dir


def export_replay_report_tar(
    session_id: str,
    source_log_path: Path,
    out_path: Path,
    *,
    blob_source_dir: Path,
) -> None:
    """Package the replay report for session_id into a deterministic tar at out_path.

    Passes a fixed sentinel for generated_at_wall so the tar is byte-stable
    across runs.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        export_replay_report(
            session_id,
            source_log_path,
            tmp_dir,
            blob_source_dir=blob_source_dir,
            generated_at_wall="1970-01-01T00:00:00+00:00",
        )
        with tarfile.open(out_path, "w") as tf:
            session_dir = tmp_dir / session_id
            for member_path in sorted(session_dir.rglob("*")):
                arcname = str(member_path.relative_to(tmp_dir))
                info = tf.gettarinfo(str(member_path), arcname=arcname)
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mtime = 0
                if member_path.is_file():
                    with member_path.open("rb") as fobj:
                        tf.addfile(info, fobj)
                else:
                    tf.addfile(info)


def run_tier_b_replay(event_log_path: Path) -> list[SpeakDecision]:
    """Read a JSONL event log and return SpeakDecisions from policy_decision events.

    Each policy_decision event must carry payload_inline with action_type and
    primary_reason_code.  Fields not stored inline are set to their zero/empty
    defaults — sufficient for Tier-B comparison of the decision tuple.
    """
    decisions: list[SpeakDecision] = []
    with event_log_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("event_type") != "policy_decision":
                continue
            inline = obj.get("payload_inline") or {}
            decisions.append(SpeakDecision(
                action_type=inline["action_type"],
                primary_reason_code=ReasonCode(inline["primary_reason_code"]),
                supporting_reason_codes=[ReasonCode(rc) for rc in inline.get("supporting_reason_codes", [])],
                redacted_explanation=None,
                caused_by=obj.get("caused_by", []),
                budget_bucket=inline.get("budget_bucket"),
                allowed_prosody_tags=[],
                max_duration_ms=None,
                response_content_source=inline.get("response_content_source", "no_synthesis"),
            ))
    return decisions


def assert_bit_identical(
    original: list[SpeakDecision],
    replayed: list[SpeakDecision],
) -> None:
    """Assert two SpeakDecision sequences are bit-identical via astuple() comparison."""
    assert len(original) == len(replayed), (
        f"length mismatch: original={len(original)}, replayed={len(replayed)}"
    )
    for i, (a, b) in enumerate(zip(original, replayed)):
        assert astuple(a) == astuple(b), (
            f"decision[{i}] mismatch:\n  original={astuple(a)!r}\n  replayed={astuple(b)!r}"
        )
