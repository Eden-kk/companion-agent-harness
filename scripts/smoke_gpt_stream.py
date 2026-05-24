"""Live smoke test for gpt_stream_runner: one case, real WebSocket.

Oracle-floor-timed policy: gpt is triggered only at b/i seam ticks.
Server VAD is disabled (turn_detection={"type":"none"}); if that shape
is rejected by the API, update the session.update call in gpt_stream_runner.py.

Run:
    env OPENAI_API_KEY=sk-... /raid/yid042/venvs/companion-harness/bin/python \
        scripts/smoke_gpt_stream.py [case_id] [arm]

Defaults: TC1, prompted.  Exits 0 on success, non-zero on error.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse

from companion_harness.evals.adapters.gpt_stream_runner import run_case


def main() -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY missing — aborting.", file=sys.stderr)
        return 2

    ap = argparse.ArgumentParser()
    ap.add_argument("case_id", nargs="?", default="TC1")
    ap.add_argument("arm", nargs="?", default="prompted")
    args = ap.parse_args()

    print(f"== smoke_gpt_stream: case={args.case_id}  arm={args.arm} ==", flush=True)
    emissions = run_case(args.case_id, args.arm, realtime=True, k=1)
    print(f"emissions: {len(emissions)}", flush=True)
    for e in emissions:
        print(f"  tick={e.tick}  spoke={e.spoke}  detected={e.detected_item}"
              f"  text={e.text[:80]!r}", flush=True)

    spoke = [e for e in emissions if e.spoke]
    print(f"\nspoke={len(spoke)} ticks; "
          f"deliveries={sum(1 for e in spoke if e.detected_item)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
