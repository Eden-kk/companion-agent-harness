# Manual Test Handbook — Option C Stage 4 (8-turn mic-test)

**Status**: STUB — operator-required; cannot be executed autonomously.

Sourced from `docs/plan-option-c-stage4-flag-flip-mictest.md` v3 §3–§4.

---

## Pre-test checklist (§3.1)

1. Launch b200 console with new default (no flag needed); confirm startup log shows `use_hybrid=True`.
2. Start event recording:
   ```
   python tests/manual/stream_display_to_file.py --host http://localhost:8800 --out /tmp/repro-hybrid-stage4.jsonl
   ```
3. Wear wired headphones (bluetooth 50-300 ms latency bloat distorts barge-in cut measurements).

---

## 8-turn script (§3.2)

| Turn | Script / action | Expected mode | Pass gate |
|------|----------------|---------------|-----------|
| T1 | "Hey there." | `AMBIENT_DUPLEX` holds | duplex chunk events only; no `hybrid_mode_switched_to_chat` |
| T2 | "What's two plus two?" | `LONG_RESPONSE_GATED → CHAT_STREAMING` | response contains "4"; first-audio ≤ 1500 ms (delta from `hybrid_mode_switched_to_chat` to first `assistant_audio_chunk_emitted`) |
| T3 | "My name is Hao." | `LONG_RESPONSE_GATED → CHAT_STREAMING` | `memory_write_candidate` emitted; ack contains "Hao" |
| T3-barge-in | Speak "stop" 1.5–2.5 s into T3 TTS playback | `CHAT_STREAMING → RESETTING_TO_DUPLEX` | cut ≤ 200 ms; measured as `t(audio_output_stopped) - t(speech_onset)` from JSONL; fall back to last `assistant_audio_chunk_emitted` if `audio_output_stopped` missing; `trigger="barge_in"` on cancel event |
| T4 | 3 s silence | `AMBIENT_DUPLEX` preserved | duplex `is_listen=True` events; no chat-stream |
| T5 | 3 s silence | `AMBIENT_DUPLEX` preserved | duplex alive; no `hybrid_mode_switched_to_chat` |
| T6 | "Mm-hm." | `AMBIENT_DUPLEX` preserved | no chat-stream switch; backchannel path fires |
| T7 | "Thanks, goodbye." | clean close | session ends without orphan events |

---

## Aggregate pass gates (§3.3)

- Mean chat-stream length ≥ 80 chars across T2 and T3.
- First-audio latency: BOTH T2 and T3 ≤ 1500 ms (>2000 ms on any single turn fails).
- Barge-in cut ≤ 200 ms (T3-barge-in only).
- `synthesis_skipped_no_proposal` count = 0.
- `log_drop_or_degrade` events: ideally 0.
- Stage 0 replay tests pass when re-run against captured session.

---

## Post-handbook analysis (§4)

Run against `/tmp/repro-hybrid-stage4.jsonl` after completing the 8 turns:

```python
python3 -c "
import json
from pathlib import Path
events = []
for line in Path('/tmp/repro-hybrid-stage4.jsonl').read_text().splitlines():
    try:
        evt = json.loads(line).get('msg', {}).get('event')
        if evt:
            events.append(evt)
    except json.JSONDecodeError:
        continue
assert events, 'no events parsed — check JSONL format (must come from stream_display_to_file.py)'
event_ids = {e['event_id'] for e in events}
orphans = []
for e in events:
    for parent_id in e.get('caused_by', []) or []:
        if parent_id not in event_ids:
            orphans.append((e['event_id'], e['event_type'], parent_id))
if orphans:
    print(f'FAIL: {len(orphans)} orphan caused_by references')
    for orph in orphans[:5]:
        print(' ', orph)
else:
    print(f'PASS: all {len(events)} events have closed caused_by chains')
"
```

Additional checks:
- Count `hybrid_mode_switched_to_chat` events (expect 2: T2, T3).
- Per chat-stream turn: `t(first assistant_audio_chunk_emitted) - t(hybrid_mode_switched_to_chat)` ≤ 1500 ms.
- Per chat-stream turn: sum `chat_stream_text_delta.content` chars ≥ 80.
- Group `log_drop_or_degrade` by `tee_name`; flag any `tee_name=foreground_ring` count > 0.

---

## Outcome

- **Pass**: every per-turn AND aggregate gate green → record results in a follow-up PR; default stays ON.
- **Fail**: any gate misses → revert `--use-hybrid` default to `False`, file issue with offending metric + JSONL excerpt, remediate.
