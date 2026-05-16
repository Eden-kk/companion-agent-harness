"""T1: export_replay_report — deterministic directory tree + manifest contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from companion_harness.replay import export_replay_report


def _make_event(
    session_id: str,
    event_id: str,
    event_type: str,
    ts: int,
    payload_ref: str | None = None,
) -> dict:
    return {
        "event_id": event_id,
        "session_id": session_id,
        "schema_version": "0.1",
        "seq_no": 1,
        "event_type": event_type,
        "timestamp_mono_ms": ts,
        "timestamp_wall": "2026-01-01T00:00:00+00:00",
        "source": "test",
        "caused_by": [],
        "payload_hash": "",
        "payload_ref": payload_ref,
        "payload_kind": "signal",
        "subject_class": "unknown",
        "sensitivity": "safe",
        "retention_policy_id": "default",
    }


def _write_fixture_log(path: Path, events: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


def test_export_creates_expected_tree(tmp_path):
    session_id = "sess-001"
    log_path = tmp_path / "events.jsonl"
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir()

    blob_name = "payload-001.bin"
    (blob_dir / blob_name).write_bytes(b"\x00\x01\x02")

    events = [
        _make_event(session_id, "e-001", "raw_audio", 100),
        _make_event(session_id, "e-002", "policy_decision", 200, payload_ref=blob_name),
        _make_event(session_id, "e-003", "policy_decision", 300),
        _make_event("other-session", "e-004", "raw_audio", 400),
        _make_event(session_id, "e-005", "vad_frame", 500),
    ]
    _write_fixture_log(log_path, events)

    out_dir = tmp_path / "export"
    session_dir = export_replay_report(session_id, log_path, out_dir, blob_source_dir=blob_dir)

    assert session_dir == out_dir / session_id
    assert (session_dir / "events.jsonl").exists()
    assert (session_dir / "manifest.json").exists()
    assert (session_dir / "blobs" / blob_name).exists()
    assert (session_dir / "blobs" / blob_name).read_bytes() == b"\x00\x01\x02"


def test_export_events_jsonl_excludes_other_sessions(tmp_path):
    session_id = "sess-002"
    log_path = tmp_path / "events.jsonl"
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir()

    events = [
        _make_event(session_id, "e-001", "raw_audio", 100),
        _make_event("other", "e-999", "raw_audio", 150),
        _make_event(session_id, "e-002", "vad_frame", 200),
    ]
    _write_fixture_log(log_path, events)

    out_dir = tmp_path / "export"
    session_dir = export_replay_report(session_id, log_path, out_dir, blob_source_dir=blob_dir)

    lines = (session_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ids = [json.loads(l)["event_id"] for l in lines if l]
    assert ids == ["e-001", "e-002"]


def test_export_manifest_fields(tmp_path):
    session_id = "sess-003"
    log_path = tmp_path / "events.jsonl"
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir()

    events = [
        _make_event(session_id, "e-001", "raw_audio", 1000),
        _make_event(session_id, "e-002", "policy_decision", 2000, payload_ref="ref-a.bin"),
        _make_event(session_id, "e-003", "policy_decision", 3000, payload_ref="ref-b.bin"),
    ]
    _write_fixture_log(log_path, events)

    out_dir = tmp_path / "export"
    session_dir = export_replay_report(
        session_id, log_path, out_dir,
        blob_source_dir=blob_dir,
        generated_at_wall="2026-01-01T00:00:00+00:00",
    )

    manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["session_id"] == session_id
    assert manifest["event_count"] == 3
    assert manifest["event_count_by_type"]["raw_audio"] == 1
    assert manifest["event_count_by_type"]["policy_decision"] == 2
    assert manifest["first_event_timestamp_mono_ms"] == 1000
    assert manifest["last_event_timestamp_mono_ms"] == 3000
    assert manifest["blob_refs"] == sorted(["ref-a.bin", "ref-b.bin"])
    assert manifest["manifest_schema_version"] == "replay-report-manifest/v1"
    assert manifest["generated_at_wall"] == "2026-01-01T00:00:00+00:00"
    assert manifest["schema_version"] == "0.1"


def test_export_deterministic_across_two_runs(tmp_path):
    session_id = "sess-004"
    log_path = tmp_path / "events.jsonl"
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir()

    events = [
        _make_event(session_id, "e-001", "raw_audio", 100),
        _make_event(session_id, "e-002", "vad_frame", 200),
    ]
    _write_fixture_log(log_path, events)

    sentinel = "1970-01-01T00:00:00+00:00"

    out1 = tmp_path / "run1"
    export_replay_report(session_id, log_path, out1, blob_source_dir=blob_dir, generated_at_wall=sentinel)

    out2 = tmp_path / "run2"
    export_replay_report(session_id, log_path, out2, blob_source_dir=blob_dir, generated_at_wall=sentinel)

    for fname in ["events.jsonl", "manifest.json"]:
        content1 = (out1 / session_id / fname).read_bytes()
        content2 = (out2 / session_id / fname).read_bytes()
        assert content1 == content2, f"{fname} differs between runs"
