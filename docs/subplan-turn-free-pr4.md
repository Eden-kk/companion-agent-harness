# Sub-plan: PR4 — Background-think injection (plumbing)

**Status:** DRAFT (round 1, post-critic) — for plan-critic re-review.
**Parent:** `plan-turn-free-continuous-execution.md` PR4. **Stacks on:** `feat/turn-free-pr3c`.
**One outcome:** the orchestrator injects a background "thought" into the foreground model's KV cache as a role-tagged `background-think` text unit (`streaming_prefill(text_list=...)`), emitting an audit event — "background model whispers context into the foreground's ear."

**Scope:** injection **plumbing only**, CPU-testable. **Deferred** (live/measured): the real background-reasoner process on a dedicated CUDA stream; the GPU-contention p95<250ms gate; the ≥80%@N≥10 incorporation re-probe; role-aware eviction (Strategy 2). PR4 ships the seam a real reasoner plugs into.

## 1. The injection point — INSIDE `_gpu_work` (executor thread)

`stream_chunks` runs the GPU call (`streaming_prefill`+`streaming_generate`) inside `_gpu_work`, dispatched via `run_in_executor` (the loop top is the **event-loop thread** — a GPU call there would block the loop). So the text-prefill drain MUST happen **inside `_gpu_work`, before the audio `streaming_prefill`**:

```python
# adapter: inject_scratchpad enqueues; _gpu_work (executor thread) drains
def inject_scratchpad(self, text: str) -> None:
    self._scratchpad_queue.append(text)              # collections.deque; append is GIL-atomic

def _gpu_work(pcm_float):
    while self._scratchpad_queue:                    # drain (popleft GIL-atomic) in executor thread
        duplex.streaming_prefill(text_list=[self._scratchpad_queue.popleft()])
    duplex.streaming_prefill(audio_waveform=pcm_float)   # then the audio chunk
    return duplex.streaming_generate(...)
```

The orchestrator (event-loop thread) **appends** to the deque; `_gpu_work` (executor thread) **drains** it. `collections.deque` append/popleft are atomic under the GIL — safe across the two threads (document it). `streaming_prefill(text_list=...)` is a supported `TEXT`-unit prefill (design §4/§9; probe-confirmed). Confined to `stream_chunks` (continuous path); turn-based `infer_stream` untouched.

## 2. Thought source + orchestrator wiring (caused_by ordering)

```python
class _BackgroundThoughtSourceProtocol(Protocol):
    def pending_thought(self) -> str | None: ...
```

Injected as `thought_source`, default module-level `_NULL_THOUGHT_SOURCE` (returns None → no injection → PR3c behavior preserved). **Inject AFTER the chunk yield** (so `caused_by` is the *current* chunk's `raw_audio_chunk` evt — no off-by-one): in `run()`, after `_act`, append the thought; the adapter drains it before the **next** chunk's audio prefill.

```python
# run(), after self._act(...):
thought = self._thought_source.pending_thought()
if thought is not None:
    self._foreground.inject_scratchpad(thought)
    self._emit_background_think_injected(n_chars=len(thought), caused_by_evt_id=caused_by_evt_id)
```

Role tag + length go in `payload_inline` (`{"role": "background-think", "n_chars": ...}`). **Do NOT log the thought text** (free-text → SensitiveField/privacy). Probe 1c showed the model does not voice injected context (safe).

## 3. File-by-file

| File | Change |
|---|---|
| `companion_harness/foreground_model_minicpm.py` | add `self._scratchpad_queue = collections.deque()` in `__init__`; `inject_scratchpad(text)` (append); in `_gpu_work`, drain+`streaming_prefill(text_list=[t])` before the audio prefill (§1). Turn-based path untouched. |
| `companion_harness/continuous_orchestrator.py` | `_BackgroundThoughtSourceProtocol` + `_NULL_THOUGHT_SOURCE`; ctor `thought_source=_NULL_THOUGHT_SOURCE`; pull+inject+emit after `_act` in `run()`; `_emit_background_think_injected`; add `inject_scratchpad` to `_ForegroundModelProtocol`. |
| `companion_harness/v0_1g_event_schema.py` + `tests/test_v0_1g_event_schema.py` | register `background_think_injected` (signal/self/safe/signal_default_30d; `required_fields=("role","n_chars")` if the dataclass field exists — read it first). |
| `tests/test_continuous_pr4_think_injection.py` (new) | CPU success test (§4). |

**Existing-stub note:** `_ForegroundModelProtocol` is a structural (non-`runtime_checkable`, no `isinstance`) Protocol; existing test fakes that omit `inject_scratchpad` do NOT break — the orchestrator only calls it when `thought_source` yields a thought, and existing tests use the null source (None) → never called. No existing stub needs updating for the orchestrator tests.

## 4. Success criterion (sole programmatic gate)

`tests/test_continuous_pr4_think_injection.py` — CPU, stubs:
- **Orchestrator test:** stub `foreground` recording `inject_scratchpad` calls (+ faithful `stream_chunks`); stub `thought_source` returning a thought on chunk 1 only. Assert: chunk 1 → one `inject_scratchpad("...")` + a `background_think_injected` event (role="background-think", n_chars correct, caused_by closed, **no thought text in payload**); other chunks → none; default null source → none (regression); PR1–PR3c suites green.
- **Adapter drain test (CPU, no torch):** construct the adapter via `object.__new__(MiniCPMStreamingModel)` (bypass `__init__`'s model load), manually set `_scratchpad_queue = deque()` and a fake `_duplex` recording `streaming_prefill(text_list=...)` calls + a stub `_inference_executor` (or run `_gpu_work` inline). Call `inject_scratchpad` then drive one `_gpu_work`; assert the fake `_duplex` got a `streaming_prefill(text_list=[thought])` call BEFORE the `streaming_prefill(audio_waveform=...)` call. (If `_gpu_work` is a closure not directly callable, extract the drain into a small `_drain_scratchpad(duplex)` method and unit-test that.)

## 5. Invariants

- **#1/#10:** `background_think_injected` logged with `caused_by` (length+role only, no text); non-blocking.
- **#2/#4:** the thought is **context, not speech** — foreground output still passes `decide_chunk`+gate (probe 1c). No gate bypass.
- **SensitiveField:** thought text never logged.

## 6. Risks / open questions

| Item | Handling |
|---|---|
| Drain location (event loop vs executor) | drain INSIDE `_gpu_work` (executor); orchestrator only appends. deque append/popleft GIL-atomic (§1). |
| CPU test can't build the real adapter (`__init__` loads model) | `object.__new__` bypass + manual attrs + fake `_duplex` (§4); or extract `_drain_scratchpad` and unit-test it. |
| caused_by off-by-one | inject AFTER the chunk yield → current chunk's evt (§2). |
| Real reasoner / CUDA stream / GPU-contention / incorporation | deferred (live/measured); PR4 is the seam. |

## 7. Out of scope

Real background-reasoner process (dedicated CUDA stream), GPU-contention p95 gate, incorporation re-probe, role-aware eviction (Strategy 2), live wiring.
