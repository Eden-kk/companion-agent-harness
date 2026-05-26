"""Thin per-session wrapper around MiniCPMODuplex exposing only what the orchestrator needs."""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

CHUNK_SAMPLES: int = 16000  # 16 kHz × 1 s
REF_AUDIO_KWARG: str = "ref_audio"  # Stage 0 confirmed: prepare() accepts ref_audio kwarg


@dataclass
class DuplexTickResult:
    is_listen: bool
    text: str
    audio_waveform: np.ndarray  # always length CHUNK_SAMPLES; zeros when silent
    end_of_turn: bool
    current_time: float


class MiniCPMDuplexSession:
    def __init__(
        self,
        base_model,
        *,
        prefix_system_prompt: str,
        ref_audio: np.ndarray | None = None,
        force_listen_count: int = 0,
        name: str,
    ) -> None:
        self._name = name
        self._duplex = base_model.as_duplex(generate_audio=True, chunk_ms=1000)
        # force_listen_count is not an as_duplex() param per modeling_minicpmo.py
        # :2434/:2516; set post-construction.
        if force_listen_count:
            setattr(self._duplex, "force_listen_count", force_listen_count)
        self._duplex.prepare(prefix_system_prompt=prefix_system_prompt, ref_audio=ref_audio)

    def prefill(self, audio_1s: np.ndarray, *, text_list: list[str] | None = None) -> None:
        if text_list is not None:
            self._duplex.streaming_prefill(audio_waveform=audio_1s, text_list=text_list)
        else:
            self._duplex.streaming_prefill(audio_waveform=audio_1s)

    def generate(self, *, listen_prob_scale: float) -> DuplexTickResult:
        result = self._duplex.streaming_generate(listen_prob_scale=listen_prob_scale)
        current_time = time.monotonic()
        raw = result.get("audio_waveform") if isinstance(result, dict) else getattr(result, "audio_waveform", None)
        if raw is None:
            wav = np.zeros(CHUNK_SAMPLES, dtype=np.float32)
        else:
            wav = np.asarray(raw, dtype=np.float32)
            if len(wav) < CHUNK_SAMPLES:
                wav = np.pad(wav, (0, CHUNK_SAMPLES - len(wav)))
            elif len(wav) > CHUNK_SAMPLES:
                wav = wav[:CHUNK_SAMPLES]
        is_listen = bool(result.get("is_listen", True) if isinstance(result, dict) else getattr(result, "is_listen", True))
        text = (result.get("text", "") if isinstance(result, dict) else getattr(result, "text", "")) or ""
        end_of_turn = bool(result.get("end_of_turn", False) if isinstance(result, dict) else getattr(result, "end_of_turn", False))
        return DuplexTickResult(
            is_listen=is_listen,
            text=text,
            audio_waveform=wav,
            end_of_turn=end_of_turn,
            current_time=current_time,
        )

    def set_break(self) -> None:
        self._duplex.set_break_event()

    def clear_break(self) -> None:
        self._duplex.clear_break_event()

    def is_break_set(self) -> bool:
        return bool(self._duplex.is_break_set())

    def reset(self, *, prefix_system_prompt: str, ref_audio: np.ndarray | None = None) -> None:
        # reset_session pattern from foreground_model_minicpm.py:302 +
        # probe_minicpm_partial_chunk.py:190-191.
        self._duplex.model.reset_session(reset_token2wav_cache=False)
        self._duplex.prepare(prefix_system_prompt=prefix_system_prompt, ref_audio=ref_audio)
