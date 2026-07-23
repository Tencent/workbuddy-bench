# CodeBuddy Code (cbc) harness

The configuration in this directory controls how `cbc` behaves inside each trial container. The agent implementation lives in
`src/workbuddy_bench/agents/cbc_agent.py` (`CbcAgent`).

```
codebuddy-code/
  _defaults.yaml              shared identity (name/import_path/protocol) + params + models_file pointer + mount skeleton
  versions/<version>.yaml     per version: CBC_VERSION + settings_file + env (loader derives the mount tag from these)
  presets/models.json         static models.json fields (shared across all versions)
  presets/settings/<group>.json  settings presets grouped by version (deny lists vary by version)
  docker/Dockerfile           split-mount image build template
```

**Scoped per version, never unioned**: deny lists and env shift from one cbc version to the next, so each version maps explicitly to its own preset
group. That mapping lives right in the `settings_file` / `env` fields of each `versions/<v>.yaml` — there's no separate index table.
`_defaults.yaml` offers no fallback for `settings_file`, so a missing mapping fails fast.

## Image delivery (split-mount)

cbc is not baked into the task image. Each version has a corresponding read-only image, mounted at `/opt/codebuddy-code` at runtime,
and `cbc_agent.install()` symlinks `cbc` / `codebuddy` onto PATH. The loader derives the tag from
`versions/<version>.yaml` as `workbuddy-bench/harness/codebuddy-code:<version>`.

```bash
scripts/harness/build-harness-mounts.sh --harness codebuddy-code/2.109.3            # build
scripts/harness/build-harness-mounts.sh --harness codebuddy-code/2.109.3 --dry-run  # just print the docker build
```

By default, `scripts/run.sh` just checks that the image exists and errors out if it's missing. You can let a run build it on the fly instead:

```bash
AUTO_BUILD_HARNESS_MOUNT=1 uv run ./scripts/run.sh --job <slug>
```

## The two config files: settings.json vs models.json

| File | What it controls | What the preset holds (static) | What cbc_agent fills in at runtime (dynamic) |
|---|---|---|---|
| `settings.json` | behavior, no endpoints | official documented evaluation-related fields | model's `thinking_enabled` → `alwaysThinkingEnabled: true` |
| `models.json` | model / endpoint | vendor / supportsToolCall / supportsImages / supportsReasoning / (optional) maxOutputTokens | `id`·`name` (addressing), `url`, `apiKey`, `maxInputTokens` (path A only, see below) |

- `id` / `name`: for local_proxy, the route slug; for direct, the real backend id.
- `url`: the proxy address for local_proxy, or `CBC_BASE_URL` for direct (must end in `/v1/chat/completions`).
- `apiKey`: the dummy key for local_proxy, or `CBC_API_KEY` for direct.

> The endpoint url + key are **never written into the preset**; they are assembled from the connection at runtime, and secrets never enter the manifest.

## Context window + compaction (branches by version)

cbc's compaction mechanism changes across versions, so the window translation **branches by version** (verified against the dist source). The core logic:

```
resolveCompactTriggerAt(maxInputTokens, pct):
    return maxInputTokens ? maxInputTokens * pct     # path A
                          : getAutoCompactWindow()   # path B = clamp(AUTO_COMPACT_WINDOW ?? 200k, 100k, 1M)
```

The two paths are **mutually exclusive**: once `maxInputTokens` is set, `AUTO_COMPACT_WINDOW` is never read.

|  | path A (cbc < 2.103.4, e.g. 2.93.5) | path B (cbc ≥ 2.103.4, current default) |
|---|---|---|
| window → | `models.json.maxInputTokens` | `CODEBUDDY_AUTO_COMPACT_WINDOW` env |
| pct → | `CODEBUDDY_AUTOCOMPACT_PCT_OVERRIDE` env | inactive (no pct multiplication, env not passed) |
| maxInputTokens | **written** | **not written** (writing it disables the window env) |
| trigger point | `window × pct` (can express < 100k) | absolute `window`, clamped to **[100k, 1M]** |
| when maxInputTokens is missing | → NaN → never compacts | — |

The branch boundary is `cbc_agent.py: _AUTOCOMPACT_WINDOW_ENV_MIN_VERSION = (2,103,4)`;
versions that can't be parsed fall back to path B. Choose B for an absolute token window; use A's pct for a trigger point below 100k.

### Input sources (priority job > bench; the model cap is validated)

| Source | Field | Role |
|---|---|---|
| `configs/bench/` | `context_window` / `context_compact_pct` | global defaults |
| `configs/jobs/<slug>.yaml` | top-level fields of the same name | overrides bench |
| `configs/models/<m>.yaml` | `model.context_window` (optional) | physical cap; a resolved value exceeding it fails fast |

After resolution, `resolve_manifest._resolve_context_window` stores the result in the manifest's
`context_window: {window, compact_pct, model_physical_max}`, which cbc_agent then translates per the table above.

## Version → deny list

Differences in `permissions.deny` across `settings/<group>.json` (tool existence verified against each version's
`dist/codebuddy.js`):

| Tool | legacy (2.93.5) | v2_103 (2.103.4) | v2_109 (2.109.x) | Introduced in |
|---|:---:|:---:|:---:|---|
| WebSearch / AskUserQuestion | deny | deny | deny | all versions |
| ComputerUse | absent | deny | deny | 2.94 |
| WaitForMcpServers | absent | deny | deny | ~2.103 |
| Workflow | absent | absent | deny | 2.105 |
| EnterPlanMode / ExitPlanMode | absent | absent | deny | 2.109 |

- Denying a full tool name means cbc removes the tool from the tool table (log: `filtered by permissions.deny`), not just prompting for approval.
- Convention: **only deny tools that exist in that version** (denying a nonexistent tool is a no-op but misleads the reader).
- In a headless benchmark, PlanMode can enter an interactive approval flow, causing the rollout to write a plan
  and then stop producing the target artifact; hence 2.109+ explicitly denies `EnterPlanMode` /
  `ExitPlanMode`.
- 2.103.4+ additionally disables the ComputerUse / WaitForMcpServers gates in `harness.env` and skips title generation
  (`CODEBUDDY_CODE_DISABLE_SESSION_TITLE_REFRESH=1`).
- **Fail-fast**: `cbc_agent.__init__` rejects an empty / missing `settings_preset` — if a version yaml omits
  `settings_file`, it errors out immediately, preventing WebSearch and the like from being silently allowed by an "empty deny list."

## Auditing

The manifest's `harness_runtime_config` block records the final configuration that enters the container (settings_json /
models_json / translated_env / max_turns / disallowed_tools / model_params), with sensitive values redacted
(url=`<proxy_url>`, key=`<dummy:proxy>`) — so the run can be reproduced without reading the agent code.
