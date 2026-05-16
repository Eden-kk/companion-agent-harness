# Phase C fixture pack

Per-session directory layout per `docs/plan-v0.2d-execution.md` OQ-1:

```
tests/fixtures/phase_c/<session_id>/
  manifest.json               # session_id, recording_date, speaker_count, duration_ms,
                              #   license, redaction_notes, schema_version
  ground_truth_speakers.json  # schema_version + utterances[]: {utterance_id,
                              #   t_start_ms, t_end_ms, speaker_id, addressed_agent}
  event_log.jsonl             # replay-safe event log (no raw audio bytes)
  audio.wav                   # optional: mono 16 kHz source audio (omitted if synthesized)
```

## Sessions

| session_id | option | speakers | duration | license |
|---|---|---|---|---|
| `phase_c_synthetic_two_speaker_001` | 1 (Kokoro TTS, seed=42) | 2 | 12 s | safe_eval_fixture |
| `phase_c_synthetic_three_speaker_001` | 1 (Kokoro TTS, seed=43) | 3 | 18 s | safe_eval_fixture |
| `phase_c_libripaired_two_speaker_001` | 2 (LibriVox CC-BY proxy) | 2 | 15 s | CC-BY-4.0 |

## Labelling convention

`ground_truth_speakers.json` is the ground-truth labelling artifact:
- `utterance_id`: unique within the session, used as the join key in metrics.
- `t_start_ms / t_end_ms`: utterance time range from session start.
- `speaker_id`: speaker label (e.g. `speaker_A`, `speaker_B`, `speaker_C`).
- `addressed_agent`: `true` if the utterance is directed at the companion agent.

## License posture (per OQ-4)

- **Option 1 (synthesized):** No real audio. Fully synthetic via Kokoro TTS with a
  deterministic seed. No consent required.
- **Option 2 (LibriVox CC-BY):** Attribution required. Cite "LibriVox
  (https://librivox.org)" in eval reports. The `audio.wav` file for this session
  is a synthesized CI proxy; the real LibriVox clip is referenced by the manifest's
  `attribution` field.
- **Option 3 (operator-recorded):** Deferred to v0.3. Requires consent doc checked in.

## Schema version

Both `manifest.json` and `ground_truth_speakers.json` carry `schema_version: "1.0"`.
v0.3 may introduce `"2.0"` for cross-session speaker recognition fields.
