"""One-way sync from a tact-bench checkout into the harness's vendored cases directory.

Usage:
    python scripts/sync_tact_bench.py [--from /path/to/tact-bench]

The tact-bench checkout must be on a branch containing the 32-case bank + arms.yaml
(master lineage). By default, syncs from /home/yid042/projects/tact-bench.

Copies into companion_harness/evals/adapters/tact_bench_data/cases/:
  - cases/layer1-prose.md
  - cases/layer2-semistructured.yaml
  - cases/layer3-formal-trajectories.yaml
  - cases/arms.yaml
  - experiments/vanilla-vs-prompted/scenarios.yaml -> scenarios.yaml
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

_HARNESS_ROOT = Path(__file__).parent.parent
_DEST_CASES = _HARNESS_ROOT / "companion_harness" / "evals" / "adapters" / "tact_bench_data" / "cases"

_FILE_MAP: list[tuple[str, str]] = [
    ("cases/layer1-prose.md", "layer1-prose.md"),
    ("cases/layer2-semistructured.yaml", "layer2-semistructured.yaml"),
    ("cases/layer3-formal-trajectories.yaml", "layer3-formal-trajectories.yaml"),
    ("cases/arms.yaml", "arms.yaml"),
    ("experiments/vanilla-vs-prompted/scenarios.yaml", "scenarios.yaml"),
]


def sync(tact_bench_root: Path) -> None:
    _DEST_CASES.mkdir(parents=True, exist_ok=True)
    for src_rel, dest_name in _FILE_MAP:
        src = tact_bench_root / src_rel
        dest = _DEST_CASES / dest_name
        if not src.exists():
            print(f"WARNING: source not found, skipping: {src}")
            continue
        shutil.copy2(src, dest)
        print(f"copied {src} -> {dest}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="tact_bench", default="/home/yid042/projects/tact-bench",
                        help="Path to tact-bench checkout (default: /home/yid042/projects/tact-bench)")
    args = parser.parse_args()
    sync(Path(args.tact_bench))


if __name__ == "__main__":
    main()
