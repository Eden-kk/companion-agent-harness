"""S3 smoke test: run TC1 arm=prompted input_modality=audio on real GPU.

Usage:
    /raid/yid042/venvs/companion-harness/bin/python scripts/smoke_minicpm_stream.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# ensure repo root on path
sys.path.insert(0, str(Path(__file__).parent.parent))


def main() -> int:
    print("smoke_minicpm_stream: loading MiniCPMStreamingModel...", flush=True)
    t0 = time.monotonic()
    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel

    model = MiniCPMStreamingModel()
    print(f"  loaded in {round((time.monotonic() - t0) * 1000)}ms", flush=True)

    from companion_harness.evals.adapters.minicpm_stream_runner import run_case

    print("\nrunning TC1, arm=prompted, input_modality=audio ...", flush=True)
    t1 = time.monotonic()
    emissions = run_case("TC1", "prompted", "audio", model)
    elapsed = round((time.monotonic() - t1) * 1000)

    print(f"\n--- TC1 emissions ({len(emissions)} ticks, {elapsed}ms) ---")
    for e in emissions:
        marker = "SPOKE" if e.spoke else "     "
        det = f"  detected={e.detected_item}({e.form}, conf={e.confidence:.2f})" if e.detected_item else ""
        print(f"  t={e.tick:2d} {marker}  {e.text[:80]!r}{det}")

    delivered = [e for e in emissions if e.detected_item is not None]
    spoke = [e for e in emissions if e.spoke]
    print(f"\nspoke={len(spoke)} ticks, deliveries={len(delivered)}")

    if not any(e.spoke for e in emissions):
        print("WARNING: model never spoke — check gate relax and duplex state")
        return 2
    print("PASS — smoke completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
