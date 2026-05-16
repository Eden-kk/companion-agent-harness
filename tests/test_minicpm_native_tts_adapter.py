"""Contract tests for MiniCPMNativeTtsAdapter (v0.1j Task 14 real wiring).

GPU-conditional: tests that actually call the model are skipped when CUDA is
unavailable.  The structural / no-model tests run everywhere.

Success criteria:
  - test_minicpm_native_tts_adapter_synthesizes_audio      GPU required
  - test_tts_adapter_flag_native_minicpm_no_longer_raises  no model load
  - test_kokoro_path_unchanged                             no model load
  - test_no_unavailable_157_marker_in_server               no model load
"""

from __future__ import annotations

import inspect
import pytest


# ---------------------------------------------------------------------------
# Structural / marker checks — no model load, run everywhere
# ---------------------------------------------------------------------------


def test_no_unavailable_157_marker_in_server() -> None:
    """UNAVAILABLE: #157 marker must be gone from server.py."""
    import manual_test_console.server as server_mod

    src = inspect.getsource(server_mod)
    assert "UNAVAILABLE: #157" not in src, (
        "UNAVAILABLE: #157 marker still present in server.py — remove it"
    )


def test_tts_adapter_flag_native_minicpm_no_longer_raises() -> None:
    """_load_native_minicpm_tts_adapter must not raise NotImplementedError at import time.

    We verify the function exists and is callable without immediately raising
    NotImplementedError (the old seam behaviour).  Actual construction requires
    CUDA and is tested in test_minicpm_native_tts_adapter_synthesizes_audio.
    """
    from manual_test_console.server import _load_native_minicpm_tts_adapter

    # The function must exist and not raise NotImplementedError when inspected.
    # We don't call it here — model load requires CUDA (see GPU test below).
    assert callable(_load_native_minicpm_tts_adapter)
    src = inspect.getsource(_load_native_minicpm_tts_adapter)
    assert "NotImplementedError" not in src, (
        "_load_native_minicpm_tts_adapter still raises NotImplementedError"
    )


def test_kokoro_path_unchanged() -> None:
    """Kokoro factory and KokoroTtsAdapter import are unmodified (regression guard)."""
    from companion_harness.tts_kokoro import KokoroTtsAdapter
    from companion_harness.tts_adapter import TtsAdapter
    from manual_test_console.server import _load_kokoro_tts_adapter

    assert callable(_load_kokoro_tts_adapter)
    # KokoroTtsAdapter must still satisfy the Protocol structurally
    import inspect as _i
    sig = _i.signature(KokoroTtsAdapter.synthesize)
    params = list(sig.parameters)
    assert "text" in params and "prosody_tags" in params


def test_minicpm_native_tts_adapter_protocol_shape() -> None:
    """MiniCPMNativeTtsAdapter has the required synthesize signature."""
    from companion_harness.tts_minicpm_native import MiniCPMNativeTtsAdapter

    sig = inspect.signature(MiniCPMNativeTtsAdapter.synthesize)
    params = list(sig.parameters)
    assert "text" in params
    assert "prosody_tags" in params


# ---------------------------------------------------------------------------
# GPU-conditional: actual synthesis
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("torch") or
    not __import__("torch").cuda.is_available(),
    reason="CUDA not available — skipping real MiniCPM-o TTS synthesis test",
)
def test_minicpm_native_tts_adapter_synthesizes_audio() -> None:
    """MiniCPMNativeTtsAdapter.synthesize() returns non-empty PCM16 bytes.

    Loads MiniCPMStreamingModel (init_tts=True, post-PR #197) and drives
    synthesize() via asyncio.run().  Asserts the output is non-empty bytes.
    """
    import asyncio

    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel
    from companion_harness.tts_minicpm_native import MiniCPMNativeTtsAdapter

    model = MiniCPMStreamingModel()
    adapter = MiniCPMNativeTtsAdapter(model)

    async def _collect() -> bytes:
        chunks = []
        async for chunk in adapter.synthesize("Hello.", []):
            chunks.append(chunk)
        return b"".join(chunks)

    result = asyncio.run(_collect())
    assert isinstance(result, bytes)
    assert len(result) > 0, "synthesize() returned empty bytes — TTS produced no audio"
