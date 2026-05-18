"""Tests for MiniCPMStreamingModel.chat_stream_turn helper (Option C Stage 2).

Uses __new__ + attribute injection (no GPU weights loaded) — same pattern as
test_foreground_model_minicpm_reset.py. Verifies:
- 3 delta yields → 3 proposals with correct proposal_type
- request_chat_stop() causes early return (Layer 1 cancellation)
- no orphan threads after iteration completes
- special tokens are stripped from delta text
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel


class _FakeStreamer:
    """Yields a fixed sequence of token strings then sentinel None."""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = iter(tokens)

    def __next__(self) -> str | None:
        return next(self._tokens, None)


def _build_model() -> MiniCPMStreamingModel:
    model = MiniCPMStreamingModel.__new__(MiniCPMStreamingModel)
    model._chat_stop_flag = threading.Event()
    model._tokenizer = MagicMock()
    model._base = MagicMock()
    model._inference_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-infer")
    model._logger = None
    model._session_id = "test"
    model._seq = 0
    return model


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_stream_turn_yields_three_proposals() -> None:
    """3 non-empty deltas → 3 ThinkerProposals with proposal_type='observation'."""
    tokens = ["Hello", " world", "!"]
    streamer = _FakeStreamer(tokens)
    model = _build_model()
    model._base.chat.return_value = streamer

    with (
        patch("companion_harness.foreground_model_minicpm.StoppingCriteria"),
        patch("companion_harness.foreground_model_minicpm.StoppingCriteriaList"),
    ):
        gen = await model.chat_stream_turn(np.zeros(16000, dtype=np.float32), caused_by=["evt-1"])
        proposals = [p async for p in gen]

    assert len(proposals) == 3
    for p in proposals:
        assert p.proposal_type == "observation"
        assert p.caused_by == ["evt-1"]

    model._inference_executor.shutdown(wait=False)


@pytest.mark.asyncio
async def test_request_chat_stop_causes_early_return() -> None:
    """Setting stop flag before iterating returns 0 proposals."""
    tokens = ["Hello", " world", "!"]
    streamer = _FakeStreamer(tokens)
    model = _build_model()
    model._base.chat.return_value = streamer

    with (
        patch("companion_harness.foreground_model_minicpm.StoppingCriteria"),
        patch("companion_harness.foreground_model_minicpm.StoppingCriteriaList"),
    ):
        gen = await model.chat_stream_turn(np.zeros(16000, dtype=np.float32), caused_by=["evt-2"])
        model.request_chat_stop()
        proposals = [p async for p in gen]

    assert proposals == []

    model._inference_executor.shutdown(wait=False)


@pytest.mark.asyncio
async def test_request_chat_stop_mid_stream_cancels_next_delta() -> None:
    """Flag set after first delta: second delta never yielded."""
    unblock = threading.Event()

    class _BlockingStreamer:
        def __init__(self) -> None:
            self._calls = 0

        def __next__(self) -> str | None:
            self._calls += 1
            if self._calls == 1:
                return "first"
            unblock.wait()
            return "second"

    streamer = _BlockingStreamer()
    model = _build_model()
    model._base.chat.return_value = streamer

    with (
        patch("companion_harness.foreground_model_minicpm.StoppingCriteria"),
        patch("companion_harness.foreground_model_minicpm.StoppingCriteriaList"),
    ):
        gen = await model.chat_stream_turn(np.zeros(16000, dtype=np.float32), caused_by=["evt-mid"])
        proposals = []
        async_iter = gen.__aiter__()
        proposals.append(await async_iter.__anext__())
        model.request_chat_stop()
        unblock.set()
        remaining = [p async for p in async_iter]

    assert len(proposals) == 1
    assert proposals[0].content == "first"
    assert remaining == []

    model._inference_executor.shutdown(wait=False)


@pytest.mark.asyncio
async def test_no_orphan_threads_after_full_iteration() -> None:
    """After complete iteration, thread count does not grow unboundedly."""
    tokens = ["word"]
    streamer = _FakeStreamer(tokens)
    model = _build_model()
    model._base.chat.return_value = streamer

    threads_before = threading.active_count()

    with (
        patch("companion_harness.foreground_model_minicpm.StoppingCriteria"),
        patch("companion_harness.foreground_model_minicpm.StoppingCriteriaList"),
    ):
        gen = await model.chat_stream_turn(np.zeros(16000, dtype=np.float32), caused_by=["evt-3"])
        async for _ in gen:
            pass

    await asyncio.sleep(0.05)
    model._inference_executor.shutdown(wait=True)

    threads_after = threading.active_count()
    assert threads_after <= threads_before + 1


@pytest.mark.asyncio
async def test_special_tokens_stripped() -> None:
    """Special tokens like <|im_start|> are stripped; content is the clean text."""
    tokens = ["<|im_start|>Hello<|im_end|>"]
    streamer = _FakeStreamer(tokens)
    model = _build_model()
    model._base.chat.return_value = streamer

    with (
        patch("companion_harness.foreground_model_minicpm.StoppingCriteria"),
        patch("companion_harness.foreground_model_minicpm.StoppingCriteriaList"),
    ):
        gen = await model.chat_stream_turn(np.zeros(16000, dtype=np.float32), caused_by=["evt-4"])
        proposals = [p async for p in gen]

    assert len(proposals) == 1
    assert proposals[0].content == "Hello"

    model._inference_executor.shutdown(wait=False)
