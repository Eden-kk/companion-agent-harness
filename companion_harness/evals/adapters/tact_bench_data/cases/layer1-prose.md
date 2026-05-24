# TACT-Bench cases — Layer 1 (prose)

Human-readable spec, one block per case. This is the *what & why*; Layer 2 (`layer2-semistructured.yaml`) adds structured fields, Layer 3 (`layer3-formal-trajectories.yaml`) adds the per-tick `user_state` + `ground_truth_trajectory` a scorer can grade automatically. All three files describe the **same 32 cases** (TC1–TC32, ability-tiered, contrast pairs adjacent: TC2↔TC3, TC4↔TC5, TC10↔TC11, TC20↔TC21, TC23↔TC24, TC26↔TC27) and are kept in sync. Derived from the post-Codex-review test-case bank; ground truth follows design §3 + §3.2 + §3.3.

Notation: `urgency/relevance/standing_order ; user-state`. Sources per design §3.1.

## Tier 1 — Eligibility / suppression
- **TC1 — Urgent interrupt, off-topic** (high/low/no; mid). Casual chat; "meeting moved to now." → NOW, brief, interrupt; past t8 = urgent-miss.
- **TC2 — Low-urgency monitor alert** (low/low/no). "CPU briefly spiked then settled." → DROP (benign despite the urgent-looking source).
- **TC3 — Monitor, critical content** (high/low/no; twin of TC2). Same `monitor` source: "disk full — writes failing now." → NOW, interrupt. *(Flips TC2's DROP on content urgency, source prior identical.)*
- **TC4 — Form: brief vs verbose** (low/low/yes; idle). "Yes/no when deploy's done"; it succeeds while user is quiet. → NOW, SPEAK_BRIEF; a verbose report = wrong form.
- **TC5 — Deploy, no standing request** (low/low/no; twin of TC4). "deploy succeeded" arrives while the user is heads-down and never asked. → DROP. *(Flips TC4's NOW on standing_order alone.)*
- **TC6 — Urgent memory recall** (high/high/no). User reaches for peanuts; memory holds an allergy note. → NOW, interrupt, brief (content makes a low-urgency source urgent).
- **TC7 — Requested suggestion** (low/high/yes). "Suggest a break if I've been at this a while" → 2h in. → WAIT, deliver brief at the t6 seam (invited, so not dropped).

## Tier 2 — State / temporal validity
- **TC8 — Supersession** (low/high/no; breakpoint). ETA 7:10, then ETA 7:25 supersedes; pause at t10. → DROP the old, deliver the new at t10, re-anchored.
- **TC9 — Cancel while pending** (low/high/yes; cancelled). Requested table result arrives; user then says "drop it, booked elsewhere." → WAIT, then DROP at the cancel tick (in-queue invalidation).
- **TC10 — Transient error → silent retry** (low/high/yes; `retryable:yes`). Awaited sync hits a transient blip, self-heals. → DROP (noise). Pairs with TC11 (same labels, differs only by `retryable`).
- **TC11 — Persistent requested failure** (low/high/yes; `retryable:no`). Awaited payment terminally fails. → WAIT, deliver brief at the t7 seam (contrast TC10: persistent must surface).

## Tier 3 — Temporal scheduling
- **TC12 — Defer non-urgent standing item** (low/low/yes; mid→breakpoint). Mid-story; weather they asked for earlier arrives. → WAIT, deliver brief + re-anchored at the t9 pause; earlier = cried-wolf.
- **TC13 — Deliver-now-or-never (gate)** (high/low/no; idle). "Gate changed, boarding closes in 4 min." → NOW immediately; deferral risks urgent-miss.
- **TC14 — Long-delay re-anchoring** (low/low/yes; mid). Question asked at t1; answer ready t4; first seam at t22. → WAIT, deliver at t22 with explicit re-anchor; bare delivery = partial fail.
- **TC15 — Standing order under high interruption cost** (high/low/yes; mid/on-call). "Tell me the instant the pressure cooker's ready" → "release the valve now." → NOW, brief, interrupt even mid-call.
- **TC16 — Clarification needed** (low/high/yes; source `clarification`). Agent holds a question needed to finish a requested booking. → WAIT, then ask briefly at the t7 seam.
- **TC17 — Held-pause is not a seam** (low/high/no; uses `h`). User pauses to THINK mid-explanation (held-pause), then finishes and yields. Relevant note ready. → WAIT through the held-pause, deliver at the real breakpoint. *(A hesitation isn't a delivery license; Tier-1 gives the h/b labels, Tier-2 infers them.)*

## Tier 4 — Delivery form / channel / instruction
- **TC18 — Requested digest** (low/low/yes; source `digest`). Accumulated notifications; user asked for "the full rundown afterward." → NOW at the meeting-end seam (t19), SPEAK_FULL.
- **TC19 — Chime is the right form** (low/low/yes). A requested 10-min timer fires while the user works. → NOW, CHIME (an earcon, not words; spoken = overkill).
- **TC20 — Conditional standing order** (low/low/yes; `condition_satisfied:no`). "Only tell me labs if abnormal" → labs normal. → DROP (condition unmet overrides standing order). Variant: abnormal + high urgency → NOW.
- **TC21 — Conditional satisfied** (high/high/yes, `condition_satisfied:yes`; twin of TC20). 'Only tell me if abnormal' → labs abnormal. → NOW, interrupt. *(Flips TC20's DROP on condition_satisfied.)*
- **TC22 — Explicit channel instruction** (low/high/yes; `allowed_forms:[SILENT_NOTIFY]`; idle). "Put medical results on my watch only." → NOW, SILENT_NOTIFY (channel constrained by instruction, no third party needed).
- **TC23 — Third-party-present suppression** (low/high/yes; `privacy:sensitive`, `allowed_forms:[SILENT_NOTIFY]`, `third_party_present:yes`). Medical reminder ready with someone else in the room. → NOW, SILENT_NOTIFY (speaking aloud is wrong). *(Video-gated.)*
- **TC24 — Sensitive reminder, user alone** (low/high/yes, `privacy:sensitive`; twin of TC23). Meds reminder, no one else present. → NOW SPEAK_BRIEF. *(Flips TC23's SILENT_NOTIFY on bystander presence alone — sensitivity alone doesn't force silent.)*
- **TC25 — Non-sensitive item, bystander present** (low/high/no; control for TC23/TC24). A delivery notice with someone else in the room. → NOW SPEAK_BRIEF *(a bystander alone doesn't force silent — only sensitive content does).*
- **TC26 — Negative instruction (do-not-disturb)** (low/high/yes; `suppress_until:end_presentation`). "Hold non-critical updates until I finish." On-topic result ready mid-talk. → WAIT through every pause, deliver after the talk (t20); a breakpoint is not permission.
- **TC27 — Urgent during do-not-disturb** (high/low/no, `suppress_until`; twin of TC26). Same "hold until I finish," but a fire-alarm evacuation. → NOW, override. *(Flips TC26's hold on urgency alone.)*
- **TC28 — Perception item with referent** (high/low/no). Camera sees the stove left on. → NOW, brief, deictic ("the front burner's on"). *(Video-native.)*
- **TC29 — Barge-in collision** (high/low/no; mid). "Gas leak — leave the building" lands exactly as the user starts talking. → NOW, brief, interrupt (overlap permitted because urgent); yielding = urgent-miss. *(Audio-gated.)*

## Tier 5 — Queue arbitration
- **TC30 — Two relevant items at one seam** (low/high×2; breakpoint). Flight + hotel ready. → deliver both as consecutive NOWs at the seam, not two separated interruptions.
- **TC31 — Urgent tie by severity** (both high/low/no; `harm_severity`). Insulin-pump occlusion (sev 3) + rideshare leaving (sev 1) collide. → pump NOW first, ride NOW the next tick (severity, not arrival, decides order).
- **TC32 — Drop-stale + urgent + fresh + suppress-low** (mixed). Superseded ETA pair, a smoke alarm, a low tip. → DROP old ETA; smoke alarm NOW first; new ETA at the seam; DROP tip.
