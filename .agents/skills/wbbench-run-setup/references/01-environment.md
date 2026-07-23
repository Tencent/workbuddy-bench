# Phase 1 — Environment

Goal: a checkout that can actually run a job.

## Prerequisites (confirm before anything else)

- `uv` installed (`uv --version`). Deps are pinned through `uv`, including the
  exact Harbor git rev — do not `pip install` around it.
- Docker running (`docker info`). Every task runs in a local Docker sandbox;
  Harbor also builds/pulls task images. No Docker → nothing runs.
- Python ≥ 3.12 (`python3 --version`). `uv sync` provisions the interpreter, but
  a system Python below 3.12 will bite you in ad-hoc `python3 -m ...` calls.

## Steps

1. `uv sync` — installs all deps incl. the pinned Harbor rev. Re-run after any
   pull that touches `pyproject.toml` / `uv.lock`.
2. Ensure `.env` exists: `cp .env.example .env` if missing. **Leave the values
   empty for now** — which env-var *names* you need isn't known until the model
   is configured (Phase 2), and the names are driven by the model YAML, not a
   fixed list. `.env.example` is documentation, not a required-var checklist.
3. Ensure the dataset(s) are downloaded (Phase 0, see
   [`00-datasets.md`](00-datasets.md)). A fresh checkout has an empty `datasets/`;
   the run can't proceed without the subset the job targets.

## Common failure modes

- **`uv: command not found`** → install uv first (`https://docs.astral.sh/uv/`),
  then re-run.
- **`Cannot connect to the Docker daemon`** → Docker isn't running / no
  permission. Start Docker; on Linux confirm the user is in the `docker` group.
- **Harbor import / version errors after a pull** → stale env; re-run `uv sync`.
- **Everything below assumes `uv run ...`** — running bare `python3` uses the
  wrong interpreter/env. Always `uv run ./scripts/run.sh ...`.

## Guardrail

Do not edit anything outside `configs/` and `.env` during setup. Dataset and
task files are read-only inputs, downloaded from HuggingFace (Phase 0) rather
than tracked in git.
