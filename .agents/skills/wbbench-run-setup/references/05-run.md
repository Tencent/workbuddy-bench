# Phase 5 — Launch

Goal: validate the job, then run it on explicit confirmation.

## 1. Dry-run (always first)

```bash
uv run ./scripts/run.sh --job <slug> --dry-run
```

This resolves the manifest, prints it, and exits — no Docker, no dataset
mutation, no model calls. It runs the same preflight the real run does:
`model_connection` validation, protocol-bridge check, `extra_body`/`direct`
incompatibility check, and a harness-mount preflight. Fix any error and re-run
until it resolves cleanly. Typical dry-run failures:

- **`model_connection=direct is incompatible with this job`** → the model has
  `extra_body` or needs a protocol bridge. Set `model_connection: local_proxy`
  (Phase 4).
- **`Model config not found` / `no top-level 'model:'`** → job's `model:` slug
  doesn't match a `configs/models/<slug>.yaml`.
- **missing local harness mount image** → build it:
  `scripts/harness/build-harness-mounts.sh --harness <family>/<version>`, or pass
  `AUTO_BUILD_HARNESS_MOUNT=1` on the real run.
- **auth/connection errors** → an env-var name the model references is missing
  from `.env` (back to Phase 3).

## 2. Confirm, then run for real

**Ask the user to confirm before the real run** — it is time- and
cost-intensive (real model calls, Docker builds, every task). Only after
explicit confirmation:

```bash
uv run ./scripts/run.sh --job <slug>
```

`--help` lists available jobs and every env knob.

## Env knobs (from `run.sh --help`)

- `SHARDS=N` — parallel Harbor shards (default 1). Also engaged automatically
  when the job has a `task_selection`.
- `SHARD_CONCURRENCY=N` — concurrency within each shard (default: job's
  `n_concurrent_trials`, else 2).
- `NO_FORCE_BUILD=1` — skip Docker image rebuild (faster re-runs).
- `DISABLE_VERIFICATION=1` — agent rollout only; skip task verification + the
  host-side LLM judge.
- `AUTO_BUILD_HARNESS_MOUNT=1` — build a missing local harness mount image.
- `PROXY_PORT=N` — preferred job-private proxy port (default 3456; auto-bumps if
  busy).
- `SHARED_PROXY=1` — reuse one long-lived shared proxy on :3456 instead of a
  per-job one. Start it first: `scripts/proxy/proxy-shared.sh start`.

Both `SHARDS` and `SHARD_CONCURRENCY` must be positive integers — `run.sh`
rejects non-numeric/zero values early.

## Where results land

`<bench.jobs_dir>/<slug>/<timestamp>/` (bench `jobs_dir` defaults to `results`).
A run dir contains `config.json`, `result.json`, `job.log`, and per-trial dirs
(patches, verifier output, trajectories). With `record_full_io`, per-trial
`agent/requests.jsonl` is populated after the run.

## Guardrails

- Never kill proxies by port/pattern — `run.sh` stops only the job-private proxy
  it started (verified PID + config). Leave shared proxies alone.
- Do not launch the real run without user confirmation.

When the run finishes, hand off to analysis — see [06-analysis.md](06-analysis.md).
