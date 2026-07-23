# Phase 4 — Job

Goal: write `configs/jobs/<slug>.yaml` — a *pure composition* of model +
harness + dataset, plus optional overrides.

**Read `configs/jobs/_template.job.yaml` and `configs/jobs/_reference.yaml`
first.** The template is the minimal required set; `_reference.yaml` documents
every overlay block field-by-field. `configs/jobs/` is gitignored by default —
only a few examples ship.

## Required fields

```yaml
model: <provider>/<slug>          # a configs/models/<...>.yaml slug (Phase 2)
harness: <family>/<version>       # e.g. codebuddy-code/2.103.4
dataset: datasets/<dataset-id>/tasks
harness_backend: local            # only local Docker is supported
model_connection: local_proxy     # direct | local_proxy  (no implicit selection)
```

## Choosing the harness — discover, never hardcode

List live options: `ls configs/harnesses/*/versions/`. The slug is
`<family>/<version>` where `<version>` is the file stem (drop `.yaml`). Two
families exist in this repo (`claude-code`, `codebuddy-code`), each with several
pinned versions — but always read the directory rather than trusting this list;
versions get added/removed.

## Choosing the dataset — discover, then read its metadata

List versioned datasets: `ls -d datasets/*/`. Fill the job as
`dataset: datasets/<id>/tasks`. To decide *what a dataset evaluates and when to
use it*, read that dataset's `dataset.toml` (task count, category distribution,
scoring plan) — do not guess from the directory name. Each dataset also has a
matching `configs/bench/<dataset-id>.yaml` carrying run invariants + context
window; `_default.yaml` is the base both merge from.

## `model_connection` — prefer `local_proxy`

`local_proxy` is the **recommended default**; use it unless there's a specific
reason not to. It stands up a small FastAPI proxy between the harness and the
backend that does three things:

- **Protocol bridging** — translates Anthropic↔OpenAI when the harness and
  backend don't speak the same protocol.
- **`extra_body` injection** — forwards sampling params the harness can't send
  natively (`top_k` / `min_p` / `chat_template_kwargs`, ...).
- **Request logging** — captures every request/response for later inspection
  (this is what `record_full_io` writes; unavailable under `direct`).

It works purely at the transport layer and never alters model content, so
turning it on costs nothing — you gain observability and a uniform path and lose
nothing. Mechanism lives in `src/workbuddy_bench/proxy/README.md`.

`direct` sends the harness straight to `model.backend_url_env`: simpler, but no
param injection, no request logging, no protocol conversion. Choose it only when
you deliberately want the harness talking to the backend unmediated.

**When `local_proxy` is mandatory** — set it automatically, don't make the user
discover it: the model uses `extra_body`, **or** the harness protocol isn't in
the model's `protocols`. `direct` fails fast in both cases (see `run.sh`'s
incompatibility check). Otherwise still default to `local_proxy`, and fall back
to `direct` only on explicit user request.

### Starting it

Nothing to start by hand in the common case: under `local_proxy`, `run.sh`
brings up a **job-private** proxy on `:3456` (bumping to a free port if that's
taken) at launch and tears it down when the run ends. Only the opt-in **shared**
proxy needs a manual start — `SHARED_PROXY=1` plus
`scripts/proxy/proxy-shared.sh start` (see [05-run.md](05-run.md)).

## Slug convention

`{model}-{harness-short}-{dataset-or-purpose}`. **No timestamps** — Harbor
auto-creates timestamped run dirs; the job filename is a reusable experiment id.
The `<slug>` is what you pass to `--job` and where results land
(`<bench.jobs_dir>/<slug>/<timestamp>/`).

## Common optional knobs (all from `_reference.yaml`)

- `n_attempts:` — attempts per task (default from bench config).
- `timeout_multiplier:` — scale task timeouts.
- `record_full_io: true` — capture every model request/response (local_proxy
  only); split per-trial into `.../<trial>/agent/requests.jsonl` after the run.
- `task_selection:` — run a subset instead of all tasks. Modes: `all` (default),
  `first`/`last` + `count`, `index` + `indices`, `random` + `count` (+`seed`),
  `name` + `names`. A selection routes the run through the sharded executor.
- Overlays (deep-merged onto the bench/harness/model layers):
  `orchestrator_override`, `environment_override`, `model_params_override`
  (most common — e.g. tweak `max_output_tokens` / `extra_body`),
  `harness_params_override`, `env_override`, and `context_window` /
  `context_compact_pct`.

> Disabled tools are **not** a job knob — they live in the harness settings
> preset `configs/harnesses/<family>/presets/settings.json` under
> `permissions.deny`.

## Guardrails

- Write only under `configs/`. Treat datasets/tasks as read-only.
- Show the user the composed job YAML before writing it.

Validation + launch are in [05-run.md](05-run.md).
