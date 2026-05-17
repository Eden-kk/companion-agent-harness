"""GPU test: B3 listen suppression — 200 utterances, zero is_listen on first chunk.

@pytest.mark.gpu: requires CUDA + MiniCPM-o weights on b200.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest


@pytest.mark.gpu
def test_no_listen_on_first_chunk_200_utterances():
    """200 short TTS calls — none should complete with is_listen=True on first gen.

    B3-FIX verification: current_turn_ended=False activates listen→tts_bos remap.
    Without the fix, ~small fraction of calls emit is_listen on first streaming_generate.
    """
    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel
    from companion_harness.tts_minicpm_native import MiniCPMNativeTtsAdapter

    model = MiniCPMStreamingModel()
    adapter = MiniCPMNativeTtsAdapter(model)

    texts = [
        "Hello world.",
        "Testing one two three.",
        "The quick brown fox.",
        "How are you today?",
        "This is a short test.",
    ] * 40  # 200 utterances

    listen_first_count = 0

    async def _run_one(text):
        nonlocal listen_first_count
        chunks = []
        async for chunk in adapter.synthesize(text, []):
            chunks.append(chunk)
        # If no chunks produced, that indicates is_listen or end_of_turn fired immediately
        if not chunks:
            listen_first_count += 1

    async def _run_all():
        for text in texts:
            await _run_one(text)

    asyncio.run(_run_all())

    assert listen_first_count == 0, (
        f"{listen_first_count}/200 utterances produced zero audio (likely is_listen=True). "
        "B3-FIX may not be active — verify current_turn_ended=False is set after prepare()."
    )
