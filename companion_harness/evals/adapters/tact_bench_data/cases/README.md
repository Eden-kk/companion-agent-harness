# TACT-Bench cases — three layers

The same 28 cases (TC1–TC28) at increasing formality. Pick the layer for your purpose:

| File | Layer | For | Ground truth |
|---|---|---|---|
| `layer1-prose.md` | 1 — prose | humans reading/reviewing | a sentence |
| `layer2-semistructured.yaml` | 2 — semi-structured | inspection, authoring, LLM-judged runs | structured fields + full spoken dialogue + **prose** `ground_truth` |
| `layer3-formal-trajectories.yaml` | 3 — formal | **mechanical scoring** (no human/LLM judge) | per-tick `user_state` + `gt` trajectory |

Layer 3 is the only one a scoring script can grade automatically: each case has a tick grid (`user_state` run-length), the queued `items` with labels + §3.3 fields, and a `gt` map of decisive ticks → action, expanded by the rule documented at the top of that file (WAIT from `t_avail` until the listed tick; then the action; then DONE). Policy: design `§3` + `§3.2` + `§3.3`.

Status: Layer 3 trajectories are **v1, pending human validation** (design §4c) — especially the off-prior (TC14–TC16), conditional/negative/privacy cases (TC10, TC21, TC24, TC27), and any `modality_gated` case (text-only scores under-represent TC13/20/21/22).

Relationship to `../experiments/vanilla-vs-prompted/scenarios.yaml`: that file holds the **full 28-case suite** in full-dialogue runnable form (real user turns + `[context]` cues); these `cases/` files are the same cases in compact layered form. The vanilla-vs-prompted **pilot runs a core subset** (TC1–4, TC14–16, TC17–19); the rest are present for the full run. When the scorer is built, Layer 3 here is the source of truth.
