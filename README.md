# companion-agent-harness

## What this is

Implementation harness for an architecture hypothesis on realtime multimodal companion agents — an agent that perceives audio + video alongside the user, remembers them across sessions, and chooses silence almost always. The spec is **frozen at v0.1** in [`docs/architecture-v0.1.md`](docs/architecture-v0.1.md). This repo's job is to instantiate that spec, not to iterate on it.

## How to use this repo

- For Claude Code sessions: read [`CLAUDE.md`](CLAUDE.md). It encodes the project's coding discipline and non-negotiable invariants.
- For what to build next: read [`ROADMAP.md`](ROADMAP.md). The current milestone is **v0.1a**.

## Development environment

Code runs on a remote B200 GPU box; see [`docs/remote-dev.md`](docs/remote-dev.md) for the workflow.
