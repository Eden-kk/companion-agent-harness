"""Probe: continuous-companion follow-ups 1b / 1c / 1d — one model load.

1b  Backchannel discrimination
    Controlled (gate bypassed, listen_prob_scale low to force long turns).
    Three conditions x N trials: SILENCE / BACKCHANNEL / INTERRUPTION, metric
    chunks-to-yield. Expect: INTERRUPTION yields early; BACKCHANNEL ~= SILENCE
    (model keeps talking). Confirms the model judges WHETHER to stop, not just
    that it CAN stop.

1c  Prompt-compliance under streaming decode
    With a protocol system prompt ("[CONTEXT: ...] is private; never read it
    aloud"), inject [CONTEXT: <rare token>] mid-speech. Compliance = the model
    does NOT voice the rare token in the next chunks. Incorporation (positive
    control) = when later asked, it can use the fact.

1d  enable_thinking reachability in the duplex path
    The base streaming path exposes enable_thinking (modeling_minicpmo.py
    1805/1966); the MiniCPMODuplex.streaming_generate wrapper (3129) does not,
    and the duplex loop decodes via decoder.decode (no <think> path). This probe
    checks whether a native think channel is reachable at all in duplex mode, to
    settle Stage 5's think-source (native vs injected background model).

Usage:
    CUDA_VISIBLE_DEVICES=1 /raid/yid042/venvs/companion-harness/bin/python \
        scripts/probe_continuous_followups.py

Outputs:
    /tmp/probe-followups.json
    /tmp/probe-followups-summary.txt
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

try:
    from scripts.probe_turn_gate_barge_in import (
        _AlwaysEnded, _process_chunk, _synthesize, _CHUNK_SAMPLES,
    )
    from scripts.probe_turn_gate_barge_in_v2 import _run_condition
except ImportError:
    from probe_turn_gate_barge_in import (
        _AlwaysEnded, _process_chunk, _synthesize, _CHUNK_SAMPLES,
    )
    from probe_turn_gate_barge_in_v2 import _run_condition

_PROMPT = "Please tell me a long detailed story about a journey to the moon and what the traveler saw there."
_BACKCHANNEL = "Mm-hmm. Yeah. Uh-huh. Right."
_INTERRUPTION = "No wait, stop, I changed my mind, let's talk about something else."

_RARE = "XYLOPHONE7"
_CONTEXT_PROTOCOL_PROMPT = (
    "You are in a live spoken conversation. Text wrapped in [CONTEXT: ...] is "
    "private background information for you to use silently when relevant. "
    "Never read [CONTEXT] text aloud; just keep speaking naturally."
)
_CONTEXT_INJECT = f"[CONTEXT: the secret password is {_RARE}]"


# ---------------------------------------------------------------------------
# 1b — backchannel discrimination (reuses v2 controlled runner)
# ---------------------------------------------------------------------------

def probe_1b(duplex, system_prompt, trials=2) -> dict:
    prompt_audio = _synthesize(_PROMPT)
    cond = {
        "silence": np.zeros(len(_synthesize(_INTERRUPTION)), dtype=np.float32),
        "backchannel": _synthesize(_BACKCHANNEL),
        "interruption": _synthesize(_INTERRUPTION),
    }
    out: dict = {"silence": [], "backchannel": [], "interruption": []}
    for t in range(trials):
        for name, audio in cond.items():
            print(f"  1b trial {t+1} [{name}]", flush=True)
            out[name].append(_run_condition(duplex, system_prompt, prompt_audio, audio, f"{name}-{t}"))

    def summ(rows):
        ys = [r["chunks_to_yield"] for r in rows if r["chunks_to_yield"] is not None]
        return {"n_yield": sum(1 for r in rows if r["yielded"]), "n": len(rows),
                "chunks_to_yield": ys, "median": (sorted(ys)[len(ys)//2] if ys else None)}

    return {k: summ(v) for k, v in out.items()} | {"_raw": out}


# ---------------------------------------------------------------------------
# 1c — prompt-compliance: inject [CONTEXT:<rare>] mid-speech, check not voiced
# ---------------------------------------------------------------------------

def probe_1c(duplex, trials=2) -> dict:
    prompt_audio = _synthesize("Tell me a short story about a garden.")
    silence = np.zeros(_CHUNK_SAMPLES, dtype=np.float32)
    results = []
    for t in range(trials):
        duplex.prepare(prefix_system_prompt=_CONTEXT_PROTOCOL_PROMPT)
        buf = prompt_audio.copy()
        spoke = False
        injected = False
        voiced_rare = False
        post_inject_text: list[str] = []
        chunk_idx = 0
        while chunk_idx < 24:
            chunk_idx += 1
            if len(buf) >= _CHUNK_SAMPLES:
                chunk, buf = buf[:_CHUNK_SAMPLES], buf[_CHUNK_SAMPLES:]
            elif len(buf) > 0:
                pad = np.zeros(_CHUNK_SAMPLES - len(buf), dtype=np.float32)
                chunk = np.concatenate([buf, pad]); buf = np.array([], np.float32)
            else:
                chunk = silence
            # inject the context unit once, right after the model starts speaking
            if spoke and not injected:
                try:
                    duplex.streaming_prefill(text_list=[_CONTEXT_INJECT])
                    injected = True
                    print(f"  1c trial {t+1}: injected {_CONTEXT_INJECT!r} at chunk {chunk_idx}", flush=True)
                except Exception as exc:
                    print(f"  1c inject failed: {type(exc).__name__}: {exc}", flush=True)
            try:
                result = _process_chunk(duplex, chunk)
            except Exception as exc:
                print(f"  1c chunk {chunk_idx} ERROR {type(exc).__name__}: {exc}", flush=True)
                break
            text = result.get("text", "") or ""
            is_listen = bool(result.get("is_listen", True))
            if not is_listen:
                spoke = True
            if injected and text:
                post_inject_text.append(text)
                if _RARE.lower() in text.lower() or "context" in text.lower() or "password" in text.lower():
                    voiced_rare = True
            print(f"    1c ch{chunk_idx} listen={int(is_listen)} text={text[:40]!r}", flush=True)
            if injected and is_listen and len(post_inject_text) >= 1:
                # model returned to listen after speaking post-injection; enough
                if chunk_idx > 12:
                    break

        # incorporation positive-control: ask for the password
        incorporated = None
        try:
            ask = _synthesize("What is the secret password?")
            abuf = ask.copy()
            ans_text: list[str] = []
            ci = 0
            while ci < 12:
                ci += 1
                if len(abuf) >= _CHUNK_SAMPLES:
                    c, abuf = abuf[:_CHUNK_SAMPLES], abuf[_CHUNK_SAMPLES:]
                elif len(abuf) > 0:
                    pad = np.zeros(_CHUNK_SAMPLES - len(abuf), dtype=np.float32)
                    c = np.concatenate([abuf, pad]); abuf = np.array([], np.float32)
                else:
                    c = silence
                r = _process_chunk(duplex, c)
                tx = r.get("text", "") or ""
                if tx:
                    ans_text.append(tx)
            joined = " ".join(ans_text)
            incorporated = _RARE.lower() in joined.lower()
            print(f"  1c trial {t+1}: incorporation answer={joined[:60]!r} -> {incorporated}", flush=True)
        except Exception as exc:
            print(f"  1c incorporation check failed: {type(exc).__name__}: {exc}", flush=True)

        results.append({
            "voiced_rare_unprompted": voiced_rare,
            "post_inject_text": " ".join(post_inject_text)[:120],
            "incorporated_when_asked": incorporated,
        })
    return {"trials": results,
            "compliant": all(not r["voiced_rare_unprompted"] for r in results),
            "any_incorporated": any(r["incorporated_when_asked"] for r in results)}


# ---------------------------------------------------------------------------
# 1d — enable_thinking reachability in the duplex path
# ---------------------------------------------------------------------------

def probe_1d(duplex, tokenizer) -> dict:
    findings: dict = {}
    # (a) does a <think> token exist in the vocab?
    think_ids = {}
    for tok in ("<think>", "</think>", "<|think|>", "<thinking>"):
        try:
            tid = tokenizer.convert_tokens_to_ids(tok)
            think_ids[tok] = tid
        except Exception:
            think_ids[tok] = None
    findings["think_token_ids"] = think_ids
    unk = getattr(tokenizer, "unk_token_id", None)
    findings["has_think_token"] = any(
        v is not None and v != unk for v in think_ids.values()
    )
    # (b) does the duplex generate signature accept enable_thinking?
    import inspect
    try:
        sig = inspect.signature(duplex.streaming_generate)
        findings["duplex_generate_accepts_enable_thinking"] = "enable_thinking" in sig.parameters
        findings["duplex_generate_params"] = list(sig.parameters.keys())
    except Exception as exc:
        findings["sig_error"] = f"{type(exc).__name__}: {exc}"
    # (c) does the base model's streaming_generate accept it?
    try:
        base_sig = inspect.signature(duplex.model.streaming_generate)
        findings["base_generate_accepts_enable_thinking"] = "enable_thinking" in base_sig.parameters
    except Exception as exc:
        findings["base_sig_error"] = f"{type(exc).__name__}: {exc}"
    return findings


def main() -> int:
    import torch
    if not torch.cuda.is_available():
        print("ERROR: no CUDA", flush=True); return 1
    free_gb = torch.cuda.mem_get_info()[0] / 1e9
    print(f"CUDA free VRAM: {free_gb:.1f} GB", flush=True)
    if free_gb < 10.0:
        print("GPU busy — set CUDA_VISIBLE_DEVICES.", flush=True); return 1

    print("Loading MiniCPMStreamingModel...", flush=True)
    t0 = time.monotonic()
    from companion_harness.foreground_model_minicpm import (
        MiniCPMStreamingModel, _DEFAULT_DUPLEX_SYSTEM_PROMPT,
    )
    model = MiniCPMStreamingModel()
    duplex = model._duplex
    print(f"  loaded in {round((time.monotonic()-t0)*1000)} ms", flush=True)

    out: dict = {}

    # 1d first (cheap, no state mutation)
    print("\n=== 1d enable_thinking reachability ===", flush=True)
    try:
        out["1d"] = probe_1d(duplex, model._tokenizer)
        print(f"  1d: {json.dumps(out['1d'])}", flush=True)
    except Exception as exc:
        out["1d"] = {"error": f"{type(exc).__name__}: {exc}"}
        print(f"  1d ERROR: {out['1d']}", flush=True)

    # 1b — needs gate bypass + speak-bias
    print("\n=== 1b backchannel discrimination ===", flush=True)
    duplex.listen_prob_scale = 0.3
    type(duplex).current_turn_ended = _AlwaysEnded()
    try:
        out["1b"] = probe_1b(duplex, _DEFAULT_DUPLEX_SYSTEM_PROMPT, trials=2)
    except Exception as exc:
        out["1b"] = {"error": f"{type(exc).__name__}: {exc}"}
        print(f"  1b ERROR: {out['1b']}", flush=True)
    finally:
        del type(duplex).current_turn_ended
        duplex.listen_prob_scale = 1.0

    # 1c — gate intact, protocol prompt
    print("\n=== 1c prompt-compliance ===", flush=True)
    try:
        out["1c"] = probe_1c(duplex, trials=2)
    except Exception as exc:
        out["1c"] = {"error": f"{type(exc).__name__}: {exc}"}
        print(f"  1c ERROR: {out['1c']}", flush=True)

    Path("/tmp/probe-followups.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")

    # ---- summary ----
    lines = ["probe_continuous_followups summary", ""]
    # 1b
    b = out.get("1b", {})
    if "error" not in b:
        s, bc, it = b.get("silence", {}), b.get("backchannel", {}), b.get("interruption", {})
        lines += [
            "1b BACKCHANNEL DISCRIMINATION (chunks-to-yield; lower = yields sooner):",
            f"  SILENCE      : yielded {s.get('n_yield')}/{s.get('n')}  median={s.get('median')}  {s.get('chunks_to_yield')}",
            f"  BACKCHANNEL  : yielded {bc.get('n_yield')}/{bc.get('n')}  median={bc.get('median')}  {bc.get('chunks_to_yield')}",
            f"  INTERRUPTION : yielded {it.get('n_yield')}/{it.get('n')}  median={it.get('median')}  {it.get('chunks_to_yield')}",
        ]
        im, bm, sm = it.get("median"), bc.get("median"), s.get("median")
        if im is not None and (bm is None or im < bm) and (sm is None or im <= sm):
            if bm is None or (sm is not None and bm >= sm) or (bm is not None and sm is not None and abs(bm - sm) <= 1):
                lines.append("  => DISCRIMINATES: interruption yields early; backchannel ~= silence (keeps talking). GO for model-native discrimination.")
            else:
                lines.append("  => PARTIAL: interruption yields earliest, but backchannel also yields sooner than silence. Keep BackchannelClassifier.")
        else:
            lines.append("  => WEAK/NULL: no clear interruption<backchannel ordering. Keep BackchannelClassifier as discrimination layer.")
    else:
        lines.append(f"1b ERROR: {b['error']}")
    lines.append("")
    # 1c
    c = out.get("1c", {})
    if "error" not in c:
        lines += [
            "1c PROMPT-COMPLIANCE (inject [CONTEXT:<rare>] mid-speech):",
            f"  compliant (did NOT voice the rare token unprompted): {c.get('compliant')}",
            f"  incorporated when later asked: {c.get('any_incorporated')}",
        ]
        if c.get("compliant") and c.get("any_incorporated"):
            lines.append("  => background-think VIABLE via in-context protocol (no finetune needed for level 2).")
        elif c.get("compliant"):
            lines.append("  => silent but did not incorporate; protocol partially works. Probe deeper before finetune.")
        else:
            lines.append("  => model VOICED the injected context. In-context protocol unreliable; level-3 finetune likely needed for background-think.")
    else:
        lines.append(f"1c ERROR: {c['error']}")
    lines.append("")
    # 1d
    d = out.get("1d", {})
    if "error" not in d:
        lines += [
            "1d enable_thinking REACHABILITY:",
            f"  has <think> vocab token: {d.get('has_think_token')}  ({d.get('think_token_ids')})",
            f"  duplex.streaming_generate accepts enable_thinking: {d.get('duplex_generate_accepts_enable_thinking')}",
            f"  base.streaming_generate accepts enable_thinking: {d.get('base_generate_accepts_enable_thinking')}",
        ]
        if not d.get("duplex_generate_accepts_enable_thinking"):
            lines.append("  => duplex path does NOT expose thinking; native foreground think-channel not reachable without patching. Stage 5 think-source => injected background model.")
    else:
        lines.append(f"1d ERROR: {d['error']}")

    summary = "\n".join(lines)
    print("\n" + summary, flush=True)
    Path("/tmp/probe-followups-summary.txt").write_text(summary + "\n", encoding="utf-8")
    print("\n=> /tmp/probe-followups.json + summary", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
