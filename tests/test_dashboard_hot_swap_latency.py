"""Contract test — hot-swap p95 latency < 100ms (Dashboard P1 D5).

Per plan §Numeric gates: hot_swap_latency_ms_p95 < 100.

Latency is measured server-side (server-recorded latency_ms from the
model_swap_completed event payload), per plan §OQ-D5.1 lean. The gate covers
50 trials per plan D5 implementation sketch step 5.

Marked @pytest.mark.gpu to allow CI skip on overloaded machines per §Risks
(the gate is load-bearing but tolerance-aware).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from manual_test_console.server import build_app

_TRIALS = 50


def _make_app(tmp_path: Path):
    return build_app(blob_dir=tmp_path / "blobs", live_pipeline_enabled=False)


@pytest.mark.asyncio
async def test_hot_swap_latency_p95_under_100ms(tmp_path: Path) -> None:
    """p95 of server-recorded latency_ms over 50 swap trials must be < 100ms."""
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        latencies: list[int] = []
        async with ClientSession() as session:
            for i in range(_TRIALS):
                seam = "vad"
                enabled = i % 2 == 0  # alternate True/False
                resp = await session.post(
                    f"{base}/config/model-swap",
                    json={"seam": seam, "enabled": enabled},
                )
                assert resp.status == 200
                body = await resp.json()
                assert body["accepted"] is True
                latency = body["latency_ms"]
                assert latency >= 0
                latencies.append(latency)

        assert len(latencies) == _TRIALS
        latencies.sort()
        p95 = latencies[int(_TRIALS * 0.95) - 1]  # 95th percentile (0-indexed: trial 47 of 50)
        assert p95 < 100, (
            f"hot_swap_latency_ms_p95={p95} >= 100ms "
            f"(min={latencies[0]}, max={latencies[-1]}, median={latencies[_TRIALS // 2]})"
        )
    finally:
        await server.close()
