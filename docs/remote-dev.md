# Remote development workflow

> Verified working as of v0.1i (2026-05-15).

## The remote machine

Development runs on a B200 GPU box reachable via `ssh b200` (alias configured in the user's local `~/.ssh/config`). The harness, model weights, and CUDA dependencies live there. The local machine handles editing, git, docs, and lightweight tests only.

## Code sync

Git is the source of truth. Workflow:

1. Edit + commit locally.
2. Push to GitHub (`git push origin <branch>`).
3. On b200: `git pull` in `~/companion-agent-harness/`.

`rsync` or `sshfs` are acceptable alternatives if a feedback loop is too slow for the git roundtrip, but the same rule holds: code flows local -> b200, not the other way. Build artifacts, model checkpoints, datasets, and replay logs **do not** travel back over the wire — they belong on b200.

## Model deployment

Model weights, vLLM / SGLang serving processes, and CUDA dependencies live on b200 only. The local machine never installs `torch`, CUDA, or any GPU runtime. Adapter code on the local side imports against a small protocol or stub; the heavy SDK is loaded only when running on b200.

## Testing

- **Local:** unit tests and Stage 0 replay tests (Tier B — policy-layer determinism over recorded signal traces). These need no GPU.
- **Remote (b200):** integration tests — anything that touches a model. Stage 1 end-to-end fixtures with the ForegroundModel, latency measurements, replay tier A (behavioral) checks.

CI is intended to run the local subset on every commit; the remote subset runs on demand or on a tagged branch.

Manual-test sessions are not a test substitute; contract tests still gate. See `docs/manual-test-handbook.md` §5.

## Environment

- Canonical venv: `/raid/yid042/venvs/companion-harness/`. All pytest invocations (gatekeeper, coder, manual smoke) MUST use `/raid/yid042/venvs/companion-harness/bin/python -m pytest`. Bare `python3` is forbidden — it masked the v0.1b torchless bug (see memory note `feedback_gatekeeper_canonical_venv.md`).
- Adding a dependency: edit `requirements.txt`, `git pull` on b200, then `/raid/yid042/venvs/companion-harness/bin/pip install -r requirements.txt`. Pin with version + hash.
- Do not install packages on the local machine for adapters that only run on b200.

## Reviewer-visibility workflow

External reviewers (GPT-5 helper, external consultants) need PR visibility. The repo defaults to private. To toggle for review:

```sh
# Make public for external review:
gh repo edit Eden-kk/companion-agent-harness --visibility public

# Toggle back after review:
gh repo edit Eden-kk/companion-agent-harness --visibility private
```

Pre-flight before toggling public: see `CLAUDE.md` core invariants — no `.env` or secrets may be in the tree. Public visibility exposes the whole repo, not just the PR. The toggle is operationally irreversible during the public window — anything scraped while public stays scraped. Review tree contents first.

## CI failure debugging

Check PR status:

```sh
gh pr checks <PR_NUMBER>
```

Drill into a failing run:

```sh
gh run view <RUN_ID> --log-failed | head -200
```

CI runs in a different environment than local. Missing entries in `requirements-dev.txt` may pass locally (canonical venv has them installed) but fail in CI — the canonical-venv pin (see §Environment above) catches this class of bug before push.

## Gatekeeper-loop convergence

Every PR runs a coder/spec-gatekeeper converge loop until the latest ledger entry reads `Verdict: APPROVE` or `APPROVE-WITH-NITS`. Ledger at `meta/pr-review-ledger.md`. Reference: memory note `feedback_pr_converge_pipeline.md`. Reviewers do not unilaterally merge — the project lead does, after seeing a clean APPROVE.

## What we do NOT do

- No passwordless-root SSH keys.
- No `sudo NOPASSWD` configuration.
- No modifying `/etc` on b200.
- No installing system packages without explicit confirmation in a session log.
- No checking in model checkpoints, weights, or large datasets to git (see `.gitignore`).
