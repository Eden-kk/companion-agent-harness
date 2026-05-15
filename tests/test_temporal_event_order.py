"""v0.1c Task 16 — test_temporal_event_order.

Success criterion (verbatim):
  pytest -k temporal_event_order passes non-vacuously — ordering is derived from
  the logged event sequence, AND a scripted out-of-window sub-case yields explicit
  uncertainty rather than an invented order.

Fixture: companion_harness/fixtures/temporal_event_order_001/case.json

Consumer contract for this fixture (CRITICAL):
  The signal_trace is non-monotonic at the sub-case boundary — the
  out_of_window_uncertain sub-case resets timestamp_mono_ms to 0.
  Partition by sub_case BEFORE any sorting or processing. Never sort
  the full trace by timestamp.

Ordering is derived from FrameRef.timestamp_mono_ms stored in the ring buffer
(cup at t=1000ms, keys at t=2000ms → cup.ts < keys.ts → cup came first).
A shuffle-invariant assertion proves ordering is not read from array position.
"""

from __future__ import annotations

import random

import pytest

from companion_harness.fixtures.loader import load_fixture
from companion_harness.vision_sidecar import FrameRef, VisionSidecar


# ---------------------------------------------------------------------------
# Helpers

def _noop_scorer(prev: bytes, curr: bytes) -> float:
    return 0.0


def _noop_grounder(frame: bytes, query: str) -> tuple[str, float]:
    return "", 0.0


def _make_sidecar(window_ms: int = 60_000) -> VisionSidecar:
    return VisionSidecar(
        scene_scorer=_noop_scorer,
        grounding_model=_noop_grounder,
        window_ms=window_ms,
    )


def _ingest_rows(sidecar: VisionSidecar, rows: list[dict]) -> dict[str, FrameRef]:
    """Ingest all frame rows into the sidecar, in fixture order within the sub-case.

    Ingests raw_video_frame rows and resolution-gate frames (resolution_frame=True)
    so that the sidecar's eviction cutoff advances to the query timestamp.
    Returns a mapping from grounding_result label -> FrameRef (video frames only).
    """
    label_to_ref: dict[str, FrameRef] = {}
    for row in rows:
        is_video = row["event_type"] == "raw_video_frame"
        is_gate = row.get("resolution_frame", False)
        if not (is_video or is_gate):
            continue
        ref = FrameRef(
            event_id=row["frame_id"],
            timestamp_mono_ms=row["timestamp_mono_ms"],
            frame_bytes=bytes.fromhex(row["frame_bytes_hex"]),
        )
        sidecar.ingest_frame(ref)
        if is_video and row.get("grounding_result"):
            label_to_ref[row["grounding_result"]] = ref
    return label_to_ref


def _ordering_from_buffer(
    sidecar: VisionSidecar,
    label_to_ref: dict[str, FrameRef],
    label_a: str,
    label_b: str,
) -> str | None:
    """Return 'a_before_b', 'b_before_a', or None (uncertain).

    Looks up both labels in the ring buffer (by event_id) to confirm they are
    still present, then compares timestamp_mono_ms. If either is absent from
    the buffer, returns None.
    """
    buffer_ids = {r.event_id for r in sidecar.buffer_snapshot()}
    ref_a = label_to_ref.get(label_a)
    ref_b = label_to_ref.get(label_b)
    if ref_a is None or ref_b is None:
        return None
    if ref_a.event_id not in buffer_ids or ref_b.event_id not in buffer_ids:
        return None
    if ref_a.timestamp_mono_ms < ref_b.timestamp_mono_ms:
        return "a_before_b"
    if ref_b.timestamp_mono_ms < ref_a.timestamp_mono_ms:
        return "b_before_a"
    return None


# ---------------------------------------------------------------------------
# Fixture loading


@pytest.fixture(scope="module")
def fixture_data() -> dict:
    return load_fixture("temporal_event_order_001")


@pytest.fixture(scope="module")
def in_window_rows(fixture_data: dict) -> list[dict]:
    return [r for r in fixture_data["signal_trace"] if r["sub_case"] == "in_window_ordered"]


@pytest.fixture(scope="module")
def out_of_window_rows(fixture_data: dict) -> list[dict]:
    return [r for r in fixture_data["signal_trace"] if r["sub_case"] == "out_of_window_uncertain"]


# ---------------------------------------------------------------------------
# Sub-case: in_window_ordered — persistent sidecar per sub-case


@pytest.fixture(scope="module")
def in_window_sidecar(in_window_rows: list[dict]) -> tuple[VisionSidecar, dict[str, FrameRef]]:
    sidecar = _make_sidecar(window_ms=60_000)
    label_to_ref = _ingest_rows(sidecar, in_window_rows)
    return sidecar, label_to_ref


def test_in_window_both_referents_in_buffer(
    in_window_sidecar: tuple[VisionSidecar, dict[str, FrameRef]],
) -> None:
    """Cup and keys are both within the 60s window at query time (t=40000ms)."""
    sidecar, label_to_ref = in_window_sidecar
    buffer_ids = {r.event_id for r in sidecar.buffer_snapshot()}
    assert label_to_ref["cup"].event_id in buffer_ids, "cup must be in ring buffer"
    assert label_to_ref["keys"].event_id in buffer_ids, "keys must be in ring buffer"


def test_in_window_ordering_cup_before_keys(
    in_window_sidecar: tuple[VisionSidecar, dict[str, FrameRef]],
) -> None:
    """Ordering derived from logged timestamp_mono_ms: cup (t=1000) before keys (t=2000)."""
    sidecar, label_to_ref = in_window_sidecar
    result = _ordering_from_buffer(sidecar, label_to_ref, "cup", "keys")
    assert result == "a_before_b", (
        f"expected cup_before_keys from ring-buffer timestamps, got {result!r}"
    )


def test_in_window_ordering_from_seq_not_array_position(
    in_window_rows: list[dict],
) -> None:
    """Shuffle the trace rows — ordering derived from timestamp_mono_ms must be unchanged.

    This asserts that ordering is read from the logged event fields (timestamp_mono_ms
    stored in FrameRef in the ring buffer), not from array position in the fixture.
    """
    shuffled = [r for r in in_window_rows if r["event_type"] == "raw_video_frame"]
    random.shuffle(shuffled)

    sidecar = _make_sidecar(window_ms=60_000)
    label_to_ref: dict[str, FrameRef] = {}
    for row in shuffled:
        ref = FrameRef(
            event_id=row["frame_id"],
            timestamp_mono_ms=row["timestamp_mono_ms"],
            frame_bytes=bytes.fromhex(row["frame_bytes_hex"]),
        )
        sidecar.ingest_frame(ref)
        if row.get("grounding_result"):
            label_to_ref[row["grounding_result"]] = ref

    result = _ordering_from_buffer(sidecar, label_to_ref, "cup", "keys")
    assert result == "a_before_b", (
        f"shuffled trace must still yield cup_before_keys; got {result!r}"
    )


# ---------------------------------------------------------------------------
# Sub-case: out_of_window_uncertain — persistent sidecar per sub-case


@pytest.fixture(scope="module")
def out_of_window_sidecar(
    out_of_window_rows: list[dict],
) -> tuple[VisionSidecar, dict[str, FrameRef]]:
    sidecar = _make_sidecar(window_ms=60_000)
    label_to_ref = _ingest_rows(sidecar, out_of_window_rows)
    return sidecar, label_to_ref


def test_out_of_window_cup_not_resolvable(
    out_of_window_sidecar: tuple[VisionSidecar, dict[str, FrameRef]],
) -> None:
    """Cup (t=0ms) is not in the ring buffer when query arrives at t=65000ms.

    With window_ms=60000 and query at t=65000, the eviction cutoff is
    65000 - 60000 = 5000ms. Cup at t=0 (<=5000) is evicted; it is not
    resolvable for ordering.
    """
    sidecar, label_to_ref = out_of_window_sidecar
    buffer_ids = {r.event_id for r in sidecar.buffer_snapshot()}
    assert label_to_ref["cup"].event_id not in buffer_ids, (
        "cup frame (t=0ms) must not be in ring buffer when query arrives at t=65000ms"
    )


def test_out_of_window_ordering_is_uncertain(
    out_of_window_sidecar: tuple[VisionSidecar, dict[str, FrameRef]],
) -> None:
    """With cup not resolvable, ordering cannot be determined — explicit uncertainty, not invention."""
    sidecar, label_to_ref = out_of_window_sidecar
    result = _ordering_from_buffer(sidecar, label_to_ref, "cup", "keys")
    assert result is None, (
        f"ordering must be uncertain (None) when cup is not in ring buffer; got {result!r}"
    )
