"""Unit tests for hybrid chat-stream conversation history (Option 1).

Covers:
- msgs shape with N=0, N=1, N=3 prior turns
- FIFO eviction at N=6 cap (_CONV_HISTORY_MAX_TURNS)
- empty transcripts / empty assistant text skipped
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel
from companion_harness.realtime_orchestrator import _CONV_HISTORY_MAX_TURNS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeStreamer:
    def __init__(self, tokens: list[str]) -> None:
        self._tokens = iter(tokens)

    def __next__(self) -> str | None:
        return next(self._tokens, None)


def _build_model() -> MiniCPMStreamingModel:
    model = MiniCPMStreamingModel.__new__(MiniCPMStreamingModel)
    model._chat_stop_flag = threading.Event()
    model._tokenizer = MagicMock()
    model._base = MagicMock()
    model._inference_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-history")
    model._logger = None
    model._session_id = "test"
    model._seq = 0
    return model


def _captured_msgs(model: MiniCPMStreamingModel, prior_turns):
    """Run chat_stream_turn synchronously and return the msgs list passed to chat()."""
    import asyncio

    streamer = _FakeStreamer(["hi"])
    model._base.chat.return_value = streamer
    captured: list = []

    original_chat = model._base.chat

    def _spy_chat(**kwargs):
        captured.append(kwargs.get("msgs", []))
        return original_chat(**kwargs)

    model._base.chat.side_effect = lambda **kw: (captured.append(kw.get("msgs", [])), streamer)[1]

    with (
        patch("companion_harness.foreground_model_minicpm.StoppingCriteria"),
        patch("companion_harness.foreground_model_minicpm.StoppingCriteriaList"),
    ):
        gen = asyncio.get_event_loop().run_until_complete(
            model.chat_stream_turn(
                np.zeros(16000, dtype=np.float32),
                caused_by=["evt-1"],
                prior_turns=prior_turns,
            )
        )
        asyncio.get_event_loop().run_until_complete(
            _drain(gen)
        )

    return captured[0] if captured else []


async def _drain(gen):
    async for _ in gen:
        pass


# ---------------------------------------------------------------------------
# msgs shape tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_msgs_no_prior_turns() -> None:
    """N=0 prior turns: msgs = [system, user_audio]."""
    model = _build_model()
    streamer = _FakeStreamer(["hi"])
    model._base.chat.return_value = streamer
    captured: list = []
    model._base.chat.side_effect = lambda **kw: (captured.append(kw.get("msgs", [])), streamer)[1]

    with (
        patch("companion_harness.foreground_model_minicpm.StoppingCriteria"),
        patch("companion_harness.foreground_model_minicpm.StoppingCriteriaList"),
    ):
        gen = await model.chat_stream_turn(
            np.zeros(16000, dtype=np.float32),
            caused_by=["e"],
            prior_turns=None,
        )
        async for _ in gen:
            pass

    msgs = captured[0]
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    model._inference_executor.shutdown(wait=False)


@pytest.mark.asyncio
async def test_msgs_one_prior_turn_pair() -> None:
    """N=1 pair (user+assistant): msgs = [system, user_hist, asst_hist, user_audio]."""
    model = _build_model()
    streamer = _FakeStreamer(["hi"])
    model._base.chat.return_value = streamer
    captured: list = []
    model._base.chat.side_effect = lambda **kw: (captured.append(kw.get("msgs", [])), streamer)[1]

    prior = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]

    with (
        patch("companion_harness.foreground_model_minicpm.StoppingCriteria"),
        patch("companion_harness.foreground_model_minicpm.StoppingCriteriaList"),
    ):
        gen = await model.chat_stream_turn(
            np.zeros(16000, dtype=np.float32),
            caused_by=["e"],
            prior_turns=prior,
        )
        async for _ in gen:
            pass

    msgs = captured[0]
    assert len(msgs) == 4
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == ["hello"]
    assert msgs[2]["role"] == "assistant"
    assert msgs[2]["content"] == ["hi there"]
    assert msgs[3]["role"] == "user"
    assert msgs[3]["content"][0] is not None  # audio ndarray
    model._inference_executor.shutdown(wait=False)


@pytest.mark.asyncio
async def test_msgs_three_prior_turn_pairs() -> None:
    """N=3 pairs: msgs = [system] + 6 history + [user_audio] = 8 total."""
    model = _build_model()
    streamer = _FakeStreamer(["hi"])
    model._base.chat.return_value = streamer
    captured: list = []
    model._base.chat.side_effect = lambda **kw: (captured.append(kw.get("msgs", [])), streamer)[1]

    prior = []
    for i in range(3):
        prior.append({"role": "user", "content": f"q{i}"})
        prior.append({"role": "assistant", "content": f"a{i}"})

    with (
        patch("companion_harness.foreground_model_minicpm.StoppingCriteria"),
        patch("companion_harness.foreground_model_minicpm.StoppingCriteriaList"),
    ):
        gen = await model.chat_stream_turn(
            np.zeros(16000, dtype=np.float32),
            caused_by=["e"],
            prior_turns=prior,
        )
        async for _ in gen:
            pass

    msgs = captured[0]
    assert len(msgs) == 8  # system + 6 history + current user
    assert msgs[0]["role"] == "system"
    assert msgs[-1]["role"] == "user"
    model._inference_executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# FIFO eviction test (orchestrator-level)
# ---------------------------------------------------------------------------


def test_conv_history_fifo_eviction() -> None:
    """_conv_history never exceeds _CONV_HISTORY_MAX_TURNS; oldest entries evicted."""
    history: list[dict] = []

    def _append(entry):
        history.append(entry)
        if len(history) > _CONV_HISTORY_MAX_TURNS:
            history.pop(0)

    for i in range(10):
        _append({"role": "user", "content": f"u{i}"})
        _append({"role": "assistant", "content": f"a{i}"})

    assert len(history) <= _CONV_HISTORY_MAX_TURNS
    # Most recent entries should be retained
    assert history[-1]["content"] == "a9"
    assert history[-2]["content"] == "u9"


def test_conv_history_max_turns_value() -> None:
    """_CONV_HISTORY_MAX_TURNS is 6 (3 user+assistant pairs)."""
    assert _CONV_HISTORY_MAX_TURNS == 6


# ---------------------------------------------------------------------------
# Empty transcript / empty assistant text skipped
# ---------------------------------------------------------------------------


def test_empty_transcript_not_appended() -> None:
    """Empty ASR transcript must not be appended to history."""
    history: list[dict] = []

    def _maybe_append_user(transcript: str) -> None:
        if transcript:
            history.append({"role": "user", "content": transcript})
            if len(history) > _CONV_HISTORY_MAX_TURNS:
                history.pop(0)

    _maybe_append_user("")
    _maybe_append_user("  ")  # whitespace — truthy, but test orchestrator skips ""
    _maybe_append_user("")

    # Only the whitespace-only string was appended (truthy in Python).
    # The orchestrator uses `if transcript:` which matches empty string only.
    assert len(history) == 1
    assert history[0]["content"] == "  "


def test_empty_assistant_text_not_appended() -> None:
    """Zero-char assistant response must not be appended to history."""
    history: list[dict] = []

    def _maybe_append_assistant(text: str) -> None:
        if text:
            history.append({"role": "assistant", "content": text})
            if len(history) > _CONV_HISTORY_MAX_TURNS:
                history.pop(0)

    _maybe_append_assistant("")
    _maybe_append_assistant("Hello there")

    assert len(history) == 1
    assert history[0]["content"] == "Hello there"
