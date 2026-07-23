# claude-code harness

Claude Code (the `claude` CLI) acts as the harness under test. The agent
implementation lives in `src/workbuddy_bench/agents/cc_agent.py` (`CcAgent`),
structured identically to `cbc_agent.py` (likewise a `BaseInstalledAgent`
subclass written from scratch, not inheriting from Harbor's built-in classes).

```
claude-code/
  _defaults.yaml            shared identity + params + mount skeleton
  versions/<version>.yaml   CLAUDE_CODE_VERSION + settings_file + env
  presets/settings/v2_1.json  settings preset
  docker/Dockerfile         split-mount image build template
```

## Protocol and connectivity

- claude speaks the **Anthropic protocol** (`harness_adapters.py: claude-code`, `uses_anthropic_env=True`).
- Connecting to an OpenAI backend (utu / qwen) goes through **a2o** (the proxy converts Anthropic→OpenAI); connecting to a real
  Anthropic / vLLM-anthropic backend uses passthrough.
- Under local_proxy: `ANTHROPIC_BASE_URL` points at the host proxy, `ANTHROPIC_MODEL` = route
  slug (which the proxy routes on), and the real backend key is injected by the proxy.

## Read before testing cc: reasoning_effort

- On the cc CLI side, the default effort = **max** (`CLAUDE_CODE_EFFORT_LEVEL: "max"` in `versions/*.yaml`).
- The effort actually sent to the backend is **injected by the proxy**: the proxy maps the Anthropic `thinking`
  field to `reasoning_effort`, then overrides it with
  `params.extra_body.reasoning_effort` from `configs/models/<slug>.yaml`.
- **Always set `extra_body.reasoning_effort` explicitly in the model YAML** (to a value the backend accepts, such as
  `low|medium|high`). Leave it out and it falls back to the cc-side `max` — and only Opus 4.6+ / Sonnet 4.6 /
  Fable 5 support `max`, so OpenAI-family backends (utu/qwen) will most likely reject it with a 400. Even if you want the backend's
  own default, name a tier the backend supports rather than leaving the field empty and hoping `max` works.

## Context window / auto-compact

The configuration **inputs** come from the same place as cbc, but the **translation mechanism is completely different** — don't carry your cbc mental model over.

**Input sources** (same as cbc):

| Source | Field |
|---|---|
| `configs/bench/_default.yaml` | default `context_window: 200000` |
| `configs/jobs/<slug>.yaml` | top-level `context_window` / `context_compact_pct` override |
| `configs/models/<slug>.yaml` | `model.context_window` = physical max; a job value exceeding it fails fast |

**How cc translates it**: cc_agent injects the two values **directly** into the claude env, with no version branching, no clamp,
and no touching of maxInputTokens:

- `CLAUDE_CODE_AUTO_COMPACT_WINDOW = context_window` (absolute tokens)
- `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE = context_compact_pct` (percentage, can only be lowered)

**Difference from cbc** (why you can't copy it over): cbc is a version-dependent either/or plus clamp (route A writes
`maxInputTokens`, trigger point = window×pct; route B uses `CODEBUDDY_AUTO_COMPACT_WINDOW`,
clamps to [100k,1M], and pct has no effect). cc uses `CLAUDE_CODE_*` variables, a single path, and no clamp. **The two
just happen to both read the `context_window` bench item; the implementation is each CLI's own mechanism.**

**Testing 256k / 512k / 1M comparisons**: change the job's (or bench's) `context_window`, and make sure the model YAML's
`model.context_window` physical max is ≥ that value. If you don't set `context_window`, cc falls back to the claude
default (= the model's context window, capped by the model's real window). Observed in practice: with an unknown route-slug model plus an
explicit 1M window, cc runs as usual without error.

## --max-turns

`CLAUDE_CODE_MAX_TURNS` (default 256) → `--max-turns`. A **turn = one agentic
round-trip** (one model response, typically including one tool call + result feedback), **not** one back-and-forth
conversational exchange. When the limit is reached, cc errors out (in print mode).

## Versions

`versions/<v>.yaml` only sets `CLAUDE_CODE_VERSION` + `settings_file` + `env`; the loader derives
the mount image `workbuddy-bench/harness/claude-code:<v>` and the build arg. Existing ones: 2.1.181,
2.1.187. Adding a version = write one `versions/<v>.yaml` + run
`scripts/harness/build-harness-mounts.sh --harness claude-code/<v>`.

## settings preset (presets/settings/v2_1.json)

- `permissions.allow` allowlist + `deny: [WebSearch, AskUserQuestion]` +
  `skipDangerousModePermissionPrompt` + a PreToolUse hook for ExitPlanMode (auto-approved under headless
  to avoid getting stuck).
- **Disabling AskUserQuestion is a hard requirement**: calling it in a non-interactive context would hang.

## split-mount notes

claude 2.x's `bin/claude.exe` is a **native ELF binary** (not JS); the launcher execs it directly,
without wrapping it in node.
