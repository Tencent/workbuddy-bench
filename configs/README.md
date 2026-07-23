# Configuration system

**English** | [简体中文](README.zh.md)

Every input to an evaluation run lives here. The four subdirectories are the four
layers. At runtime they are first **deep-merged**, then resolved into a single
**resolved manifest JSON**, and finally rendered into the **runtime config YAML**
handed to Harbor (the difference between the last two is explained under
"Artifacts: manifest JSON vs. runtime config YAML" below):

```
configs/bench/<dataset_id>.yaml       ← run invariants + context window (per dataset)
configs/harnesses/<family>/…          ← identity, params, and env of the agent CLI under test
configs/models/<provider>/<slug>.yaml ← model identity, backend URL/KEY env names, sampling params
configs/jobs/<slug>.yaml              ← composition of the three above + per-run overrides
```

Merge order (later overrides earlier):

```
bench/_default.yaml
  → bench/<dataset_id>.yaml
    → harnesses/<family>/<version>.yaml
      → models/<slug>.yaml
        → jobs/<slug>.yaml
```

The merge is performed by `workbuddy_bench.runner.prepare_job`; the rendered
runtime config YAML is written to `.workspace/data/generated/jobs/` (a local,
gitignored working directory). Credentials never enter these YAML files — the
files carry only **env variable names**, while the real secrets stay in `.env`.

### Artifacts: manifest JSON vs. runtime config YAML

A run produces two easily-confused artifacts with different paths and purposes:

- **Resolved manifest JSON** —
  `scripts/logs/instances/<instance-id>/manifest.json`.
  Produced by `resolve_manifest`; the complete resolved result of a single run
  (model / harness / connection / context window / `harness_runtime_config`
  audit block, etc.), consumed downstream by `validate_model`, `proxy_config`,
  `prepare_job`, `sharded_eval`, and others.
- **Harbor runtime config YAML** —
  `.workspace/data/generated/jobs/<job>.yaml`.
  Produced by `prepare_job` and passed to `harbor run -c`; the actual runtime
  config fed to Harbor. Both the sharded and non-sharded paths now write here.

Execution chain:

```
job/model/harness/bench YAML
  → resolve_manifest
  → scripts/logs/instances/<instance-id>/manifest.json   (resolved manifest JSON)
  → prepare_job
  → .workspace/data/generated/jobs/<job>.yaml            (Harbor runtime config YAML)
  → harbor run
```

---

## bench/ — per-dataset run invariants

`load_bench` first reads `_default.yaml` (the bench-wide fallback), then layers
`<dataset_id>.yaml` on top. `<dataset_id>` is the dataset directory name (also the
`[dataset].id` in `dataset.toml`), derived from the job's `dataset:` path.

Main fields:

| Field | Meaning |
|---|---|
| `n_attempts` | How many times each task is repeated (to estimate variance) |
| `n_concurrent_trials` | Number of concurrent trials (top-level) |
| `timeout_multiplier` | Global multiplier applied to every task's declared timeout |
| `jobs_dir` | Root for result output (external = `results`) |
| `environment` | Container environment: `type: docker` + custom `WorkBuddyDockerEnvironment` |
| `agent_user` / `verifier_user` | Container exec user (default `dev`; empty = root) |
| `context_window` / `context_compact_pct` | Context window + compaction trigger ratio |
| `llm_judge` | Optional Layer-3 LLM judge (off by default) |

Scoring is not configured here — each dataset's `dataset.toml` / `task.toml`
points `[verifier].import_path` at `workbuddy_bench.judge:CompositeVerifier`; see
[Scoring](#scoring-compositeverifier).

## harnesses/ — the agent CLI under test

A harness is the agent CLI being evaluated (`codebuddy-code`, `claude-code`).
Each family:

```
harnesses/<family>/
  _defaults.yaml           # shared identity: name / import_path / protocol / params / mount skeleton
  versions/<version>.yaml  # per version: CLI version + settings_file + env (loader derives the mount image tag from this)
  presets/…                # deterministic config presets (static fields of settings.json / models.json)
  docker/Dockerfile        # split-mount image build template
  CONFIG.md                # mechanics of this family
```

The CLI is **not baked into the task image**: each version builds a read-only
split-mount image that is mounted into the container at runtime and symlinked
onto PATH by the agent's `install()`. A single task image can therefore test any
harness / version. For the mechanics and how to add a harness, see
[`harnesses/HARNESS_AUTHORING.md`](harnesses/HARNESS_AUTHORING.md); for each
family's specific behavior, see its `CONFIG.md`.

## models/ — model identity + backend

```
models/<provider>/<slug>.yaml   # one file per model
models/_template.model.yaml     # empty template (the runner skips _template.*)
```

A model YAML describes: `model.name` (the id sent to the backend), `protocols`
(`openai` / `anthropic`, the first is primary), `backend_url_env` /
`backend_key_env` (which env to read), and `params` (sampling parameters).
Parameters the harness cannot forward natively (`top_k` / `min_p` /
`chat_template_kwargs`, etc.) go into `params.extra_body` — **a model with
`extra_body` must use `model_connection: local_proxy`**; `direct` will fail fast.

The Web v1.0 rollout and its in-container verifier judge both use the same model
slug config. In a real `local_proxy` run, `prepare_job` points the verifier's
unified `WORKBUDDY_VERIFIER_LLM_*` environment variables at the job-private
proxy: `BASE_URL=<proxy>/v1`, `API_KEY=<judge-slug>`, `MODEL=<judge-slug>`. When
`llm_judge.enabled: false`, this trio is not injected; the real backend URL/KEY is
read only by the host proxy, via the model YAML's `backend_url_env` /
`backend_key_env`.

## jobs/ — composition + entry point

A job is a pure composition of model + harness + dataset, plus optional
overrides. For the full field set see [`jobs/_reference.yaml`](jobs/_reference.yaml);
for an empty template see `jobs/_template.job.yaml`. The minimum required fields:

```yaml
model: <provider>/<slug>            # slug under configs/models/
harness: codebuddy-code/<version>   # <family>/<version>
dataset: datasets/<dataset-id>/tasks
harness_backend: local              # where the harness/sandbox runs
model_connection: local_proxy       # direct | local_proxy
```

The `dataset:` path must exist on disk. Datasets are not shipped in git — download
the subset first with `./scripts/dataset/fetch-dataset.sh <subset>` (see
[`datasets/README.md`](../datasets/README.md)).

`configs/jobs/` is gitignored by default; only a few examples ship with the repo
(see the `.gitignore` allowlist).

---

## Scoring (CompositeVerifier)

Scoring is driven by `CompositeVerifier` in `src/workbuddy_bench/judge/` (a Harbor
`BaseVerifier` subclass). The run config only needs to point `import_path` at it;
the dataset-specific logic is registered by the `[verifier]` contract in
`dataset.toml` and the dataset-local `shared/verifier/plugin.py`.
`verifier.kwargs.profile` is no longer supported.

The plugin builds an `EvaluationContext` / `EvaluationPlan`, then hands them to
the `CompositeVerifierEngine`, which runs the evidence collectors, the rule / LLM
/ agent judge runners, and the scoring policy. Datasets that need a fully custom
flow (such as Web v1.0) can take over the entire verification process via
`custom_verify`.

- `reward.json` — numeric-only; read by Harbor's pass/fail gate and the host metrics.
- `score.json` — rich diagnostics, including judge / stage / evidence / plan debug info.

### Current dataset entry points

| Dataset | Scoring entry point |
|---|---|
| `wb-bench-web-v1.0` | Dataset-local fixed-judge Web verifier; runs rule / LLM / VLM / agent judges and emits a penalty score. |
| `wb-bench-code-v1.0` | Reads the task-local `tests/verifier.toml`; runs the task's pytest suite or a native script verifier. |
| `wb-bench-office-v1.0` | Runs the Office rule verifier and, when needed, merges in host-side LLM judge results. |
| `wb-bench-sec-v1.0` | Task-native scoring: each task ships its own `tests/scoring.py` (or `test_outputs.py`) that writes numeric `reward.json` directly (PoC verification / YARA matching / ground-truth comparison); does not use the workbuddy LLM judge or diff_capture. |

```toml
[verifier]
import_path = "workbuddy_bench.judge:CompositeVerifier"
timeout_sec = 600.0
```

The old `verifier.kwargs.profile` / `sources` / `judges` and the `dataset.toml`
`[judging]` fallback are no longer supported.

## Harbor parameters

Harbor has no standalone doc site; parameter descriptions live in the `description=`
of each Pydantic model:

```bash
python -c "from harbor.models.task.config import TaskConfig; print(TaskConfig.model_json_schema())"
```

The source is under `.venv/.../harbor/models/{task,job,trial}/config.py`. The table
above lists the subset that external users actually touch; the bench composes its
four config layers and `prepare_job` renders them into the Harbor runtime YAML.
