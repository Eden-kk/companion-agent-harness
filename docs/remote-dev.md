# Remote development workflow

> **TODO: Verify b200 host and venv on first remote session — not validated at bootstrap.**

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

## Environment

- Python venv path: `~/companion-agent-harness/.venv` on b200.
- `requirements.txt` lives in the repo. It is empty at bootstrap; add dependencies as adapters land, pinned with version + hash.
- Do not install packages on the local machine for adapters that only run on b200.

## What we do NOT do

- No passwordless-root SSH keys.
- No `sudo NOPASSWD` configuration.
- No modifying `/etc` on b200.
- No installing system packages without explicit confirmation in a session log.
- No checking in model checkpoints, weights, or large datasets to git (see `.gitignore`).
