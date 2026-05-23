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
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

from companion_harness.event_logger import EventLogger
from companion_harness.evals.protocols import BenchmarkAdapter
from companion_harness.evals.schemas import BenchmarkResult, FailureSlice, MetricValue
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
                "expected_form": scenario.get("expected_form"),
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


# --- Mode A (structured NOW/WAIT/DROP probe) helpers -----------------------

def _transcript_through(user_script: list, t: int) -> str:
    lines = [
        f"user: {e['text']}"
        for e in user_script
        if e.get("speaker") == "user" and e.get("text") and int(e["t"]) <= t
    ]
    return "\n".join(lines) if lines else "(no user speech yet)"


def _mode_a_prompt(history: str, item: dict, user_speaking: bool) -> str:
    payload = item.get("payload", "")
    urgency = (item.get("labels") or {}).get("urgency", "low")
    state = (
        "The user is currently speaking (mid-utterance)."
        if user_speaking
        else "The user is currently silent — a natural pause."
    )
    return (
        "You are an always-on voice assistant deciding when to surface a background result.\n"
        f"Conversation so far:\n{history}\n\n"
        f"{state}\n"
        f'You are holding this background result to deliver: "{payload}" (urgency: {urgency}).\n'
        "Should you deliver it NOW, WAIT for a better moment, or DROP it as no longer worth saying?\n"
        "Answer with exactly one word: NOW, WAIT, or DROP."
    )


def _parse_now_wait_drop(raw: str) -> str:
    up = (raw or "").upper()
    for token in ("NOW", "WAIT", "DROP"):
        if token in up:
            return token
    return "UNKNOWN"


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

    def mode_a_labels(self, case: EvaluationCase) -> list[dict]:
        """Mode-A structured probe: per-decision-point NOW/WAIT/DROP labels.

        From t_available onward, ask the model — given the conversation so far, the
        held item, and whether the user is mid-utterance — to emit one of
        NOW/WAIT/DROP, stopping once it resolves (NOW/DROP). Tests whether deferral
        is *elicitable* under structured prompting even though the native gate
        (Mode B) never defers. Prompt-constrained (one-word answer), not
        logit-constrained decode — a future refinement.
        """
        inputs = case.inputs or {}
        item = inputs.get("item") or {}
        t_avail = int(item.get("t_available", 0))
        user_script = inputs.get("user_script") or []
        _, n_chunks, mask = _build_text_plan(inputs)
        model = self._model_or_load()

        labels: list[dict] = []
        for t in range(max(t_avail, 0), n_chunks):
            speaking = mask[t] if t < len(mask) else False
            prompt = _mode_a_prompt(_transcript_through(user_script, t), item, speaking)
            decision = _parse_now_wait_drop(model.chat(prompt, max_new_tokens=4))
            labels.append({"t": t, "decision": decision, "user_speaking": speaking})
            if decision in ("NOW", "DROP"):
                break
        return labels

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


# ===========================================================================
# Examiner — Mode-B delivery judge (model-agnostic; OpenAI-backed)
# plan-tact-bench-minicpm-adapter.md PR3.
# ===========================================================================

_JUDGE_SYSTEM = (
    "You are a strict evaluator of a real-time voice assistant that shares one audio "
    "channel with a user. The assistant also privately received a HELD RESULT (a "
    "background fact) at a known moment and may speak it, defer it, or stay silent. "
    "Given the user's speech and the assistant's per-chunk spoken text, decide whether "
    "and how the assistant DELIVERED the held result's information.\n\n"
    "Rules:\n"
    "- 'Delivered' = the assistant conveyed the HELD RESULT's actual information to the "
    "user (paraphrase counts).\n"
    "- The assistant merely repeating or continuing the USER's own words is NOT a "
    "delivery — set echo_only=true if its speech is just parroting the user.\n"
    "- first_delivery_chunk = the integer t of the first assistant chunk that conveys "
    "the held result, or null.\n"
    "- form = 'BRIEF' (a single short sentence/phrase) or 'FULL' (multi-sentence / "
    "verbose) for the delivery, else null.\n"
    "Return ONLY a JSON object with keys: delivered (bool), first_delivery_chunk "
    "(int|null), form ('BRIEF'|'FULL'|null), echo_only (bool), rationale (string, "
    "<=25 words)."
)


def _pending_label(reason: str = "judge unavailable (no OPENAI_API_KEY)") -> dict:
    """Label used when the judge cannot run — metrics treat delivered=None as PENDING."""
    return {
        "delivered": None,
        "first_delivery_chunk": None,
        "form": None,
        "echo_only": None,
        "rationale": reason,
    }


@dataclass
class DeliveryJudge:
    """LLM delivery judge behind the Examiner seam (Mode-B native gate scoring).

    Satisfies the Examiner Protocol (``respond``); the runner may also call
    ``label()`` directly with a trajectory. Skips cleanly (PENDING label) when no
    client is available, and caches verdicts by (trajectory, item, user_script) so
    re-scoring is deterministic and free.
    """

    model: str = "gpt-4o"
    client_factory: object | None = None  # () -> OpenAI-like client; injectable for tests
    name: str = "tact_delivery_judge"

    def __post_init__(self) -> None:
        self._client: object | None = None
        self._client_resolved = False
        self._cache: dict[str, dict] = {}

    def _client_or_none(self) -> object | None:
        if not self._client_resolved:
            if self.client_factory is not None:
                self._client = self.client_factory()
            elif os.environ.get("OPENAI_API_KEY"):
                from openai import OpenAI  # noqa: WPS433 — optional dep, lazy

                self._client = OpenAI()
            else:
                self._client = None
            self._client_resolved = True
        return self._client

    async def respond(self, event: Event) -> object:
        """Examiner seam: label from an event carrying trajectory/item/user_script."""
        payload = getattr(event, "payload_inline", None) or {}
        return self.label(
            payload.get("trajectory") or [],
            payload.get("item") or {},
            payload.get("user_script"),
        )

    def label(self, trajectory: list[dict], item: dict, user_script: list | None = None) -> dict:
        key = self._cache_key(trajectory, item, user_script)
        if key in self._cache:
            return self._cache[key]
        client = self._client_or_none()
        label = _pending_label() if client is None else self._call(client, trajectory, item, user_script)
        self._cache[key] = label
        return label

    @staticmethod
    def _cache_key(trajectory: list[dict], item: dict, user_script: list | None) -> str:
        blob = json.dumps({"t": trajectory, "i": item, "u": user_script or []}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    def _call(self, client: object, trajectory: list[dict], item: dict, user_script: list | None) -> dict:
        payload = item.get("payload", "")
        urgency = (item.get("labels") or {}).get("urgency", "low")
        t_avail = int(item.get("t_available", -1))
        user_lines = [
            f"  t={e['t']}: {e['text']}"
            for e in (user_script or [])
            if e.get("speaker") == "user" and e.get("text")
        ] or ["  (none provided)"]
        spoke = [(c["t"], c["text"].strip()) for c in trajectory if not c["is_listen"] and c["text"].strip()]
        spoke_lines = [f"  t={t}: {txt}" for t, txt in spoke] or ["  (the assistant never spoke)"]
        user_msg = (
            f"HELD RESULT (urgency={urgency}): {payload}\n"
            f"The held result became available at chunk t={t_avail}.\n\n"
            "USER said:\n" + "\n".join(user_lines) + "\n\n"
            "ASSISTANT spoke (only chunks where it spoke):\n" + "\n".join(spoke_lines) + "\n\n"
            "Did the assistant deliver the held result? Respond as specified."
        )
        resp = client.chat.completions.create(  # type: ignore[attr-defined]
            model=self.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _JUDGE_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
        fdc = data.get("first_delivery_chunk")
        return {
            "delivered": bool(data.get("delivered", False)),
            "first_delivery_chunk": fdc if isinstance(fdc, int) else None,
            "form": data.get("form") if data.get("form") in ("BRIEF", "FULL") else None,
            "echo_only": bool(data.get("echo_only", False)),
            "rationale": str(data.get("rationale", ""))[:200],
        }

    @staticmethod
    def dump_labels(labels: dict, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(labels, indent=2))


# ===========================================================================
# Metrics  (model-agnostic) — plan-tact-bench-minicpm-adapter.md PR4
#
# Each Metric implements the per-case Metric.compute(replay_run) -> MetricValue
# seam, emitting its per-case contribution (in_scope + num/den or pending).
# aggregate_tact_metrics() combines those contributions across all cases of one
# arm into the headline rates — the cross-case aggregation point the framework
# defers to the reporter (PR5 calls it). A delivery only counts if
# first_delivery_chunk >= t_available (pre-availability matches are phantom).
# ===========================================================================

_PENDING = ("pending",)


def _first_breakpoint_at_or_after(mask: list[bool], t: int) -> "int | None":
    for i in range(max(t, 0), len(mask)):
        if not mask[i]:
            return i
    return None


def _case_facts(replay_run: ReplayRun) -> SimpleNamespace:
    r = replay_run.results or {}
    eb = r.get("expected_behavior") or {}
    label = r.get("judge_label")
    mask = r.get("speaking_mask") or []
    t_avail = eb.get("t_available")
    t_avail = int(t_avail) if t_avail is not None else -1
    pending = label is None or label.get("delivered") is None
    fdc = None if pending else label.get("first_delivery_chunk")
    delivered = (not pending) and bool(label.get("delivered")) and fdc is not None
    valid = bool(delivered and fdc is not None and fdc >= t_avail)
    return SimpleNamespace(
        exposes=[str(e) for e in (eb.get("exposes") or [])],
        behavior=eb.get("behavior"),
        t_avail=t_avail,
        stale=eb.get("becomes_stale_at"),
        expected_form=eb.get("expected_form") or "BRIEF",
        mask=mask,
        pending=pending,
        fdc=fdc,
        delivered=delivered,
        valid=valid,
        form=(None if pending else label.get("form")),
    )


# --- per-case contribution functions: None=out-of-scope, _PENDING, or (num, den) ---

def _cried_wolf_contrib(f: SimpleNamespace):
    if not any(e.startswith("cried-wolf") for e in f.exposes):
        return None
    if f.pending:
        return _PENDING
    if not f.delivered:
        return (0, 0)  # no delivery → not an interrupt
    if f.behavior == "DROP":
        unwanted = 1
    elif f.behavior == "DEFER":
        bp = _first_breakpoint_at_or_after(f.mask, f.t_avail)
        unwanted = 1 if (bp is None or f.fdc < bp) else 0
    else:
        unwanted = 0
    return (unwanted, 1)


def _urgent_miss_contrib(f: SimpleNamespace):
    if "urgent-miss" not in f.exposes:
        return None
    if f.pending:
        return _PENDING
    on_time = f.valid and (not isinstance(f.stale, int) or f.fdc <= f.stale)
    return (0 if on_time else 1, 1)


def _breakpoint_hit_contrib(f: SimpleNamespace):
    if "breakpoint-hit" not in f.exposes:
        return None
    if f.pending:
        return _PENDING
    if not f.valid:
        return (0, 0)  # only actual deliveries are placed
    hit = 1 if (0 <= f.fdc < len(f.mask) and not f.mask[f.fdc]) else 0
    return (hit, 1)


def _delivery_rate_contrib(f: SimpleNamespace):
    if f.behavior == "DROP":
        return None  # delivery is never correct for DROP cases
    if f.pending:
        return _PENDING
    return (1 if f.valid else 0, 1)


def _conditional_form_contrib(f: SimpleNamespace):
    if not any(e in ("form", "form-accuracy") for e in f.exposes):
        return None
    if f.pending:
        return _PENDING
    if not f.valid:
        return (0, 0)  # form is conditioned on an actual valid delivery
    return (1 if f.form == f.expected_form else 0, 1)  # expected_form: BRIEF (default) | FULL


_METRIC_FNS = {
    "cried_wolf": (_cried_wolf_contrib, True),       # zero-when-no-deliveries
    "urgent_miss": (_urgent_miss_contrib, False),
    "breakpoint_hit": (_breakpoint_hit_contrib, False),
    "delivery_rate": (_delivery_rate_contrib, False),
    "conditional_form": (_conditional_form_contrib, False),
}


def _contrib_to_value(contrib) -> dict:
    if contrib is None:
        return {"in_scope": False}
    if contrib == _PENDING:
        return {"in_scope": True, "pending": True}
    return {"in_scope": True, "num": contrib[0], "den": contrib[1]}


def aggregate_tact_metrics(replay_runs: list[ReplayRun]) -> dict:
    """Combine per-case contributions into the headline rates for one arm.

    `None` = n/a (no qualifying delivery, or judge PENDING). cried_wolf is 0.0
    when there were no deliveries to cry wolf with.
    """
    facts = [_case_facts(r) for r in replay_runs]
    out: dict[str, float | None] = {}
    for name, (fn, zero_when_empty) in _METRIC_FNS.items():
        contribs = [c for c in (fn(f) for f in facts) if c is not None]  # in-scope only
        if any(c == _PENDING for c in contribs):
            out[name] = None
            continue
        num = sum(c[0] for c in contribs)
        den = sum(c[1] for c in contribs)
        if den == 0:
            out[name] = 0.0 if zero_when_empty else None
        else:
            out[name] = num / den
    return out


def _make_metric(metric_name: str):
    fn = _METRIC_FNS[metric_name][0]

    class _TactMetric:
        name = metric_name

        def compute(self, replay_run: ReplayRun) -> MetricValue:
            value = _contrib_to_value(fn(_case_facts(replay_run)))
            return MetricValue(name=metric_name, value=value, unit=None, aggregation="distribution")

    _TactMetric.__name__ = "".join(p.capitalize() for p in metric_name.split("_")) + "Metric"
    return _TactMetric


CriedWolf = _make_metric("cried_wolf")
UrgentMiss = _make_metric("urgent_miss")
BreakpointHit = _make_metric("breakpoint_hit")
DeliveryRate = _make_metric("delivery_rate")
ConditionalForm = _make_metric("conditional_form")


def tact_metrics() -> list:
    """The five per-case Metric instances, in report order."""
    return [CriedWolf(), UrgentMiss(), BreakpointHit(), DeliveryRate(), ConditionalForm()]


# ===========================================================================
# FailureSliceExtractor + adapter assembly + suite runner — PR5
# ===========================================================================

@dataclass
class TactFailureSliceExtractor:
    def extract(self, case: EvaluationCase, replay_run: ReplayRun, result: BenchmarkResult) -> list[FailureSlice]:
        if (replay_run.final_status or "") != "error":
            return []
        return [FailureSlice(
            case_id=case.case_id,
            causal_event_ids=tuple(),
            suspected_adapter="tact_minicpm_driver",
            relevant_policy_inputs={},
            suggested_fix="see ReplayRun.failures",
        )]


def build_tact_bench(
    input_mode: str = "text",
    arm: str = "prompted",
    judge_model: str = "gpt-4o",
    with_judge: bool = True,
    model_factory: object | None = None,
    judge_client_factory: object | None = None,
) -> BenchmarkAdapter:
    """Compose the six protocols into the runnable TACT-Bench adapter.

    Factories are injectable so the suite can run hermetically in tests (fake
    model + fake judge client). In production both are None → real MiniCPM-o +
    real OpenAI (the judge self-skips to PENDING without OPENAI_API_KEY).
    """
    judge = DeliveryJudge(model=judge_model, client_factory=judge_client_factory) if with_judge else None
    return BenchmarkAdapter(
        name=_BENCH_NAME,
        version=_BENCH_VERSION,
        case_source=TactCaseSource(input_mode=input_mode),
        scenario_driver=TactMiniCPMDriver(arm=arm, input_mode=input_mode, model_factory=model_factory),
        examiner=judge,
        metrics=tact_metrics(),
        failure_slicer=TactFailureSliceExtractor(),
        reporters=[],
    )


def _render_tact_report(adapter: BenchmarkAdapter, runs: list[ReplayRun], agg: dict) -> str:
    arm = getattr(adapter.scenario_driver, "arm", "?")
    input_mode = getattr(adapter.scenario_driver, "input_mode", "?")
    judged = adapter.examiner is not None and any(
        (r.results or {}).get("judge_label") and (r.results["judge_label"].get("delivered") is not None)
        for r in runs
    )

    def cell(v: object) -> str:
        if isinstance(v, (int, float)):
            return f"{v:.2f}"
        return "n/a" if judged else "PENDING (no judge)"

    lines = [
        f"# TACT-Bench report — arm={arm}, input_mode={input_mode}",
        "",
        f"Cases: {len(runs)} | judge: {'OpenAI ' + adapter.examiner.model if adapter.examiner else 'none'}",
        "",
        "## Metrics",
        "",
        "| Metric | value | direction |",
        "|---|---|---|",
        f"| cried_wolf | {cell(agg.get('cried_wolf'))} | lower better |",
        f"| urgent_miss | {cell(agg.get('urgent_miss'))} | lower better |",
        f"| breakpoint_hit | {cell(agg.get('breakpoint_hit'))} | higher better |",
        f"| delivery_rate | {cell(agg.get('delivery_rate'))} | diagnostic |",
        f"| conditional_form | {cell(agg.get('conditional_form'))} | higher better |",
        "",
        "## Per-case delivery labels",
        "",
        "| case | behavior | delivered | chunk | form |",
        "|---|---|---|---|---|",
    ]
    for r in runs:
        res = r.results or {}
        eb = res.get("expected_behavior") or {}
        lbl = res.get("judge_label") or {}
        lines.append(
            f"| {r.case_id} | {eb.get('behavior')} | {lbl.get('delivered')} | "
            f"{lbl.get('first_delivery_chunk')} | {lbl.get('form')} |"
        )
    lines.append("")
    return "\n".join(lines)


async def run_tact_suite(adapter: BenchmarkAdapter, output_dir: "str | Path", split: str = "all") -> dict:
    """Drive all cases → judge → metrics → write run.json/metrics.json/report.md.

    The cross-protocol orchestration the generic runner doesn't do (it only runs
    drivers). Per-case event logs are written by the driver into event_logs/.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_config = SimpleNamespace(output_dir=output_dir)

    runs: list[ReplayRun] = []
    case_rows: list[dict] = []
    for case in adapter.case_source.iter_cases(split):
        replay_run = await adapter.scenario_driver.run(case, None, run_config)
        if adapter.examiner is not None and (replay_run.results or {}).get("trajectory") is not None:
            label = adapter.examiner.label(
                replay_run.results["trajectory"],
                replay_run.results.get("item") or {},
                (case.inputs or {}).get("user_script"),
            )
            replay_run.results["judge_label"] = label
        runs.append(replay_run)
        case_rows.append({
            "case_id": case.case_id,
            "final_status": replay_run.final_status,
            "event_log_path": str(replay_run.event_log_path) if replay_run.event_log_path else None,
        })

    agg = aggregate_tact_metrics(runs)
    arm = getattr(adapter.scenario_driver, "arm", None)
    (output_dir / "run.json").write_text(json.dumps(
        {"adapter": adapter.name, "version": adapter.version, "arm": arm, "cases": case_rows}, indent=2))
    (output_dir / "metrics.json").write_text(json.dumps(agg, indent=2))
    (output_dir / "report.md").write_text(_render_tact_report(adapter, runs, agg))
    return {"metrics": agg, "n_cases": len(runs), "output_dir": str(output_dir)}
