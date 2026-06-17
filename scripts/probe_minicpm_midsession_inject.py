"""Probe S0a: can a MiniCPM-o duplex session reference a HELD note injected at a
mid-session chunk t_inject, while it could NOT before injection?

Run on B200 (this machine — no ssh):

    /raid/yid042/venvs/companion-harness/bin/python scripts/probe_minicpm_midsession_inject.py

WHY THIS EXISTS
---------------
The streaming-trajectory TACT redesign hinges on a held result "arriving at
t_available" mid-stream rather than being known from t0. A code comment in
foreground_model_minicpm.py claims "MiniCPM-o does not support mid-session
re-prepare; context is folded at first-call only." A sibling probe
(probe_tact_pending_injection.py) already shows the injection CALL executes
without error -- but executing without error is NOT the linchpin. The linchpin is
RECALL: after injecting a distinctive codeword mid-session, can the model surface
that codeword on demand, when before injection it provably could not?

MECHANISM UNDER TEST
--------------------
MiniCPMODuplex.streaming_prefill(..., text_list=[...]) -- modeling_minicpmo.py
@ canonical rev 4382fcae: signature L2755-2762, TEXT-mode branch L3093-3116. The
text is tokenized and its embeddings are pushed into the SAME StreamDecoder KV
cache as the audio chunks via self.decoder.feed(text_embeds). So an injected note
becomes ordinary left-context for every subsequent chunk -- no re-prepare, no KV
reset. We exercise exactly that path mid-session.

We relax the duplex turn-gate (current_turn_ended -> always True) the same way
MiniCPMStreamingModel.stream_chunks does, so the model is ALLOWED to speak when we
pose the recall question (otherwise the gate can pin it to <|listen|> and mask a
correct recall).

DESIGN
------
The codeword (BANANA-47) is unguessable, so any post-injection emission of it is
proof the note entered context. We probe recall via a TEXT-mode turn ("What is
the secret codeword?") and read streaming_generate's emitted text:

  Phase BEFORE : pose the recall question (no note yet) -> expect NO codeword.
  Phase INJECT : streaming_prefill(text_list=[private note w/ BANANA-47]) at t_inject,
                 then a couple of silent "settle" chunks.
  Phase AFTER  : pose the recall question several chunks later -> codeword reachable.

We track the codeword in three buckets so an immediate verbatim echo of the note
at the inject chunk is not confused with genuine later retention:
  - PRE-inject  (BEFORE window): must be absent (sanity).
  - echo        (inject + settle chunks): the model reading the fresh note aloud.
  - delayed     (AFTER window): answering the question well after injection.
codeword_post = echo OR delayed -- the linchpin "reachable only post-injection".

VERDICT
-------
VIABLE   -- injection success=True AND codeword appears POST-inject (echo or delayed)
            but NEVER PRE-inject.
PARTIAL  -- injection executes but the codeword never surfaces post-inject, or leaks
            pre-inject: mechanism works, surfacing needs prompt/policy tuning.
NO-SHIP  -- injection raises / returns success=False, or a chunk crashes.

Exit: 0 = VIABLE, 2 = PARTIAL, 1 = NO-SHIP.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

_SAMPLE_RATE = 16000
_CHUNK_SAMPLES = 16000  # 1.0s @ 16kHz

_KOKORO_DEFAULT_MODEL = "/raid/yid042/models/kokoro/kokoro-v0_19.onnx"
_KOKORO_DEFAULT_VOICES = "/raid/yid042/models/kokoro/voices.json"

# Distinctive, unguessable payload. If this string shows up in model output, it
# can ONLY have come from the injected note -- the model cannot invent it.
_CODEWORD = "BANANA-47"

# Held note, framed as a private ChatML system turn that tells the model to KEEP
# the codeword to itself and only reveal it if asked. This separates two effects:
#   - immediate parrot at the inject chunk (the model reads the note aloud), and
#   - genuine delayed retention (model answers a question several chunks later).
# A "private; don't say it now" framing suppresses the first so the delayed recall
# is a clean test of whether the note actually persists in context.
_HELD_NOTE = (
    "<|im_start|>system\n"
    "Private note (not user speech; keep this to yourself, do not say it aloud now). "
    f"The secret codeword is {_CODEWORD}. Only reveal it later if the user asks for "
    "the codeword.\n"
    "<|im_end|>\n"
)

# Spoken recall question (rendered via Kokoro if present; otherwise we fall back
# to a TEXT-mode prefill of the same question so the probe still works headless).
_RECALL_QUESTION = "What is the secret codeword? Tell me the codeword now."

# Timeline (1s chunks):
#   0,1   warm silent audio chunks (session settles)
#   2     BEFORE recall question (no note yet) — expect NO codeword (sanity)
#   3     INJECT held note (codeword enters context) + silent audio
#   4,5   silent settle chunks (let any immediate parrot drain)
#   6,7,8 DELAYED recall question (asked well after inject) — genuine retention test
_T_BEFORE = 2
_T_INJECT = 3
_T_AFTER = (6, 7, 8)  # delayed recall window
_TOTAL_CHUNKS = 9


def _load_kokoro() -> object | None:
    model_path = os.environ.get("KOKORO_MODEL_PATH", _KOKORO_DEFAULT_MODEL)
    voices_path = os.environ.get("KOKORO_VOICES_PATH", _KOKORO_DEFAULT_VOICES)
    if not Path(model_path).exists() or not Path(voices_path).exists():
        print(f"  note: Kokoro not at {model_path} — recall question goes via text_list", flush=True)
        return None
    try:
        from companion_harness.tts_kokoro import KokoroTtsAdapter

        adapter = KokoroTtsAdapter(model_path=model_path, voices_path=voices_path, warmup=False)
        print(f"  Kokoro loaded from {model_path}", flush=True)
        return adapter
    except Exception as exc:  # noqa: BLE001 — probe degrades gracefully
        print(f"  note: Kokoro load failed ({type(exc).__name__}: {exc}) — recall via text_list", flush=True)
        return None


def _synthesize_16k(kokoro: object | None, text: str) -> np.ndarray:
    """Render `text` to float32 16kHz mono; empty array if Kokoro unavailable."""
    if kokoro is None:
        return np.zeros(0, dtype=np.float32)

    async def _collect() -> np.ndarray:
        chunks: list[np.ndarray] = []
        async for pcm16 in kokoro.synthesize(text, []):  # type: ignore[attr-defined]
            chunks.append(np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0)
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    raw_24k = asyncio.new_event_loop().run_until_complete(_collect())
    if len(raw_24k) == 0:
        return np.zeros(0, dtype=np.float32)
    ratio = _SAMPLE_RATE / 24000.0  # nearest-neighbour 24k -> 16k
    out_len = int(len(raw_24k) * ratio)
    idx = np.clip(np.round(np.arange(out_len) / ratio).astype(int), 0, len(raw_24k) - 1)
    return raw_24k[idx]


def _fit_chunk(audio: np.ndarray) -> np.ndarray:
    """Pad/trim a waveform to exactly one 1s chunk of float32 samples."""
    out = np.zeros(_CHUNK_SAMPLES, dtype=np.float32)
    if len(audio) > 0:
        end = min(len(audio), _CHUNK_SAMPLES)
        out[:end] = audio[:end]
    return out


def _generate(duplex: object) -> dict:
    return duplex.streaming_generate(  # type: ignore[attr-defined]
        max_new_speak_tokens_per_chunk=duplex.max_new_speak_tokens_per_chunk,  # type: ignore[attr-defined]
        temperature=duplex.temperature,  # type: ignore[attr-defined]
        top_k=duplex.top_k,  # type: ignore[attr-defined]
        top_p=duplex.top_p,  # type: ignore[attr-defined]
        listen_prob_scale=duplex.listen_prob_scale,  # type: ignore[attr-defined]
        text_repetition_penalty=duplex.text_repetition_penalty,  # type: ignore[attr-defined]
        text_repetition_window_size=duplex.text_repetition_window_size,  # type: ignore[attr-defined]
    )


def main() -> int:
    print("probe_minicpm_midsession_inject: loading MiniCPMStreamingModel...", flush=True)
    t0 = time.monotonic()
    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel

    model = MiniCPMStreamingModel()
    duplex = model._duplex  # noqa: SLF001 — probe pokes the seam directly
    print(f"  model loaded in {round((time.monotonic() - t0) * 1000)}ms", flush=True)

    kokoro = _load_kokoro()
    recall_audio = _synthesize_16k(kokoro, _RECALL_QUESTION)
    have_recall_audio = len(recall_audio) > 0

    # Relax the turn-gate so the model is permitted to speak when we ask for the
    # codeword. Same technique MiniCPMStreamingModel.stream_chunks uses: swap in a
    # subclass whose current_turn_ended always reads True. Without this the gate
    # at modeling_minicpmo.py L3216 can force <|listen|> and mask a real recall.
    class _AlwaysEnded:
        def __get__(self, obj, objtype=None) -> bool:
            return True

        def __set__(self, obj, value) -> None:
            pass

    orig_cls = type(duplex)
    relaxed_cls = type(
        f"{orig_cls.__name__}_GateRelaxed",
        (orig_cls,),
        {"current_turn_ended": _AlwaysEnded()},
    )
    duplex.__class__ = relaxed_cls

    before_text = ""        # emissions before/at the BEFORE recall question (no note yet)
    echo_text = ""          # emissions at the inject chunk + settle chunks (immediate echo)
    after_text = ""         # emissions during the DELAYED recall window
    post_inject_text = ""   # ALL emissions from the inject chunk onward (boundary-proof)
    injection_ok = False
    injection_error: str | None = None
    trajectory: list[dict] = []
    rc = 1

    try:
        # Fresh session with a generic (codeword-free) system prompt — proves the
        # codeword is NOT pre-loaded at t0; it can only enter via mid-session inject.
        duplex.model.reset_session(reset_token2wav_cache=False)  # type: ignore[attr-defined]
        duplex.prepare(prefix_system_prompt="Streaming Omni Conversation.")  # type: ignore[attr-defined]

        print("\nStepping duplex loop...\n", flush=True)
        print(f"{'chunk':<6} {'phase':<10} {'is_listen':<10} {'text_preview'}")
        print("-" * 72)

        for t in range(_TOTAL_CHUNKS):
            phase = "silence"
            ask_recall = t == _T_BEFORE or t in _T_AFTER
            try:
                if t == _T_INJECT:
                    phase = "INJECT"
                    pf = duplex.streaming_prefill(  # type: ignore[attr-defined]
                        audio_waveform=_fit_chunk(np.zeros(0, dtype=np.float32)),
                        text_list=[_HELD_NOTE],
                    )
                    injection_ok = bool(pf.get("success", False))
                    if not injection_ok:
                        injection_error = f"success=False: {pf.get('reason')}"
                elif ask_recall:
                    phase = "BEFORE" if t == _T_BEFORE else "AFTER"
                    if have_recall_audio:
                        # Modality-faithful: ask via audio (OMNI/AUDIO mode).
                        duplex.streaming_prefill(audio_waveform=_fit_chunk(recall_audio))  # type: ignore[attr-defined]
                    else:
                        # Headless fallback: ask via TEXT mode.
                        duplex.streaming_prefill(text_list=[_RECALL_QUESTION])  # type: ignore[attr-defined]
                else:
                    duplex.streaming_prefill(audio_waveform=_fit_chunk(np.zeros(0, dtype=np.float32)))  # type: ignore[attr-defined]

                result = _generate(duplex)
            except Exception as exc:  # noqa: BLE001 — capture for verdict
                err = f"{type(exc).__name__}: {exc}"
                if t == _T_INJECT:
                    injection_error = err
                print(f"{t:<6} {phase:<10} {'ERROR':<10} {err}")
                trajectory.append({"t": t, "phase": phase, "error": err})
                print("\nVERDICT: NO-SHIP — exception during step (see above).")
                return 1

            is_listen = bool(result.get("is_listen", True))
            text = result.get("text", "") or ""
            if phase == "BEFORE":
                before_text += " " + text
            elif phase == "AFTER":
                after_text += " " + text
            elif t >= _T_INJECT:
                # inject chunk + settle chunks: immediate post-injection emission
                echo_text += " " + text
            if t >= _T_INJECT:
                # Boundary-proof: a codeword token can straddle the echo/after split,
                # so also accumulate EVERYTHING from the inject chunk onward.
                post_inject_text += " " + text
            preview = text.replace("\n", " ")[:50]
            print(f"{t:<6} {phase:<10} {str(is_listen):<10} {preview!r}")
            trajectory.append({"t": t, "phase": phase, "is_listen": is_listen, "text": text})

        # Whitespace-robust match: the duplex emits ~20 tokens/chunk, so a codeword
        # can be split across chunk boundaries ("BANANA-4" | "7"). We log per-chunk
        # text joined with spaces, so collapse ALL whitespace before matching to
        # reconstruct the spoken string faithfully.
        def _has_codeword(text: str) -> bool:
            collapsed = "".join(text.split())
            target = "".join(_CODEWORD.split())
            return _CODEWORD in text or target in collapsed

        codeword_before = _has_codeword(before_text)
        codeword_echo = _has_codeword(echo_text)        # spoke it right after inject
        codeword_recall = _has_codeword(after_text)     # answered the delayed question
        # The linchpin: codeword reachable anywhere from the inject chunk onward. Use
        # the full post-inject transcript so a codeword token split across the
        # echo/after window boundary is still counted (echo/recall flags are nuance).
        codeword_post = _has_codeword(post_inject_text)

        out_path = "/tmp/probe_minicpm_midsession_inject.json"
        with open(out_path, "w") as f:
            json.dump(
                {
                    "codeword": _CODEWORD,
                    "injection_ok": injection_ok,
                    "injection_error": injection_error,
                    "recall_modality": "audio" if have_recall_audio else "text_list",
                    "codeword_before": codeword_before,
                    "codeword_echo": codeword_echo,
                    "codeword_recall": codeword_recall,
                    "codeword_post": codeword_post,
                    "before_text": before_text.strip(),
                    "echo_text": echo_text.strip(),
                    "after_text": after_text.strip(),
                    "post_inject_text": post_inject_text.strip(),
                    "trajectory": trajectory,
                },
                f,
                indent=2,
            )

        print()
        print(f"  recall modality          : {'audio' if have_recall_audio else 'text_list (headless)'}")
        print(f"  (a) injection ok         : {injection_ok}" + (f"  [{injection_error}]" if injection_error else ""))
        print(f"  (b) codeword POST-inject : {codeword_post}  (echo={codeword_echo}, delayed_recall={codeword_recall})")
        print(f"  (c) codeword PRE-inject  : {codeword_before}   before_text={before_text.strip()!r}")
        print(f"      post_inject_text     : {post_inject_text.strip()!r}")
        print(f"  raw trajectory           : {out_path}")
        print()

        if not injection_ok:
            print(f"VERDICT: NO-SHIP — mid-session text injection failed: {injection_error}")
            rc = 1
        elif codeword_post and not codeword_before:
            recall_note = (
                "answered the delayed recall question" if codeword_recall
                else "echoed it immediately at inject (delayed recall did not re-surface it)"
            )
            print(
                "VERDICT: VIABLE — streaming_prefill(text_list=[...]) injected the note "
                f"mid-session; codeword appeared only AFTER injection ({recall_note}), never BEFORE."
            )
            rc = 0
        else:
            why = []
            if not codeword_post:
                why.append("codeword never surfaced post-injection (surfacing needs prompt/policy tuning)")
            if codeword_before:
                why.append("codeword leaked BEFORE injection (unexpected — investigate)")
            print("VERDICT: PARTIAL — injection executes but recall is ambiguous: " + "; ".join(why))
            rc = 2
    finally:
        duplex.__class__ = orig_cls  # restore the real gate

    return rc


if __name__ == "__main__":
    sys.exit(main())
