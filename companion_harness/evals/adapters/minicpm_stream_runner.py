"""MiniCPM streaming runner for the TACT-Bench streaming-trajectory harness (S3).

run_case() drives one Layer-3 case through a gate-relaxed as_duplex session,
feeding the scenario timeline tick by tick, injecting the PENDING note at
t_avail via streaming_prefill(text_list=[note]), and returning a list of
Emission objects from the delivery detector.

Reuses the gate-relax pattern from stream_chunks / probe_minicpm_midsession_inject.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from companion_harness.evals.adapters.delivery_detector import annotate_emissions
from companion_harness.evals.adapters.scenario_timeline import build_timeline
from companion_harness.evals.adapters.tact_bench import _ARMS, load_tier2
from companion_harness.evals.adapters.tact_bench_layer3_run import _load_layer2_context
from companion_harness.evals.adapters.tact_bench_stream_types import Emission

if TYPE_CHECKING:
    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel

_CHUNK_SAMPLES = 16000  # 1s @ 16 kHz


class _AlwaysEnded:
    def __get__(self, obj, objtype=None) -> bool:
        return True

    def __set__(self, obj, value) -> None:
        pass


def _generate(duplex: object) -> dict:
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


def run_case(
    case_id: str,
    arm: str,
    input_modality: str,
    model: "MiniCPMStreamingModel",
    *,
    give_user_state: bool = False,
) -> list[Emission]:
    """Run one TACT case through MiniCPM in a gate-relaxed duplex session.

    Parameters
    ----------
    case_id
        Layer-3 case id, e.g. "TC1".
    arm
        "vanilla" or "prompted" (keys in tact_bench._ARMS).
    input_modality
        "audio" (per-tick TTS PCM) or "text" (per-tick text_list with silence audio).
    model
        A loaded MiniCPMStreamingModel instance.
    give_user_state
        When True, inject the per-tick user-state label into text_list (Tier-2 signal ON).

    Returns
    -------
    list[Emission]
        Annotated per-tick emissions from the delivery detector.
    """
    if arm not in _ARMS:
        raise ValueError(f"unknown arm {arm!r}; expected one of {sorted(_ARMS)}")
    if input_modality not in ("audio", "text"):
        raise ValueError(f"unknown input_modality {input_modality!r}")

    timeline = build_timeline(case_id)
    ticks = timeline.ticks
    n_ticks = len(ticks)

    if input_modality == "audio":
        audio_frames = timeline.audio_view()
    else:
        audio_frames = None

    silence = np.zeros(_CHUNK_SAMPLES, dtype=np.float32)

    # Layer-2 context: per-item payloads + standing_instruction
    ctx = _load_layer2_context()
    ctx_case = ctx.get(case_id, {})
    pending_payloads: dict[str, str] = ctx_case.get("payloads", {})
    standing = ctx_case.get("standing")
    earlier_asks: dict[str, str] = {iid: standing for iid in pending_payloads} if standing else {}

    # Tier-2: load user_state and label map once (only needed when give_user_state=True)
    user_state: list[str] = []
    tier2_labels: dict[str, str] = {}
    if give_user_state:
        from companion_harness.evals.adapters.tact_bench_layer3 import load_layer3
        l3_cases = load_layer3()
        l3_obj = next((c for c in l3_cases if c.id == case_id), None)
        user_state = l3_obj.user_state if l3_obj is not None else ["i"] * n_ticks
        tier2_labels = load_tier2()["user_state_labels"]

    duplex = model._duplex  # noqa: SLF001
    orig_cls = type(duplex)
    relaxed_cls = type(
        f"{orig_cls.__name__}_GateRelaxed",
        (orig_cls,),
        {"current_turn_ended": _AlwaysEnded()},
    )
    duplex.__class__ = relaxed_cls
    try:
        duplex.model.reset_session(reset_token2wav_cache=False)
        duplex.prepare(prefix_system_prompt=_ARMS[arm])

        raw: list[tuple[int, bool, str]] = []
        for t in range(n_ticks):
            tick = ticks[t]
            text_parts: list[str] = []

            # pending note injection at t_avail (validated S0a mechanism)
            if tick.pending_note is not None:
                text_parts.append(tick.pending_note)

            if give_user_state:
                state = user_state[t] if t < len(user_state) else "i"
                text_parts.append(f"[user state: {tier2_labels.get(state, state)}]")

            if input_modality == "audio":
                audio = audio_frames[t]  # type: ignore[index]
                if text_parts:
                    duplex.streaming_prefill(  # type: ignore[attr-defined]
                        audio_waveform=audio, text_list=text_parts
                    )
                else:
                    duplex.streaming_prefill(audio_waveform=audio)  # type: ignore[attr-defined]
            else:
                # text modality: user text via text_list + 1s silence as required audio
                if tick.kind in ("speech", "context") and tick.text:
                    text_parts.append(tick.text)
                if text_parts:
                    duplex.streaming_prefill(  # type: ignore[attr-defined]
                        audio_waveform=silence, text_list=text_parts
                    )
                else:
                    duplex.streaming_prefill(audio_waveform=silence)  # type: ignore[attr-defined]

            result = _generate(duplex)
            is_listen = bool(result.get("is_listen", True))
            text = result.get("text", "") or ""
            raw.append((t, not is_listen, text))
    finally:
        duplex.__class__ = orig_cls

    return annotate_emissions(raw, _case_dict(case_id), pending_payloads, earlier_asks or None)


def _case_dict(case_id: str) -> dict:
    """Minimal case dict for annotate_emissions (items with id + t_avail)."""
    from companion_harness.evals.adapters.tact_bench_layer3 import load_layer3

    cases = load_layer3()
    for c in cases:
        if c.id == case_id:
            return {
                "id": c.id,
                "items": [{"id": it.id, "t_avail": it.t_avail} for it in c.items],
            }
    raise KeyError(f"Layer3 case not found: {case_id!r}")
