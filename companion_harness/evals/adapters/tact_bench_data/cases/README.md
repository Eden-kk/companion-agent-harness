# TACT-Bench cases — three layers

The same 28 cases (TC1–TC29, no TC5) at increasing formality. Pick the layer for your purpose:

| File | Layer | For | Ground truth |
|---|---|---|---|
| `layer1-prose.md` | 1 — prose | humans reading/reviewing | a sentence |
| `layer2-semistructured.yaml` | 2 — semi-structured | inspection, authoring, LLM-judged runs | structured fields + **prose** `ground_truth` |
| `layer3-formal-trajectories.yaml` | 3 — formal | **mechanical scoring** (no human/LLM judge) | per-tick `user_state` + `gt` trajectory |

Layer 3 is the only one a scoring script can grade automatically: each case has a tick grid (`user_state` run-length), the queued `items` with labels + §3.3 fields, and a `gt` map of decisive ticks → action, expanded by the rule documented at the top of that file (WAIT from `t_avail` until the listed tick; then the action; then DONE). Policy: design `§3` + `§3.2` + `§3.3`.

Status: Layer 3 trajectories are **v1, pending human validation** (design §4c) — especially the off-prior (TC15–17), conditional/negative/privacy cases (TC11, TC22, TC25, TC28), and any `modality_gated` case (text-only scores under-represent TC14/21/22/23).

Relationship to `../experiments/vanilla-vs-prompted/scenarios.yaml`: that file is the **pilot subset** (10 cases) in the Layer-2 style; these `cases/` files are the full, layered set. When the scorer is built, Layer 3 here is the source of truth.
