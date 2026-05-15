"""ASR adapter Protocol — injected interface for streaming ASR transcription.

## Adapter-first design

The real implementation wraps faster-whisper on b200 (see
`companion_harness.asr_faster_whisper.FasterWhisperASRModel`). This module
stays SDK-free so it can be imported on machines without faster_whisper /
torch / CUDA. Tests inject a fake that returns scripted transcripts.

## Invocation cadence

The ASR model is invoked **on EOU signal only** (once per completed
utterance), NOT per-frame. This is the opposite of `BackchannelClassifier`
(which runs ASR periodically over a rolling window). EOU is rare relative
to the 32 ms frame cadence, and whisper-tiny.en at ~50-80 ms on b200 is
acceptable to call synchronously in the policy-gate task.

## Determinism (invariant #5)

The concrete implementation pins `temperature=0.0`, `beam_size=1`,
`condition_on_previous_text=False` so transcription is bit-identical for
identical audio bytes. The Protocol itself is shape-only; deterministic
behavior is the implementer's responsibility.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["ASRModel"]


@runtime_checkable
class ASRModel(Protocol):
    """Injected interface for streaming ASR transcription.

    The real implementation wraps faster-whisper on b200.
    Tests inject a fake that returns scripted transcripts.
    """

    def __call__(self, audio_chunks: bytes, sample_rate: int = 16000) -> str:
        """Transcribe accumulated audio bytes (one utterance, post-EOU).

        Returns the transcript. Deterministic given identical inputs.
        Empty string if no speech detected.
        """
        ...
