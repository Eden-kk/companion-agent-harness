# Sub-plan: PR4 — Background-think injection (plumbing)

**Status:** DRAFT (round 0) — for plan-critic review.
**Parent:** `plan-turn-free-continuous-execution.md` PR4. **Stacks on:** `feat/turn-free-pr3c`.
**One outcome:** the orchestrator injects a background "thought" into the foreground model's single KV cache as a role-tagged `background-think` text unit (`streaming_prefill(text_list=...)`) at a chunk boundary, emitting an audit event — the design's "background model whispers context into the foreground's ear."

**Scope:** the **injection plumbing only**, CPU-testable with stubs. Explicitly **deferred** (live/measured, not this PR's CPU merge): the real background-reasoner process on a dedicated CUDA stream; the GPU-contention p95<250ms gate; the ≥80%@N≥10 incorporation re-probe; role-aware eviction (Strategy 2). PR4 ships the *seam* a real background reasoner plugs into.

## 1. The injection point (adapter owns model mechanics)

`stream_chunks` (in `MiniCPMStreamingModel`) owns the per-chunk prefill+generate loop and the duplex. So text injection must happen **inside `stream_chunks`** (it can't be interleaved from outside an async generator). Adapter-first split:
- **Adapter** exposes `inject_scratchpad(text: str)` which **enqueues** the text on a thread-safe deque on the model.
- `stream_chunks`, at the **top of each loop iteration, before the audio prefill**, drains the queue and calls `self._duplex.streaming_prefill(text_list=[text])` for each pending thought (design-confirmed: `streaming_prefill` accepts `text_list`). It yields the injected text id so the orchestrator can close `caused_by` — OR the orchestrator owns the event (see §2).
- **Orchestrator** decides WHEN to inject (pulls from a thought source) and calls `inject_scratchpad`; it emits the `background_think_injected` event (it knows `caused_by`).

This keeps `streaming_prefill` (model SDK) inside the adapter; the orchestrator stays SDK-free (adapter-first).

## 2. Thought source + orchestrator wiring

```python
class _BackgroundThoughtSourceProtocol(Protocol):
    def pending_thought(self) -> str | None: ...   # next thought to inject, or None
```

Injected into `ContinuousOrchestrator.__init__` as `thought_source`, default a module-level `_NULL_THOUGHT_SOURCE` (returns None → no injection → PR3c behavior preserved). In `run()`, once per chunk (before policy/act):

```python
thought = self._thought_source.pending_thought()
if thought is not None:
    self._foreground.inject_scratchpad(thought)
    self._emit_background_think_injected(thought_len=len(thought), caused_by_evt_id=caused_by_evt_id)
```

The thought is a **role-tagged `background-think` unit** — the role tag is the event's `payload_inline` (`{"role": "background-think", "n_chars": ...}`); the KV unit itself is plain text the model treats as context. Do NOT log the thought text (free-text → SensitiveField discipline / privacy); log only length + role. Probe 1c (§3.2) showed the model does not voice injected context, so this is safe.

## 3. File-by-file

| File | Change |
|---|---|
| `companion_harness/foreground_model_minicpm.py` | add `inject_scratchpad(self, text: str)` (enqueue on a `collections.deque`); in `stream_chunks`, drain+`streaming_prefill(text_list=[t])` at the top of each iteration before audio prefill. Surgical; turn-based `infer_stream` untouched. |
| `companion_harness/continuous_orchestrator.py` | `_BackgroundThoughtSourceProtocol` + module-level `_NULL_THOUGHT_SOURCE`; ctor `thought_source=_NULL_THOUGHT_SOURCE`; pull+inject+emit in `run()`; `_emit_background_think_injected`. Add `inject_scratchpad` to `_ForegroundModelProtocol`. |
| `companion_harness/v0_1g_event_schema.py` + `tests/test_v0_1g_event_schema.py` | register `background_think_injected` (signal/self/safe/signal_default_30d; required_fields `["role","n_chars"]` if the dataclass has required_fields). |
| `tests/test_continuous_pr4_think_injection.py` (new) | CPU success test (§4). |

## 4. Success criterion (sole programmatic gate)

`tests/test_continuous_pr4_think_injection.py::test_background_thought_injected` — CPU, stubs. Stub `thought_source` returning a thought on chunk 1 only; stub `foreground` recording `inject_scratchpad` calls (and faithfully implementing `stream_chunks`). Assert: on chunk 1 → exactly one `inject_scratchpad("...")` call + a `background_think_injected` event (payload role="background-think", n_chars correct, caused_by closed, **no thought text in payload**); other chunks → no injection; default null source → no injection (regression); PR1–PR3c suites green. A second unit test on the real adapter: `inject_scratchpad` enqueues and `stream_chunks` drains it as a `text_list` prefill — assert via a stub duplex recording `streaming_prefill(text_list=...)` calls (no torch; inject a fake `_duplex` into a minimally-constructed adapter, or test the deque drain logic in isolation).

## 5. Invariants

- **#1/#10:** `background_think_injected` logged with `caused_by` (length+role only, no free text); non-blocking.
- **#2/#4:** the injected thought is **context, not speech** — the foreground's resulting tokens still pass `decide_chunk` + the gate (probe 1c: model doesn't voice it). No bypass of the speak gate.
- **SensitiveField discipline:** thought text never logged (only n_chars + role).

## 6. Risks / open questions

| Item | Handling |
|---|---|
| Injecting `text_list` mid-stream corrupts the audio KV alignment | `streaming_prefill(text_list=...)` is a supported unit type (design §4/§9, probe-confirmed `TEXT` mode); injected as its own `<unit>` between audio chunks. Real-model behavior verified manual/b200. |
| Thread-safety of the deque (adapter executor thread vs event loop) | `collections.deque` append/popleft are atomic in CPython; orchestrator appends, stream_chunks (executor) drains. Document it. |
| Real background reasoner + CUDA stream + GPU-contention/incorporation | deferred (live/measured); PR4 is the seam. The real reasoner plugs into `_BackgroundThoughtSourceProtocol`. |
| `inject_scratchpad` on `_ForegroundModelProtocol` — stub must implement it | the CPU test's fake foreground implements it (records calls). |

## 7. Out of scope

The real background-reasoner process (dedicated CUDA stream), GPU-contention p95 gate, incorporation re-probe (≥80%@N≥10), role-aware eviction (Strategy 2), live wiring. PR4 is the injection seam + audit, nothing more.
