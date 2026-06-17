# Subplan — PR6: form discrimination cases + Mode-A NOW/WAIT/DROP probe

Closes the two measurement gaps the earlier runs exposed. Spans benchmark
definition (tact-bench `scenarios.yaml`, re-vendored) + driver (harness).

## (a) Make `conditional_form` discriminate

Today every form case expects BRIEF, so the metric can only rubber-stamp brevity.
Add two cases (→ 14 total; FORM behavior count 3→5):
- **TC13-form-full** — user actively debugging, asked for "the full rundown"; a
  terse answer under-informs. `expected_form: FULL`.
- **TC14-form-verbose-trap** — user asked for "the one-sentence headline"; the
  payload is long and invites over-explaining. `expected_form: BRIEF`.

Metric change: `conditional_form` compares the delivered form against the case's
`expected_form` (default `BRIEF` when absent, so the existing 6 form cases are
unchanged). `_case_facts` reads `expected_behavior["expected_form"]`; `TactCaseSource`
carries `scenario["expected_form"]` into `expected_behavior`.

Effect: a model that always delivers BRIEF is now correct on TC14 but **wrong** on
TC13 (under-informs); a model that dumps FULL is wrong on TC14 — so the metric
separates brief-correct from verbose-wrong.

## (b) Mode-A structured NOW/WAIT/DROP probe

The native gate (Mode B) never defers, so breakpoint-hit is always n/a — we can't
tell "can't defer" from "won't defer." Mode A asks the model, at each decision point
from `t_available` on, to emit one of `NOW / WAIT / DROP` given the conversation so
far + the held item + whether the user is mid-utterance.

Driver method `mode_a_labels(case) -> [{t, decision, user_speaking}]`:
- reuse `_build_text_plan` for the timeline + speaking mask;
- per tick build a text probe (history through t + item + speaking flag) and call
  `model.chat(prompt, max_new_tokens=4)`; parse NOW/WAIT/DROP; stop after NOW/DROP.
- **Prompt-constrained**, not logit-constrained decode (a future refinement); the
  experiment README explicitly allows the "answer with one word" form. Documented.

The A-vs-B gap (does structured prompting elicit deferral that the native gate
doesn't?) is the headline this enables. CLI wiring of Mode A is deferred; the driver
method + hermetic test satisfy the PR6 success criterion.

## Counts to update
registry `case_count` 12→14; `test_tact_case_source` (14 total, FORM=5);
`test_tact_suite` (14 event logs / n_cases); scenarios header + tact-bench
README/REVISIONS (12→14). test_tact_metrics is synthetic → unaffected (adds new
form-discrimination assertions).

## Success criterion (PR6)
`conditional_form` scores a FULL delivery correct on TC13 and wrong on TC14 (and the
reverse for BRIEF); `mode_a_labels` emits per-decision-point labels on a DEFER case
(WAIT while the user speaks, NOW at the breakpoint, with a fake model). Hermetic.
