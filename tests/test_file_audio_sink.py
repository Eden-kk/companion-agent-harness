"""FileAudioSink — contract test (live-loop Task 4).

Success criterion (verbatim):
  A contract test drives AudioOutputController with a FileAudioSink, plays a
  scripted approved-SpeakDecision byte stream, and asserts (a) the file contains
  exactly the streamed bytes, and (b) the assistant_audio_buffer_flushed event is
  logged with a caused_by[] chain.
"""

import pytest

from companion_harness.audio_output_controller import AudioOutputController, FileAudioSink
from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event


async def _agen(chunks: list[bytes]):
    for chunk in chunks:
        yield chunk


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=256), received


@pytest.mark.asyncio
async def test_file_audio_sink_receives_all_bytes(tmp_path):
    """(a) FileAudioSink writes exactly the streamed bytes to disk."""
    out = tmp_path / "audio.pcm"
    file_sink = FileAudioSink(out)

    logger, received = _make_logger()
    await logger.start()

    ctrl = AudioOutputController(
        session_id="test-file-sink",
        logger=logger,
        sink=file_sink,
    )

    speak_decision_id = "speak-decision-task4-001"
    gen_id = ctrl.start_generation(caused_by=[speak_decision_id])

    chunks = [b"chunk-A" * 10, b"chunk-B" * 10, b"chunk-C" * 10]
    await ctrl.play(_agen(chunks), generation_event_id=gen_id)

    file_sink.close()
    await logger.stop()

    # (a) file contains exactly the concatenation of all streamed chunks
    assert out.read_bytes() == b"".join(chunks)


@pytest.mark.asyncio
async def test_file_audio_sink_flushed_event_caused_by_chain(tmp_path):
    """(b) assistant_audio_buffer_flushed is logged with a non-empty caused_by[] chain."""
    out = tmp_path / "audio2.pcm"
    file_sink = FileAudioSink(out)

    logger, received = _make_logger()
    await logger.start()

    ctrl = AudioOutputController(
        session_id="test-file-sink-causal",
        logger=logger,
        sink=file_sink,
    )

    speak_decision_id = "speak-decision-task4-002"
    gen_id = ctrl.start_generation(caused_by=[speak_decision_id])

    chunks = [b"\x00" * 160, b"\x01" * 160]
    await ctrl.play(_agen(chunks), generation_event_id=gen_id)

    file_sink.close()
    await logger.stop()

    # (b) assistant_audio_buffer_flushed emitted with non-empty caused_by[]
    flushed = next(
        (e for e in received if e.event_type == "assistant_audio_buffer_flushed"), None
    )
    assert flushed is not None, "assistant_audio_buffer_flushed event not emitted"
    assert flushed.caused_by, (
        f"assistant_audio_buffer_flushed has empty caused_by — orphan event"
    )
    # caused_by traces to the generation_start event (which traces to speak_decision)
    assert gen_id in flushed.caused_by

    # Every emitted event has non-empty caused_by (invariant #1 / orphan gate)
    for evt in received:
        assert evt.caused_by, (
            f"orphan event {evt.event_id!r} ({evt.event_type}) has empty caused_by"
        )
