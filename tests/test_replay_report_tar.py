"""T2: export_replay_report_tar — byte-stable tar archive contract."""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

from companion_harness.replay import export_replay_report_tar


def _make_event(session_id: str, event_id: str, ts: int) -> dict:
    return {
        "event_id": event_id,
        "session_id": session_id,
        "schema_version": "0.1",
        "seq_no": 1,
        "event_type": "raw_audio",
        "timestamp_mono_ms": ts,
        "timestamp_wall": "2026-01-01T00:00:00+00:00",
        "source": "test",
        "caused_by": [],
        "payload_hash": "",
        "payload_ref": None,
        "payload_kind": "signal",
        "subject_class": "unknown",
        "sensitivity": "safe",
        "retention_policy_id": "default",
    }


def _write_log(path: Path, events: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


def test_tar_contains_expected_members(tmp_path):
    session_id = "sess-tar-001"
    log_path = tmp_path / "events.jsonl"
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir()

    events = [_make_event(session_id, "e-001", 100), _make_event(session_id, "e-002", 200)]
    _write_log(log_path, events)

    tar_path = tmp_path / "output.tar"
    export_replay_report_tar(session_id, log_path, tar_path, blob_source_dir=blob_dir)

    with tarfile.open(tar_path) as tf:
        names = sorted(tf.getnames())

    assert any("events.jsonl" in n for n in names), f"events.jsonl missing; got {names}"
    assert any("manifest.json" in n for n in names), f"manifest.json missing; got {names}"


def test_tar_is_byte_stable_across_runs(tmp_path):
    session_id = "sess-tar-002"
    log_path = tmp_path / "events.jsonl"
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir()

    events = [_make_event(session_id, "e-001", 100)]
    _write_log(log_path, events)

    tar1 = tmp_path / "run1.tar"
    tar2 = tmp_path / "run2.tar"
    export_replay_report_tar(session_id, log_path, tar1, blob_source_dir=blob_dir)
    export_replay_report_tar(session_id, log_path, tar2, blob_source_dir=blob_dir)

    sha1 = hashlib.sha256(tar1.read_bytes()).hexdigest()
    sha2 = hashlib.sha256(tar2.read_bytes()).hexdigest()
    assert sha1 == sha2, "tar bytes differ between runs"


def test_tar_member_timestamps_are_zero(tmp_path):
    session_id = "sess-tar-003"
    log_path = tmp_path / "events.jsonl"
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir()

    events = [_make_event(session_id, "e-001", 100)]
    _write_log(log_path, events)

    tar_path = tmp_path / "output.tar"
    export_replay_report_tar(session_id, log_path, tar_path, blob_source_dir=blob_dir)

    with tarfile.open(tar_path) as tf:
        for member in tf.getmembers():
            assert member.mtime == 0, f"non-zero mtime on {member.name}: {member.mtime}"
            assert member.uid == 0
            assert member.gid == 0
