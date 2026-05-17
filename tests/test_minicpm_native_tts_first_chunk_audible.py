"""GPU tests: CN7 first-chunk audibility + source-hash pin.

@pytest.mark.gpu: requires CUDA + MiniCPM-o weights on b200.

§5.3: synthesize "Hello world.", RMS of first 200ms > 0.01 (not silence).
§2.3: pin _generate_waveform_from_tokens source hash so upstream changes
      fail loudly rather than silently degrading latency.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect

import numpy as np
import pytest


_PINNED_HASH = "UNSET"  # operator fills in after first successful GPU run


@pytest.mark.gpu
def test_first_chunk_audible():
    """First TTS chunk must not be leading silence (CN7-FIX verification).

    Without CN7-FIX, the 1-second left-pad means first 200ms RMS ≈ 0.
    With fix, first 200ms should contain real audio: RMS > 0.01.
    """
    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel
    from companion_harness.tts_minicpm_native import MiniCPMNativeTtsAdapter

    _SAMPLE_RATE = 24000

    model = MiniCPMStreamingModel()
    adapter = MiniCPMNativeTtsAdapter(model)

    async def _get_first_chunk():
        async for chunk in adapter.synthesize("Hello world.", []):
            return chunk
        return None

    first_chunk = asyncio.run(_get_first_chunk())
    assert first_chunk is not None, "synthesize() yielded no chunks"

    pcm = np.frombuffer(first_chunk, dtype="<i2").astype(np.float32) / 32768.0
    n_200ms = int(_SAMPLE_RATE * 0.2)
    first_200ms = pcm[:n_200ms] if len(pcm) >= n_200ms else pcm
    rms = float(np.sqrt(np.mean(first_200ms ** 2)))
    assert rms > 0.01, (
        f"First 200ms RMS={rms:.4f} — first chunk appears to be silence. "
        "CN7-FIX (1-second left-pad removal) may not be active."
    )


@pytest.mark.gpu
def test_minicpm_native_tts_source_hash_pinned():
    """Pin upstream _generate_waveform_from_tokens source hash (R8 mitigation).

    If MiniCPM-o updates this function, the test fails with a clear message
    directing the operator to re-verify the subclass override in
    companion_harness/tts_minicpm_native.py.

    PINNED_HASH is set to "UNSET" until the operator runs this test on b200
    and fills in the actual hash.
    """
    import glob
    import os

    # Locate MiniCPMODuplex in the HF cache
    hf_cache = os.path.expanduser(
        "~/.cache/huggingface/modules/transformers_modules/openbmb"
    )
    pattern = os.path.join(hf_cache, "MiniCPM-o-4_5", "*", "modeling_minicpmo.py")
    matches = glob.glob(pattern)
    assert matches, f"Could not find modeling_minicpmo.py under {hf_cache}"
    # Use the pinned commit if multiple snapshots exist
    pinned_commit = "6e885630cbe907859c441ff915aa789729f3a5c4"
    pinned_path = [m for m in matches if pinned_commit in m]
    path = pinned_path[0] if pinned_path else matches[0]

    import importlib.util
    spec = importlib.util.spec_from_file_location("modeling_minicpmo", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    src = inspect.getsource(mod.MiniCPMODuplex._generate_waveform_from_tokens)
    actual_hash = hashlib.sha256(src.encode()).hexdigest()

    if _PINNED_HASH == "UNSET":
        pytest.skip(
            f"PINNED_HASH not yet set. Run this test on b200 and set "
            f"_PINNED_HASH = '{actual_hash}' in this file."
        )

    assert actual_hash == _PINNED_HASH, (
        f"MiniCPM-o _generate_waveform_from_tokens source changed. "
        f"Verify that companion_harness/tts_minicpm_native.py "
        f"_generate_waveform_from_tokens_nopad is still correct, then update "
        f"_PINNED_HASH = '{actual_hash}'."
    )
