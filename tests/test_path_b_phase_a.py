"""Phase A contract tests for Path B listen-transition signal plumbing.

Success criteria:
  test_on_response_complete_fires_once_per_response — callback fires exactly
    once when is_listen transitions False→True after ≥1 proposal.
  test_on_response_complete_not_fired_without_proposal — callback does NOT fire
    if the model never emits a proposal (stays in listen mode).
  test_snapshot_apis_passthrough — save/restore/has/clear snapshot methods on
    ForegroundModel delegate to underlying model.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, AsyncGenerator, Callable
from typing import Any
from unittest.mock import MagicMock, call

import pytest

from companion_harness.event_logger import EventLogger
from companion_harness.foreground_model import ForegroundModel
from companion_harness.schemas import Event, ThinkerProposal


def _make_logger() -> EventLogger:
    async def _sink(evt: Event) -> None:
        pass
    return EventLogger(_sink, maxsize=256)


def _proposal(text: str = "hello") -> ThinkerProposal:
    return ThinkerProposal(
        proposal_type="observation",
        content=text,
        trigger="speech",
        confidence=0.9,
        novelty=0.5,
        interruption_cost=0.3,
        max_utterance_ms=5000,
        cooldown_consumed="speech_turn",
        caused_by=["root"],
    )


class _FakeModel:
    """Fake StreamingDuplexModel that drives listen-transitions deterministically.

    script: list of (is_listen, text|None) pairs, one per frame.
    When is_listen=False and text is set, a proposal is emitted.
    """

    def __init__(self, script: list[tuple[bool, str | None]]) -> None:
        self._script = script
        self._last_is_listen: bool = True
        self._on_listen_transition: Callable[[], None] | None = None

    def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> ThinkerProposal | None:
        return None

    def set_context(self, items: Any) -> None:
        pass

    async def infer_stream(
        self,
        frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
        caused_by: list[str],
        context_items: tuple = (),
    ) -> AsyncGenerator[ThinkerProposal, None]:
        async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
            idx = 0
            async for _ in frame_iter:
                if idx >= len(self._script):
                    break
                is_listen, text = self._script[idx]
                idx += 1
                prev = self._last_is_listen
                self._last_is_listen = is_listen
                if not prev and is_listen and self._on_listen_transition is not None:
                    self._on_listen_transition()
                if not is_listen and text:
                    yield _proposal(text)
        return _gen()

    async def infer_stream_continuous(
        self,
        frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
        caused_by: list[str],
        *,
        on_proposal: Callable[[ThinkerProposal, str], None],
        on_response_complete: Callable[[str], None],
    ) -> None:
        from uuid import uuid4
        state: dict = {"response_id": None, "proposals": 0, "chars": 0}

        def _on_listen_back() -> None:
            rid = state["response_id"]
            if rid is not None and state["proposals"] > 0:
                on_response_complete(rid)
            state["response_id"] = None
            state["proposals"] = 0
            state["chars"] = 0

        self._on_listen_transition = _on_listen_back
        try:
            gen = await self.infer_stream(frame_iter, caused_by)
            async for proposal in gen:
                if state["response_id"] is None:
                    state["response_id"] = f"r-{uuid4().hex[:8]}"
                state["proposals"] += 1
                state["chars"] += len(proposal.content)
                on_proposal(proposal, state["response_id"])
            _on_listen_back()
        finally:
            self._on_listen_transition = None


async def _frames(n: int) -> AsyncIterator[tuple[bytes, bytes | None]]:
    for _ in range(n):
        yield b"\x00" * 32, None


@pytest.mark.asyncio
async def test_on_response_complete_fires_once_per_response() -> None:
    """Callback fires exactly once for a speak→listen→speak→listen sequence."""
    script = [
        (False, "word1"),   # speak
        (False, "word2"),   # speak
        (True,  None),      # listen — response 1 complete
        (False, "word3"),   # speak — response 2 starts
        (True,  None),      # listen — response 2 complete
    ]
    model = _FakeModel(script)
    completed: list[str] = []

    logger = _make_logger()
    fm = ForegroundModel(model=model, session_id="s1", logger=logger)

    collected: list[ThinkerProposal] = []
    await fm.infer_stream_continuous(
        _frames(5),
        ["root"],
        on_proposal=lambda p, _rid: collected.append(p),
        on_response_complete=completed.append,
    )

    assert len(completed) == 2, f"expected 2 completions, got {len(completed)}: {completed}"
    assert len(set(completed)) == 2, "response_ids must be distinct"
    assert len(collected) == 3


@pytest.mark.asyncio
async def test_on_response_complete_not_fired_without_proposal() -> None:
    """Callback does NOT fire if model stays in listen mode (zero proposals)."""
    script = [
        (True, None),
        (True, None),
        (True, None),
    ]
    model = _FakeModel(script)
    completed: list[str] = []

    logger = _make_logger()
    fm = ForegroundModel(model=model, session_id="s1", logger=logger)

    await fm.infer_stream_continuous(
        _frames(3),
        ["root"],
        on_proposal=lambda p, _rid: None,
        on_response_complete=completed.append,
    )

    assert completed == []


@pytest.mark.asyncio
async def test_snapshot_apis_passthrough() -> None:
    """save/restore/has/clear snapshot methods on ForegroundModel delegate correctly."""
    inner = MagicMock()
    inner.save_speculative_snapshot.return_value = object()
    inner.restore_speculative_snapshot.return_value = True
    inner.has_speculative_snapshot.return_value = False
    inner.clear_speculative_snapshot.return_value = None

    logger = _make_logger()
    fm = ForegroundModel(model=inner, session_id="s2", logger=logger)

    snap = fm.save_speculative_snapshot()
    assert snap is inner.save_speculative_snapshot.return_value
    inner.save_speculative_snapshot.assert_called_once()

    result = fm.restore_speculative_snapshot(caused_by=["evt-1"])
    assert result is True
    inner.restore_speculative_snapshot.assert_called_once_with(caused_by=["evt-1"])

    has = fm.has_speculative_snapshot()
    assert has is False
    inner.has_speculative_snapshot.assert_called_once()

    fm.clear_speculative_snapshot()
    inner.clear_speculative_snapshot.assert_called_once()


@pytest.mark.asyncio
async def test_streaming_prefill_text_passthrough() -> None:
    """streaming_prefill_text delegates to underlying model."""
    inner = MagicMock()
    inner.streaming_prefill_text.return_value = None

    logger = _make_logger()
    fm = ForegroundModel(model=inner, session_id="s3", logger=logger)

    fm.streaming_prefill_text(["hello world"], caused_by=["evt-2"])
    inner.streaming_prefill_text.assert_called_once_with(
        text_list=["hello world"], caused_by=["evt-2"]
    )


@pytest.mark.asyncio
async def test_snapshot_apis_noop_on_missing_model() -> None:
    """save no-ops when model lacks the method; restore/has/clear raise AttributeError."""
    inner = MagicMock(spec=[])  # empty spec — no methods
    logger = _make_logger()
    fm = ForegroundModel(model=inner, session_id="s4", logger=logger)

    assert fm.save_speculative_snapshot() is None
    with pytest.raises(AttributeError):
        fm.restore_speculative_snapshot(caused_by=[])
    with pytest.raises(AttributeError):
        fm.has_speculative_snapshot()
    with pytest.raises(AttributeError):
        fm.clear_speculative_snapshot()
    with pytest.raises(AttributeError):
        fm.streaming_prefill_text(["x"], caused_by=[])


@pytest.mark.asyncio
async def test_ring_entries_tagged_with_response_id() -> None:
    """Phase B: ring entries are 3-tuples with response_id; id changes per response cycle."""
    script = [
        (False, "word1"),   # speak — response A
        (False, "word2"),   # speak — response A
        (True,  None),      # listen — response A complete
        (False, "word3"),   # speak — response B
        (True,  None),      # listen — response B complete
    ]
    model = _FakeModel(script)
    logger = _make_logger()
    fm = ForegroundModel(model=model, session_id="s5", logger=logger)

    ring: list[tuple[int, str, Any]] = []

    def _collect(proposal: Any, response_id: str) -> None:
        ring.append((len(ring), response_id, proposal))

    completed: list[str] = []
    await fm.infer_stream_continuous(
        _frames(5),
        ["root"],
        on_proposal=_collect,
        on_response_complete=completed.append,
    )

    assert len(ring) == 3, f"expected 3 ring entries, got {len(ring)}"

    # Each entry is a 3-tuple.
    for entry in ring:
        assert len(entry) == 3

    # response_id must be non-empty for all entries.
    for _, rid, _ in ring:
        assert rid, f"response_id must not be empty, got {rid!r}"

    # First two proposals share response A; third is response B.
    rid_a = ring[0][1]
    assert ring[1][1] == rid_a, "word1 and word2 should share the same response_id"
    rid_b = ring[2][1]
    assert rid_b != rid_a, "response B must have a different response_id than response A"

    # completed response_ids match what was tagged in the ring.
    assert set(completed) == {rid_a, rid_b}
