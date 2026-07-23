---
name: wbbench-run-setup
description: Use when a user wants to configure and launch a WorkBuddy Bench evaluation run from scratch, or is unsure how to set one up. Walks them interactively through environment setup, model config, .env credentials, job config, and launching the run, then hands off to wbbench-report-skills for metrics and trajectory analysis.
---

# WB-Bench Run Setup

Interactively guide the user from an unconfigured checkout to a running (and then
analyzed) WorkBuddy Bench evaluation. This is a seven-phase playbook (0–6). This file is
the router: read the matching `references/` file **completely** before executing
each phase, complete one phase at a time, and confirm with the user before
moving on.

## Phases → reference files

| Phase | Goal | Read |
|-------|------|------|
| 0. Datasets | subset(s) downloaded under `datasets/` | [`references/00-datasets.md`](references/00-datasets.md) |
| 1. Environment | a checkout that can run | [`references/01-environment.md`](references/01-environment.md) |
| 2. Model | `configs/models/<provider>/<slug>.yaml` | [`references/02-model.md`](references/02-model.md) |
| 3. Credentials | `.env` URL + key for the chosen names | [`references/03-credentials.md`](references/03-credentials.md) |
| 4. Job | `configs/jobs/<slug>.yaml` | [`references/04-job.md`](references/04-job.md) |
| 5. Launch | dry-run, confirm, run | [`references/05-run.md`](references/05-run.md) |
| 6. Analysis | hand off to `wbbench-report-skills` | [`references/06-analysis.md`](references/06-analysis.md) |

## Interaction rules

- **Reply in the user's language.** Detect the language of the user's messages
  and use it throughout (they write Chinese → you answer in Chinese). Config
  keys, file contents, and shell commands stay verbatim; only your prose adapts.
- **Read the live templates — don't trust field names memorized here or in the
  references.** Schemas evolve; the authoritative sources are
  `configs/models/_template.model.yaml`, `configs/jobs/_template.job.yaml` +
  `configs/jobs/_reference.yaml`, and `.env.example`.
- **Discover choices at runtime**, never hardcode a list that may be stale:
  `ls configs/harnesses/*/versions/`, `ls -d datasets/wb-bench-*/`. Datasets are
  **not** in git — a fresh checkout has none until Phase 0 downloads them, so an
  empty `datasets/` means "download first" (see `references/00-datasets.md`), not
  "no datasets exist".
- One phase at a time. Show the user what you're about to write, then write it.

## Guardrails (apply across all phases)

- Secrets (API keys, base URLs) live **only** in `.env`. YAML references env-var
  *names* only — never a real key or URL. Never `git add .env`; never echo
  secret values back.
- The model wire-protocol key is **`protocols`** (plural). The manifest resolver
  reads only that and defaults to `openai` when it's absent, so a singular
  `protocol:` silently mis-routes a non-openai backend. Always write `protocols`.
  (Details in [`references/02-model.md`](references/02-model.md).)
- `model_connection: local_proxy` is the **recommended default** — a job-private
  proxy that adds protocol bridging, `extra_body` injection, and full request
  logging at the transport layer without altering content, and `run.sh` starts it
  for you. Prefer it unless the user asks for `direct`. It is **mandatory** (set
  it automatically, don't make the user discover it) when the model uses
  `extra_body` (`top_k` / `min_p` / `chat_template_kwargs`, ...) or the harness
  protocol isn't in the model's `protocols`; `direct` fails fast in both cases.
  (Details in [`references/04-job.md`](references/04-job.md).)
- Before dry-run, verify every env-var name the model references exists in
  `.env`.
- **Ask the user to confirm before the real run** (Phase 5) — it is time- and
  cost-intensive.
- Treat dataset/task files as read-only; only write under `configs/` and `.env`.
  Datasets come from HuggingFace via `scripts/dataset/fetch-dataset.sh` (Phase 0);
  never hand-edit downloaded task files.
