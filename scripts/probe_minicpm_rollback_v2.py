#!/raid/yid042/venvs/companion-harness/bin/python
"""Probe: test KV-rollback paths on real MiniCPM-o-4.5 duplex.

Path A: manually assign save_speculative_snapshot() return to base._speculative_snapshot
Path B: _truncate_llm_cache — capture LLM cache length pre/post generate and rewind
Path C: _drop_round — not applicable to duplex (duplex doesn't use round tracking)

Writes verdict to artifacts/snapshot_api_verdict_v2.json.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import torch
from transformers import AutoModel

_ROOT = Path(__file__).resolve().parent.parent
_SILENCE = np.zeros(16000, dtype=np.float32)
_MODEL_ID = "openbmb/MiniCPM-o-4_5"

verdict: dict = {
    "path_a_manual_save_works": False,
    "path_a_preconditions_required": "",
    "path_b_truncate_llm_cache_works": False,
    "path_c_drop_round_works": False,
    "recommended_path": "none",
    "notes": "",
}

notes = []


def get_kv_len(base) -> int:
    if base.llm_past_key_values is None:
        return 0
    if hasattr(base.llm_past_key_values, "key_cache") and base.llm_past_key_values.key_cache:
        return base.llm_past_key_values.key_cache[0].shape[-2]
    return 0


def main() -> None:
    print("Loading model …", flush=True)
    t0 = time.monotonic()
    base = AutoModel.from_pretrained(_MODEL_ID, trust_remote_code=True, torch_dtype=torch.bfloat16).to("cuda")
    base.eval()
    print(f"Loaded in {time.monotonic()-t0:.1f}s", flush=True)

    duplex = base.as_duplex(generate_audio=True, chunk_ms=1000)
    sys_prompt = "You are a helpful assistant. Speak briefly in English."
    duplex.prepare(prefix_system_prompt=sys_prompt)

    # Warmup tick
    print("Warmup prefill + generate …", flush=True)
    duplex.streaming_prefill(audio_waveform=_SILENCE)
    r0 = duplex.streaming_generate()
    notes.append(f"warmup: is_listen={r0.get('is_listen')}")
    print(f"warmup done: is_listen={r0.get('is_listen')}", flush=True)

    # ---- Path A: manual save with return-value captured ----
    print("\n--- Path A ---", flush=True)
    try:
        # Before generate: manually save snapshot and assign it
        snap = base.save_speculative_snapshot()
        base._speculative_snapshot = snap
        has_before = base.has_speculative_snapshot()
        notes.append(f"path_a: has_snapshot after manual assign = {has_before}")
        print(f"  has_snapshot after assign: {has_before}", flush=True)

        if has_before:
            kv_before = get_kv_len(base)
            duplex.streaming_prefill(audio_waveform=_SILENCE)
            r1 = duplex.streaming_generate()
            kv_after = get_kv_len(base)
            notes.append(f"path_a: kv_before={kv_before} kv_after={kv_after} is_listen={r1.get('is_listen')}")
            print(f"  kv_before={kv_before} kv_after={kv_after}", flush=True)

            # Restore
            restored = base.restore_speculative_snapshot()
            kv_restored = get_kv_len(base)
            has_after = base.has_speculative_snapshot()
            notes.append(f"path_a: restore()={restored} kv_restored={kv_restored} has_after={has_after}")
            print(f"  restore()={restored} kv_restored={kv_restored}", flush=True)

            if restored:
                # One more generate to verify no crash
                duplex.streaming_prefill(audio_waveform=_SILENCE)
                r2 = duplex.streaming_generate()
                notes.append(f"path_a: post-restore generate ok, is_listen={r2.get('is_listen')}")
                print(f"  post-restore generate ok: is_listen={r2.get('is_listen')}", flush=True)
                verdict["path_a_manual_save_works"] = True
                verdict["path_a_preconditions_required"] = (
                    "Assign return value of save_speculative_snapshot() to base._speculative_snapshot; "
                    "no internal flags required."
                )
        else:
            notes.append("path_a: has_snapshot False even after manual assign — unexpected")
    except Exception as e:
        notes.append(f"path_a: EXCEPTION {e}")
        print(f"  EXCEPTION: {e}", flush=True)

    # ---- Path B: _truncate_llm_cache ----
    print("\n--- Path B ---", flush=True)
    try:
        kv_pre = get_kv_len(base)
        duplex.streaming_prefill(audio_waveform=_SILENCE)
        r3 = duplex.streaming_generate()
        kv_post = get_kv_len(base)
        notes.append(f"path_b: kv_pre={kv_pre} kv_post={kv_post} delta={kv_post-kv_pre}")
        print(f"  kv_pre={kv_pre} kv_post={kv_post}", flush=True)

        base._truncate_llm_cache(kv_pre)
        kv_truncated = get_kv_len(base)
        notes.append(f"path_b: kv_truncated={kv_truncated} (target={kv_pre})")
        print(f"  kv_truncated={kv_truncated}", flush=True)

        # One more generate post-truncation
        duplex.streaming_prefill(audio_waveform=_SILENCE)
        r4 = duplex.streaming_generate()
        notes.append(f"path_b: post-truncate generate ok, is_listen={r4.get('is_listen')}")
        print(f"  post-truncate generate ok: is_listen={r4.get('is_listen')}", flush=True)
        verdict["path_b_truncate_llm_cache_works"] = kv_truncated == kv_pre
    except Exception as e:
        notes.append(f"path_b: EXCEPTION {e}")
        print(f"  EXCEPTION: {e}", flush=True)

    # ---- Path C: _drop_round ----
    print("\n--- Path C ---", flush=True)
    try:
        pre_round_id = getattr(base, "_next_round_id", None)
        notes.append(f"path_c: _next_round_id before generate = {pre_round_id}")
        # _drop_round needs a DynamicCache — duplex doesn't use base's round tracking
        # in streaming mode. Just report it's N/A.
        notes.append("path_c: duplex streaming_generate doesn't call _finalize_round; _drop_round N/A")
        print("  _drop_round N/A for duplex mode", flush=True)
        verdict["path_c_drop_round_works"] = False
    except Exception as e:
        notes.append(f"path_c: EXCEPTION {e}")

    # Verdict
    if verdict["path_a_manual_save_works"]:
        verdict["recommended_path"] = "a"
    elif verdict["path_b_truncate_llm_cache_works"]:
        verdict["recommended_path"] = "b"
    verdict["notes"] = " | ".join(notes)

    out = _ROOT / "artifacts" / "snapshot_api_verdict_v2.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(verdict, indent=2))
    print(f"\nVerdict written to {out}")
    print(json.dumps(verdict, indent=2))


if __name__ == "__main__":
    main()
