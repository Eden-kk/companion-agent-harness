"""AudioOutBroker emits audio_subscriber_drop audit event on confirmed QueueFull.

Contract tests (mirrors test_display_broker_per_subscriber_drop_event.py):
1. Full queue triggers audio_subscriber_drop via logger with correct payload.
2. Throttle: at most 1 emission per subscriber per 60s window.
3. build_app(audio_out_queue_depth=N) propagates to AudioOutBroker.
4. No recursion risk: audio_subscriber_drop is not an audio event (broker only
   handles audio chunks, not Event objects), confirming no feedback loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from companion_harness.schemas import Event


class _FakeLogger:
    def __init__(self) -> None:
        self.logged: list[Event] = []

    def log(self, event: Event) -> None:
        self.logged.append(event)


def test_audio_broker_emits_drop_event_on_queue_full() -> None:
    from manual_test_console.server import AudioOutBroker  # noqa: WPS433

    fake_logger = _FakeLogger()
    broker = AudioOutBroker(counter={}, queue_depth=64)
    broker.set_logger(fake_logger)
    broker.add()

    # Publish enough chunks to overflow: 80 chunks > 64 depth
    for i in range(80):
        broker.publish("sess-1", i, b"\x00\x01" * 100)

    drop_events = [e for e in fake_logger.logged if e.event_type == "audio_subscriber_drop"]
    assert len(drop_events) >= 1, "expected at least one audio_subscriber_drop event"

    drop = drop_events[0]
    assert drop.source == "audio_out_broker"
    assert drop.sensitivity == "safe"
    assert drop.subject_class == "self"
    assert drop.payload_inline is not None
    assert "subscriber_id" in drop.payload_inline
    assert "subscriber_drop_count" in drop.payload_inline
    assert drop.payload_inline["subscriber_drop_count"] >= 1
    assert "queue_depth" in drop.payload_inline
    assert drop.payload_inline["session_id"] == "sess-1"
    assert "seq" in drop.payload_inline


def test_audio_broker_throttles_drop_events_per_subscriber() -> None:
    """100 overflowing chunks → exactly 1 audio_subscriber_drop in the 60s window."""
    from manual_test_console.server import AudioOutBroker  # noqa: WPS433

    fake_logger = _FakeLogger()
    broker = AudioOutBroker(counter={}, queue_depth=64)
    broker.set_logger(fake_logger)
    broker.add()

    for i in range(100):
        broker.publish("sess-throttle", i, b"\xff" * 100)

    drop_events = [e for e in fake_logger.logged if e.event_type == "audio_subscriber_drop"]
    assert len(drop_events) == 1, (
        f"expected exactly 1 throttled drop event, got {len(drop_events)}"
    )


def test_audio_broker_queue_depth_configurable(tmp_path: Path) -> None:
    from manual_test_console.server import KEY_AUDIO_OUT_BROKER, build_app  # noqa: WPS433

    app = build_app(blob_dir=tmp_path / "blobs", audio_out_queue_depth=512)
    broker = app[KEY_AUDIO_OUT_BROKER]
    q = broker.add()
    assert q.maxsize == 512


def test_audio_broker_no_feedback_loop_on_self_drops() -> None:
    """AudioOutBroker.publish() only accepts (session_id, seq, bytes) — it never
    receives Event objects, so audio_subscriber_drop events cannot re-enter the
    publish path. This test confirms the broker stays functional after drop events
    are emitted (no exception, no recursion, counter stays accurate).
    """
    from manual_test_console.server import AudioOutBroker  # noqa: WPS433

    counter: dict = {}
    fake_logger = _FakeLogger()
    broker = AudioOutBroker(counter=counter, queue_depth=64)
    broker.set_logger(fake_logger)
    broker.add()

    # Overflow to trigger drop event emission
    for i in range(80):
        broker.publish("sess-noloop", i, b"\x00" * 50)

    # Confirm counter is set and no exception was raised
    assert counter.get("chunks_dropped", 0) >= 1
    drop_events = [e for e in fake_logger.logged if e.event_type == "audio_subscriber_drop"]
    assert len(drop_events) == 1

    # Confirm broker still works after drops (publishes to a new subscriber)
    q2 = broker.add()
    broker.publish("sess-noloop", 999, b"\xaa" * 10)
    assert not q2.empty()
