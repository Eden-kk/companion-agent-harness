"""Hermetic tests for gpt_stream_runner.  No live API calls."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from companion_harness.evals.adapters.gpt_stream_runner import (
    ContinuousTransport,
    _REASONING_HEADROOM,
    _SEAM_STATES,
    run_case,
)
from companion_harness.evals.adapters.tact_bench_stream_types import Emission


# ---------------------------------------------------------------------------
# FakeContinuousTransport
# ---------------------------------------------------------------------------

@dataclass
class FakeContinuousTransport(ContinuousTransport):
    """Scripts exact responses for each commit_and_respond call.

    responses: list of (response_text, onset_offset_secs)
        onset_offset_secs is added to time.time() at call time to simulate
        a realistic onset timestamp.  Pass 0 for "immediate".
    """
    responses: list[tuple[str, float]] = field(default_factory=list)
    _calls: list[dict] = field(default_factory=list)
    _injected: list[str] = field(default_factory=list)
    _audio: list[str] = field(default_factory=list)
    _stream_start: float = field(default=0.0)
    _call_idx: int = field(default=0, init=False)
    _connected: bool = field(default=False, init=False)

    async def connect(self, instructions: str) -> None:
        self._connected = True
        self._stream_start = time.time()

    async def append_audio(self, b64: str) -> None:
        self._audio.append(b64)

    async def inject_note(self, text: str) -> None:
        self._injected.append(text)

    async def commit_and_respond(self, max_output_tokens: int) -> tuple[str, str, float]:
        if self._call_idx >= len(self.responses):
            return "", "", 0.0
        resp_text, offset = self.responses[self._call_idx]
        self._call_idx += 1
        ts = time.time() + offset if resp_text else 0.0
        return resp_text, "response.created" if resp_text else "", ts

    async def close(self) -> None:
        pass


def _make_transport_factory(responses: list[tuple[str, float]]):
    """Return a factory that always yields the same scripted transport instance."""
    instance = FakeContinuousTransport(responses=responses)
    return lambda: instance, instance


# ---------------------------------------------------------------------------
# Minimal fake timeline / case fixtures
# ---------------------------------------------------------------------------

def _fake_build_timeline(case_id, ticks=4, t_avail=2, payload="deploy 2.4.1"):
    """Return a 4-tick Timeline with one pending item injected at tick t_avail."""
    from companion_harness.evals.adapters.scenario_timeline import Tick, Timeline
    from companion_harness.evals.adapters.scenario_timeline import PENDING_TEMPLATE

    note = PENDING_TEMPLATE.format(source="ci", payload=payload)
    tick_list = []
    for t in range(ticks):
        tk = Tick(t=t, kind="speech", text=f"turn {t}")
        if t == t_avail:
            tk.pending_note = note
        tick_list.append(tk)
    return Timeline(case_id=case_id, ticks=tick_list)


def _fake_l3_case(case_id="TC1", ticks=4, t_avail=2):
    return {
        "id": case_id,
        "ticks": ticks,
        "item": {"id": "I1", "t_avail": t_avail},
    }


def _fake_items(t_avail=2, payload="deploy 2.4.1"):
    return [{"id": "I1", "t_avail": t_avail, "payload": payload, "source": "ci"}]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _fake_l3_obj(case_id, ticks, user_state=None):
    """Minimal Layer3Case-like object for patching load_layer3."""
    obj = MagicMock()
    obj.id = case_id
    obj.user_state = user_state if user_state is not None else ["i"] * ticks
    return obj


def _run(case_id, arm, transport_factory, responses=None, ticks=4, t_avail=2,
         payload="deploy 2.4.1", user_state=None):
    """Patch the data-loading functions and run run_case with a fake transport."""
    fake_timeline = _fake_build_timeline(case_id, ticks=ticks, t_avail=t_avail,
                                          payload=payload)
    l3_case = _fake_l3_case(case_id, ticks=ticks, t_avail=t_avail)
    items = _fake_items(t_avail=t_avail, payload=payload)
    l3_obj = _fake_l3_obj(case_id, ticks, user_state=user_state)

    # patch all data-loading so no YAML files are touched
    with (
        patch("companion_harness.evals.adapters.gpt_stream_runner.build_timeline",
              return_value=fake_timeline),
        patch("companion_harness.evals.adapters.gpt_stream_runner.yaml") as mock_yaml,
        patch("companion_harness.evals.adapters.gpt_stream_runner._find_l3",
              return_value=l3_case),
        patch("companion_harness.evals.adapters.gpt_stream_runner._normalize_items",
              return_value=items),
        patch("companion_harness.evals.adapters.gpt_stream_runner.load_layer3",
              return_value=[l3_obj]),
    ):
        mock_yaml.safe_load.return_value = {"cases": [], "scenarios": []}
        return run_case(
            case_id,
            arm,
            realtime=False,
            transport_factory=transport_factory,
        )


def test_no_response_produces_no_emissions():
    factory, inst = _make_transport_factory([])
    emissions = _run("TC1", "vanilla", factory)
    assert emissions == []


def test_spoken_response_creates_emission_with_spoke_true():
    # t_avail=2 (default); ticks 0,1 are gated out; first trigger at tick 2
    factory, inst = _make_transport_factory([
        ("deploy 2.4.1 is done", 0.0),  # tick 2: first eligible seam
        ("", 0.0),                       # tick 3
    ])
    emissions = _run("TC1", "prompted", factory)
    assert len(emissions) == 1
    assert emissions[0].spoke is True
    assert "deploy" in emissions[0].text.lower()


def test_onset_tick_equals_seam_tick():
    """Onset tick must equal the seam tick where response.create was issued."""
    # user_state: ticks 0,1 are 'm' (no seam), tick 2 is 'b' (seam), tick 3 is 'i'
    user_state = ["m", "m", "b", "i"]
    # response fires at tick 2 (first seam); onset tick must be 2
    factory, inst = _make_transport_factory([
        ("deploy 2.4.1 is ready", 0.0),  # first seam (tick 2)
        ("", 0.0),                        # second seam (tick 3)
    ])
    emissions = _run("TC1", "prompted", factory, ticks=4, user_state=user_state)
    assert len(emissions) == 1
    assert emissions[0].tick == 2


def test_pending_note_injected_at_t_avail():
    """inject_note must be called exactly once, at tick t_avail."""
    factory, inst = _make_transport_factory([("", 0.0)] * 4)
    _run("TC1", "prompted", factory, t_avail=2)
    assert len(inst._injected) == 1
    assert "deploy 2.4.1" in inst._injected[0]


def test_no_trigger_at_m_or_h_ticks():
    """commit_and_respond must NOT be called at m or h ticks."""
    # ticks 0,1,3 are m/h; only tick 2 (b) is a seam
    user_state = ["m", "h", "b", "m"]
    commit_calls = []

    class CountingTransport(ContinuousTransport):
        async def connect(self, instructions): pass
        async def append_audio(self, b64): pass
        async def inject_note(self, text): pass
        async def commit_and_respond(self, max_output_tokens):
            commit_calls.append(max_output_tokens)
            return "", "", 0.0
        async def close(self): pass

    _run("TC1", "vanilla", CountingTransport, ticks=4, user_state=user_state)
    # only tick 2 (b) triggers a response
    assert len(commit_calls) == 1


def test_note_injected_before_seam_commit():
    """Note injected at t_avail=2; the seam (b) is tick 2 — note must precede commit."""
    user_state = ["m", "m", "b", "i"]
    events = []

    class OrderCapture(ContinuousTransport):
        async def connect(self, instructions): pass
        async def append_audio(self, b64): pass
        async def inject_note(self, text):
            events.append("inject")
        async def commit_and_respond(self, max_output_tokens):
            events.append("commit")
            return "", "", 0.0
        async def close(self): pass

    _run("TC1", "prompted", OrderCapture, ticks=4, t_avail=2, user_state=user_state)
    # inject must appear before first commit
    assert "inject" in events
    assert events.index("inject") < events.index("commit")


def test_annotate_emissions_integration():
    """When the text matches the payload, detected_item is populated.

    Payload uses words that produce lexical F1 >= T_LEX (0.35) when the
    utterance contains those same content words.
    """
    payload = "build finished successfully server ready"
    utterance = "The build finished successfully, the server is ready now."

    fake_timeline = _fake_build_timeline("TC1", ticks=4, t_avail=2, payload=payload)
    l3_case = _fake_l3_case("TC1", ticks=4, t_avail=2)
    items = _fake_items(t_avail=2, payload=payload)

    call_count = [0]
    class CommitTransport(ContinuousTransport):
        async def connect(self, instructions): pass
        async def append_audio(self, b64): pass
        async def inject_note(self, text): pass
        async def commit_and_respond(self, max_output_tokens):
            call_count[0] += 1
            if call_count[0] == 1:  # first eligible seam (tick 2 = t_avail)
                return utterance, "response.created", time.time() + 2.1
            return "", "", 0.0
        async def close(self): pass

    l3_obj = _fake_l3_obj("TC1", 4)

    with (
        patch("companion_harness.evals.adapters.gpt_stream_runner.build_timeline",
              return_value=fake_timeline),
        patch("companion_harness.evals.adapters.gpt_stream_runner.yaml") as mock_yaml,
        patch("companion_harness.evals.adapters.gpt_stream_runner._find_l3",
              return_value=l3_case),
        patch("companion_harness.evals.adapters.gpt_stream_runner._normalize_items",
              return_value=items),
        patch("companion_harness.evals.adapters.gpt_stream_runner.load_layer3",
              return_value=[l3_obj]),
    ):
        mock_yaml.safe_load.return_value = {"cases": [], "scenarios": []}
        emissions = run_case(
            "TC1", "prompted",
            realtime=False,
            transport_factory=CommitTransport,
        )

    spoken = [e for e in emissions if e.spoke]
    assert spoken, "expected at least one spoken emission"
    assert spoken[0].detected_item == "I1"


def test_no_trigger_before_first_t_avail():
    """Seam ticks before the earliest t_avail must NOT call commit_and_respond."""
    # 6 ticks; all seams (i); item available at t=4; seams 0-3 must not trigger
    user_state = ["i"] * 6
    commit_ticks = []

    class TickCapture(ContinuousTransport):
        _tick = -1
        async def connect(self, instructions): pass
        async def append_audio(self, b64):
            # track current tick by counting audio appends
            TickCapture._tick += 1
        async def inject_note(self, text): pass
        async def commit_and_respond(self, max_output_tokens):
            commit_ticks.append(TickCapture._tick)
            return "", "", 0.0
        async def close(self): pass

    fake_timeline = _fake_build_timeline("TC4", ticks=6, t_avail=4)
    l3_case = _fake_l3_case("TC4", ticks=6, t_avail=4)
    items = _fake_items(t_avail=4)
    l3_obj = _fake_l3_obj("TC4", 6, user_state=user_state)

    with (
        patch("companion_harness.evals.adapters.gpt_stream_runner.build_timeline",
              return_value=fake_timeline),
        patch("companion_harness.evals.adapters.gpt_stream_runner.yaml") as mock_yaml,
        patch("companion_harness.evals.adapters.gpt_stream_runner._find_l3",
              return_value=l3_case),
        patch("companion_harness.evals.adapters.gpt_stream_runner._normalize_items",
              return_value=items),
        patch("companion_harness.evals.adapters.gpt_stream_runner.load_layer3",
              return_value=[l3_obj]),
    ):
        mock_yaml.safe_load.return_value = {"cases": [], "scenarios": []}
        run_case("TC4", "vanilla", realtime=False, transport_factory=TickCapture)

    assert all(t >= 4 for t in commit_ticks), (
        f"commit triggered before t_avail=4: {commit_ticks}"
    )
    assert len(commit_ticks) >= 1, "expected at least one trigger at t>=4"


def test_unknown_arm_raises():
    with pytest.raises(ValueError, match="unknown arm"):
        run_case("TC1", "nonexistent_arm",
                 transport_factory=lambda: FakeContinuousTransport([]))


def test_reasoning_headroom_applied():
    """commit_and_respond must receive max_tokens = caller_budget + headroom."""
    received_tokens = []

    class TokenCapture(ContinuousTransport):
        async def connect(self, instructions): pass
        async def append_audio(self, b64): pass
        async def inject_note(self, text): pass
        async def commit_and_respond(self, max_output_tokens):
            received_tokens.append(max_output_tokens)
            return "", "", 0.0
        async def close(self): pass

    fake_timeline = _fake_build_timeline("TC1", ticks=2, t_avail=1)
    l3_case = _fake_l3_case("TC1", ticks=2, t_avail=1)
    items = _fake_items(t_avail=1)
    l3_obj = _fake_l3_obj("TC1", 2)

    with (
        patch("companion_harness.evals.adapters.gpt_stream_runner.build_timeline",
              return_value=fake_timeline),
        patch("companion_harness.evals.adapters.gpt_stream_runner.yaml") as mock_yaml,
        patch("companion_harness.evals.adapters.gpt_stream_runner._find_l3",
              return_value=l3_case),
        patch("companion_harness.evals.adapters.gpt_stream_runner._normalize_items",
              return_value=items),
        patch("companion_harness.evals.adapters.gpt_stream_runner.load_layer3",
              return_value=[l3_obj]),
    ):
        mock_yaml.safe_load.return_value = {"cases": [], "scenarios": []}
        run_case("TC1", "vanilla", realtime=False,
                 transport_factory=TokenCapture, max_response_tokens=40)

    assert all(t == 40 + _REASONING_HEADROOM for t in received_tokens)
