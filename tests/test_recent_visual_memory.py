"""v0.1c Task 11 — recent_visual_memory ring-buffer window contract test.

Success criterion (verbatim):
  pytest -k recent_visual_memory passes non-vacuously — an in-window referent
  resolves, AND a negative-path assertion: a referent OUTSIDE the ring-buffer
  window is NOT resolvable (proving the window bound is real; the fixture
  supplies timestamp_mono_ms values to drive eviction deterministically).
"""

import pytest

from companion_harness.fixtures.loader import load_fixture
from companion_harness.vision_sidecar import FrameRef, VisionSidecar


class _ConstantScorer:
    def __call__(self, prev_frame: bytes, curr_frame: bytes) -> float:
        return 0.05


class _ScriptedGrounder:
    """Returns scripted (label, confidence) based on the frame bytes."""

    def __init__(self, label: str, confidence: float) -> None:
        self._label = label
        self._confidence = confidence

    def __call__(self, frame: bytes, query: str) -> tuple[str, float]:
        return self._label, self._confidence


def _hex_to_bytes(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str)


def _build_sidecar() -> VisionSidecar:
    return VisionSidecar(
        scene_scorer=_ConstantScorer(),
        grounding_model=_ScriptedGrounder("keys", 0.89),
        window_ms=60_000,
    )


@pytest.fixture(scope="module")
def fixture_data() -> dict:
    return load_fixture("recent_visual_memory_001")


def _sub_case_events(fixture_data: dict, sub_case: str) -> list[dict]:
    return [e for e in fixture_data["signal_trace"] if e["sub_case"] == sub_case]


# ---------------------------------------------------------------------------
# in_window sub-case: referent at t=5000ms, query at t=40000ms (35000ms < 60000ms)

def test_in_window_referent_resolves(fixture_data: dict) -> None:
    events = _sub_case_events(fixture_data, "in_window")
    sidecar = _build_sidecar()

    referent = next(e for e in events if e["event_type"] == "raw_video_frame")
    gate = next(e for e in events if e.get("resolution_frame"))

    ref = FrameRef(
        event_id=referent["frame_id"],
        timestamp_mono_ms=referent["timestamp_mono_ms"],
        frame_bytes=_hex_to_bytes(referent["frame_bytes_hex"]),
    )
    sidecar.ingest_frame(ref)

    # Ingest the gate frame to advance the timestamp
    gate_ref = FrameRef(
        event_id=gate["frame_id"],
        timestamp_mono_ms=gate["timestamp_mono_ms"],
        frame_bytes=_hex_to_bytes(gate["frame_bytes_hex"]),
    )
    sidecar.ingest_frame(gate_ref)

    # 35000ms < 60000ms: referent must still be in buffer
    referent_ids = [r.event_id for r in sidecar.buffer_snapshot()]
    assert referent["frame_id"] in referent_ids, "referent should still be in buffer in-window"

    result = sidecar.resolve("what did I just pick up?", deictic_reference=True)

    assert result.frame_event_id is not None, "in-window referent must be resolvable"
    assert result.label == gate["grounding_result"]
    assert result.confidence == pytest.approx(gate["grounding_confidence"])


# ---------------------------------------------------------------------------
# out_of_window sub-case: referent at t=100000ms, query at t=170000ms (70000ms > 60000ms)

def test_out_of_window_referent_not_resolvable(fixture_data: dict) -> None:
    """Negative-path: referent evicted at 70000ms gap proves the 60000ms window bound is real."""
    events = _sub_case_events(fixture_data, "out_of_window")
    sidecar = _build_sidecar()

    referent = next(e for e in events if e["event_type"] == "raw_video_frame")
    gate = next(e for e in events if e.get("resolution_frame"))

    ref = FrameRef(
        event_id=referent["frame_id"],
        timestamp_mono_ms=referent["timestamp_mono_ms"],
        frame_bytes=_hex_to_bytes(referent["frame_bytes_hex"]),
    )
    sidecar.ingest_frame(ref)

    # Advance the ring-buffer clock to the gate timestamp to trigger eviction.
    # 170000 - 100000 = 70000ms > window_ms=60000ms => referent is evicted.
    # The gate frame is NOT the referent; it is the clock-advance trigger only.
    clock_advance = FrameRef(
        event_id="clock-advance-out-of-window",
        timestamp_mono_ms=gate["timestamp_mono_ms"],
        frame_bytes=b"\xff",
    )
    sidecar.ingest_frame(clock_advance)

    # Referent must have been evicted — this is the window-bound proof.
    buffered_ids = [r.event_id for r in sidecar.buffer_snapshot()]
    assert referent["frame_id"] not in buffered_ids, (
        "out-of-window referent must be evicted from ring buffer (70000ms > 60000ms window)"
    )

    # resolve() returns the clock-advance frame, NOT the evicted referent —
    # confirming the referent is unreachable.
    result = sidecar.resolve("what did I just pick up?", deictic_reference=True)
    assert result.frame_event_id != referent["frame_id"], (
        "out-of-window referent must NOT be resolvable (negative path proves window bound is real)"
    )


# ---------------------------------------------------------------------------
# edge_in_window sub-case: referent at t=200000ms, query at t=258000ms (58000ms < 60000ms)

def test_edge_in_window_referent_resolves(fixture_data: dict) -> None:
    events = _sub_case_events(fixture_data, "edge_in_window")
    sidecar = _build_sidecar()

    referent = next(e for e in events if e["event_type"] == "raw_video_frame")
    gate = next(e for e in events if e.get("resolution_frame"))

    ref = FrameRef(
        event_id=referent["frame_id"],
        timestamp_mono_ms=referent["timestamp_mono_ms"],
        frame_bytes=_hex_to_bytes(referent["frame_bytes_hex"]),
    )
    sidecar.ingest_frame(ref)

    gate_ref = FrameRef(
        event_id=gate["frame_id"],
        timestamp_mono_ms=gate["timestamp_mono_ms"],
        frame_bytes=_hex_to_bytes(gate["frame_bytes_hex"]),
    )
    sidecar.ingest_frame(gate_ref)

    # 58000ms < 60000ms: referent must still be in buffer
    assert referent["frame_id"] in [r.event_id for r in sidecar.buffer_snapshot()], (
        "edge in-window referent must still be in ring buffer at 58000ms"
    )

    result = sidecar.resolve("what did I just pick up?", deictic_reference=True)

    assert result.frame_event_id is not None, "edge in-window referent must be resolvable"
    assert result.label == gate["grounding_result"]


# ---------------------------------------------------------------------------
# Cross-sub-case: each sub-case uses an independent sidecar (no shared state)

def test_sub_cases_use_independent_sidecars(fixture_data: dict) -> None:
    """Each sub-case runs in isolation; out_of_window eviction check proves window bound."""
    for sub_case in ("in_window", "out_of_window", "edge_in_window"):
        events = _sub_case_events(fixture_data, sub_case)
        sidecar = _build_sidecar()

        raw_frames = [e for e in events if e["event_type"] == "raw_video_frame"]
        gate = next(e for e in events if e.get("resolution_frame"))

        for e in sorted(raw_frames, key=lambda x: x["timestamp_mono_ms"]):
            sidecar.ingest_frame(FrameRef(
                event_id=e["frame_id"],
                timestamp_mono_ms=e["timestamp_mono_ms"],
                frame_bytes=_hex_to_bytes(e["frame_bytes_hex"]),
            ))

        # Advance the buffer clock to the gate timestamp.
        clock_advance = FrameRef(
            event_id=f"clock-advance-{sub_case}",
            timestamp_mono_ms=gate["timestamp_mono_ms"],
            frame_bytes=b"\xff",
        )
        sidecar.ingest_frame(clock_advance)

        buffered_ids = [r.event_id for r in sidecar.buffer_snapshot()]
        referent_id = raw_frames[0]["frame_id"]

        if sub_case == "out_of_window":
            assert referent_id not in buffered_ids, (
                f"sub_case={sub_case!r}: referent must be evicted (70000ms > 60000ms window)"
            )
            result = sidecar.resolve("what did I just pick up?", deictic_reference=True)
            assert result.frame_event_id != referent_id, (
                f"sub_case={sub_case!r}: evicted referent must NOT be resolvable"
            )
        else:
            assert referent_id in buffered_ids, (
                f"sub_case={sub_case!r}: referent must still be in buffer"
            )
            result = sidecar.resolve("what did I just pick up?", deictic_reference=True)
            assert result.frame_event_id is not None, (
                f"sub_case={sub_case!r}: in-window referent must be resolvable"
            )
