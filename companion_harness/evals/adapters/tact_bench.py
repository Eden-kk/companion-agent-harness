"""TACT-Bench adapter — held-result delivery ("when an always-on agent should stay silent").

Generalized eval-framework adapter (eval-subsystem-spec.md Anchor 5). Built per
docs/plan-tact-bench-minicpm-adapter.md. Only the ScenarioDriver (PR2) is
MiniCPM-coupled; this CaseSource — and the judge/metrics — are model-agnostic.

Scenario definitions are authored upstream in the tact-bench repo
(experiments/vanilla-vs-prompted/scenarios.yaml) and vendored here under
``tact_bench_data/scenarios.yaml`` so the adapter runs CI-hermetically without that
checkout. Keep the two in sync (tact-bench REVISIONS R3).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from companion_harness.event_logger import EventLogger
from companion_harness.schemas import EvaluationCase, Event, ReplayRun

_DATA_DIR = Path(__file__).parent / "tact_bench_data"
_DEFAULT_SCENARIOS = _DATA_DIR / "scenarios.yaml"

_BENCH_NAME = "tact_bench"
_BENCH_VERSION = "v1"


@dataclass
class TactCaseSource:
    """Loads the TACT-Bench scenario set into typed ``EvaluationCase``s.

    Model-agnostic: no model SDK import. ``input_mode`` is recorded on each case
    so the (model-specific) driver knows whether to feed audio or text turns.
    """

    scenarios_path: Path = _DEFAULT_SCENARIOS
    input_mode: str = "text"
    name: str = _BENCH_NAME
    version: str = _BENCH_VERSION

    def iter_cases(self, split: str = "all") -> Iterable[EvaluationCase]:
        import yaml  # lazy: keep module import free of the yaml dependency

        data = yaml.safe_load(self.scenarios_path.read_text())
        for scenario in data["scenarios"]:
            yield self._to_case(scenario)

    def _to_case(self, scenario: dict) -> EvaluationCase:
        item = scenario.get("item") or {}
        return EvaluationCase(
            case_id=scenario["id"],
            stage=3,  # Stage 3 — speak/silence policy
            scenario=scenario.get("behavior", scenario["id"]),
            modalities=[self.input_mode],
            fixture_ref=f"tact/{scenario['id']}",
            # Eval bookkeeping events the driver actually emits (per-chunk model
            # behavior is recorded in ReplayRun.results, not as events).
            expected_events=[
                "benchmark_case_started",
                "held_result_injected",
                "benchmark_case_completed",
            ],
            expected_metrics={},
            consent_class="safe_eval_fixture",
            benchmark_name=self.name,
            benchmark_version=self.version,
            inputs={
                "user_script": scenario.get("user_script", []),
                "item": item,
                "input_mode": self.input_mode,
                "current_topic": scenario.get("current_topic"),
                "standing_instruction": scenario.get("standing_instruction"),
            },
            expected_behavior={
                "behavior": scenario.get("behavior"),
                "exposes": list(scenario.get("exposes", [])),
                "t_available": item.get("t_available"),
                "becomes_stale_at": item.get("becomes_stale_at"),
                "ground_truth": scenario.get("ground_truth", ""),
            },
            fixtures=[],
        )


# ===========================================================================
# Driver  [MODEL-SPECIFIC — the only piece that touches the model SDK]
# See docs/subplan-tact-pr2-driver.md.
# ===========================================================================

_CHUNK_SAMPLES = 16000  # 1.0s @ 16 kHz — the silence-clock tick
_TAIL_CHUNKS = 6
_SCHEMA_VERSION = "0.1"
_SOURCE = "tact_minicpm_driver"

_VANILLA_SYSTEM_PROMPT = "You are a helpful real-time voice assistant. Respond naturally to the user."

_PROMPTED_SYSTEM_PROMPT = (
    "You are an always-on voice assistant sharing a single audio channel with the user.\n"
    "From time to time you privately receive a background result as a system note (for\n"
    "example a tool result, a finished task, or a reminder). A system note is NOT something\n"
    "the user said, and you must never read the note or its labels aloud verbatim — speak\n"
    "only the underlying information, and only when appropriate.\n\n"
    "For each background result, decide each moment:\n"
    "- DELIVER NOW only if it is urgent OR the user explicitly asked to be told the moment\n"
    "  it's ready. When you deliver mid-conversation, be brief.\n"
    "- DEFER (stay silent now, deliver at the next natural pause) if it is relevant but not\n"
    "  urgent. When you finally deliver a deferred item, re-anchor it (\"about that X you\n"
    "  asked earlier...\").\n"
    "- DROP (never mention it) if it is no longer relevant, the user already resolved it, or\n"
    "  it has gone stale.\n"
    "Do not interrupt the user mid-sentence for anything non-urgent. Staying silent is often\n"
    "the correct choice. Match length to urgency: urgent -> one short sentence; otherwise brief."
)

_ARMS = {"vanilla": _VANILLA_SYSTEM_PROMPT, "prompted": _PROMPTED_SYSTEM_PROMPT}


def _held_result_turn(item: dict) -> str:
    """Held result as a private ChatML system turn (tact-bench REVISIONS R1+R2)."""
    payload = (item.get("payload") or "").strip()
    urgent = (item.get("labels") or {}).get("urgency") == "high"
    if urgent:
        payload = re.sub(r"^urgent[:\s\-]*", "", payload, flags=re.IGNORECASE).strip()
        result = f"URGENT: {payload}"
    else:
        result = payload
    note = (
        "Background result now available (private system note — not user speech; "
        f"do not read this note aloud verbatim). Result: {result}."
    )
    return f"<|im_start|>system\n{note}<|im_end|>\n"


def _user_turn(text: str) -> str:
    return f"<|im_start|>user\n{text}<|im_end|>\n"


def _est_chunks(text: str) -> int:
    return max(1, round(len(text.split()) / 3))


def _build_text_plan(inputs: dict) -> tuple[dict[int, str], int, list[bool]]:
    """Text-input plan: {chunk -> user-turn text}, n_chunks, user-speaking mask.

    No audio; the held result is injected separately at t_available. The mask is
    structural: a speaker entry occupies ~_est_chunks ticks (capped at the next
    entry), leaving silent ticks as breakpoints — the role silence plays in audio.
    """
    user_script = inputs.get("user_script", [])
    item = inputs.get("item") or {}
    entries = sorted(user_script, key=lambda e: int(e["t"]))
    t_avail = int(item.get("t_available", 0))
    text_turns: dict[int, str] = {}
    speaking: set[int] = set()
    last = 0
    for i, e in enumerate(entries):
        t = int(e["t"])
        nxt = int(entries[i + 1]["t"]) if i + 1 < len(entries) else None
        if e.get("type") == "pause":
            last = max(last, t + int(e.get("dur", 1)))
            continue
        text_turns[t] = _user_turn(e["text"])
        end_speak = t + _est_chunks(e["text"])
        if nxt is not None:
            end_speak = min(end_speak, nxt)
        speaking.update(range(t, end_speak))
        last = max(last, t + _est_chunks(e["text"]))
    n_chunks = max(last, t_avail + 1) + _TAIL_CHUNKS
    mask = [c in speaking for c in range(n_chunks)]
    return text_turns, n_chunks, mask


def _make_event(
    event_id: str,
    event_type: str,
    caused_by: list[str],
    session_id: str,
    seq_no: int,
    mono_ms: int,
    payload_kind: str = "signal",
    sensitivity: str = "safe",
    extra_hash: str = "",
) -> Event:
    payload_hash = hashlib.sha256(f"{event_type}:{event_id}:{extra_hash}".encode()).hexdigest()[:16]
    return Event(
        event_id=event_id,
        session_id=session_id,
        schema_version=_SCHEMA_VERSION,
        seq_no=seq_no,
        event_type=event_type,
        timestamp_mono_ms=mono_ms,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
        source=_SOURCE,
        caused_by=caused_by,
        payload_hash=payload_hash,
        payload_ref=None,
        payload_kind=payload_kind,  # type: ignore[arg-type]
        subject_class="self",
        sensitivity=sensitivity,  # type: ignore[arg-type]
        retention_policy_id="eval_run_30d",
    )


def _generate(duplex: object) -> dict:
    """Call streaming_generate with the model's configured params (defaults if absent)."""
    g = lambda n, d: getattr(duplex, n, d)  # noqa: E731
    return duplex.streaming_generate(  # type: ignore[attr-defined]
        max_new_speak_tokens_per_chunk=g("max_new_speak_tokens_per_chunk", 20),
        temperature=g("temperature", 0.7),
        top_k=g("top_k", 0),
        top_p=g("top_p", 0.9),
        listen_prob_scale=g("listen_prob_scale", 1.0),
        text_repetition_penalty=g("text_repetition_penalty", 1.0),
        text_repetition_window_size=g("text_repetition_window_size", 0),
    )


def _load_real_model() -> object:
    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel  # noqa: WPS433

    return MiniCPMStreamingModel()


@dataclass
class TactMiniCPMDriver:
    """Drives one TACT case through MiniCPM-o in text/silence-clock mode.

    The ONLY model-coupled piece (CLAUDE.md adapter-first). 1s of silence per tick
    is the native duplex clock; user turns + the held result ride the text channel
    (no speech audio to echo). Per-chunk is_listen/text go into ReplayRun.results;
    only bookkeeping (started / held_result_injected / completed) is emitted as Events.
    """

    arm: str = "prompted"
    input_mode: str = "text"
    model_factory: object | None = None  # () -> model; injectable for tests (no GPU)

    def __post_init__(self) -> None:
        if self.arm not in _ARMS:
            raise ValueError(f"unknown arm {self.arm!r}; expected one of {sorted(_ARMS)}")
        self._model: object | None = None
        self._seq = 0

    def _model_or_load(self) -> object:
        if self._model is None:
            self._model = self.model_factory() if self.model_factory is not None else _load_real_model()
        return self._model

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def run(self, case: EvaluationCase, harness_factory: object, run_config: object) -> ReplayRun:
        if self.input_mode != "text":
            raise NotImplementedError("TactMiniCPMDriver: only input_mode='text' is implemented (audio = PR7)")

        import numpy as np  # noqa: WPS433 — lazy; keeps CaseSource import light

        self._seq = 0
        inputs = case.inputs or {}
        item = inputs.get("item") or {}
        t_avail = int(item.get("t_available", -1))
        session_id = f"tact-{case.case_id}-{uuid.uuid4().hex[:8]}"
        started_wall = datetime.now(timezone.utc).isoformat()
        started_ms = int(time.monotonic() * 1000)
        run_id = f"tact-{case.case_id}-{started_ms}"

        # Invariant #10: emit through the async, non-blocking EventLogger.
        collected: list[Event] = []

        async def _sink(evt: Event) -> None:
            collected.append(evt)

        logger = EventLogger(sink=_sink)
        await logger.start()

        trajectory: list[dict] = []
        injected_evt_id: str | None = None
        final_status = "completed"
        failures: list[dict] = []
        try:
            start_id = f"{session_id}-start"
            logger.log(_make_event(
                event_id=start_id, event_type="benchmark_case_started",
                caused_by=[], session_id=session_id, seq_no=self._next_seq(), mono_ms=started_ms,
            ))

            text_turns, n_chunks, mask = _build_text_plan(inputs)
            note = _held_result_turn(item)

            duplex = self._model_or_load()._duplex  # noqa: SLF001
            duplex.model.reset_session(reset_token2wav_cache=False)
            duplex.prepare(prefix_system_prompt=_ARMS[self.arm])
            silence = np.zeros(_CHUNK_SAMPLES, dtype=np.float32)

            try:
                for t in range(n_chunks):
                    texts: list[str] = []
                    if t in text_turns:
                        texts.append(text_turns[t])
                    injected = t == t_avail
                    if injected:
                        texts.append(note)
                        injected_evt_id = f"{session_id}-inject-{t}"
                        logger.log(_make_event(
                            event_id=injected_evt_id, event_type="held_result_injected",
                            caused_by=[start_id], session_id=session_id,
                            seq_no=self._next_seq(), mono_ms=int(time.monotonic() * 1000),
                            payload_kind="transcript", sensitivity="sensitive",
                            extra_hash=note,  # only the hash is stored, never the raw note
                        ))
                    if texts:
                        duplex.streaming_prefill(audio_waveform=silence, text_list=["".join(texts)])
                    else:
                        duplex.streaming_prefill(audio_waveform=silence)
                    result = _generate(duplex)
                    trajectory.append({
                        "t": t,
                        "injected": injected,
                        "is_listen": bool(result.get("is_listen", True)),
                        "text": result.get("text", ""),
                    })
            except Exception as exc:  # noqa: BLE001 — record a failed run, don't crash the suite
                final_status = "error"
                failures = [{"error": f"{type(exc).__name__}: {exc}"}]

            done_caused = [start_id] + ([injected_evt_id] if injected_evt_id else [])
            logger.log(_make_event(
                event_id=f"{session_id}-done", event_type="benchmark_case_completed",
                caused_by=done_caused, session_id=session_id,
                seq_no=self._next_seq(), mono_ms=int(time.monotonic() * 1000),
            ))
        finally:
            await logger.stop()  # drains the queue into `collected`

        return self._finish(case, run_id, session_id, started_wall, started_ms, collected,
                            trajectory, mask, run_config, final_status, failures=failures)

    def _finish(self, case, run_id, session_id, started_wall, started_ms, events, trajectory,
                mask, run_config, final_status, failures) -> ReplayRun:
        event_log_path = self._write_event_log(run_config, session_id, events)
        return ReplayRun(
            run_id=run_id,
            case_id=case.case_id,
            implementation_config_version="tact_minicpm_driver_v1",
            policy_version=f"arm:{self.arm}",
            started_at=started_wall,
            finished_at=datetime.now(timezone.utc).isoformat(),
            results={
                "arm": self.arm,
                "input_mode": self.input_mode,
                "trajectory": trajectory,
                "speaking_mask": mask,
                "item": case.inputs.get("item") if case.inputs else None,
                "expected_behavior": case.expected_behavior,
                "events": [asdict(e) for e in events],
            },
            failures=failures,
            event_log_path=event_log_path,
            timing_mode="wall_clock",
            started_at_mono_ms=started_ms,
            completed_at_mono_ms=int(time.monotonic() * 1000),
            final_status=final_status,  # type: ignore[arg-type]
        )

    @staticmethod
    def _write_event_log(run_config: object, session_id: str, events: list[Event]) -> Path | None:
        output_dir = getattr(run_config, "output_dir", None)
        if output_dir is None:
            return None
        log_dir = Path(output_dir) / "event_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{session_id}.jsonl"
        with path.open("w") as f:
            for e in events:
                f.write(json.dumps(asdict(e)) + "\n")
        return path
