# Phase 2 — Model

Goal: write `configs/models/<provider>/<slug>.yaml`.

**Always read `configs/models/_template.model.yaml` first** — it carries the
authoritative, inline-documented field set (v3 schema). The fields below are a
guide, not a substitute for the live template.

## The one field people get wrong: `protocols`

Declare the backend's wire protocol under the **plural** key `protocols`
(a scalar or a list; first element = primary):

```yaml
model:
  protocols: [openai]          # or [anthropic], or [openai, anthropic] for a gateway
```

- `openai` — backend speaks OpenAI Chat Completions (`/v1/chat/completions`)
- `anthropic` — backend speaks Anthropic Messages (`/v1/messages`)
- A multi-protocol gateway lists every protocol it accepts; a harness whose
  protocol is in the list routes via same-protocol passthrough instead of
  conversion.

**Why the plural matters (verified in source):**
`resolve_manifest.normalize_model_protocols` reads **only** `protocols`; a
missing key silently defaults to `["openai"]`. Several shipped files use the
singular `protocol:` — that works *only* because they're all `openai` and the
default happens to match. `scripts/run.sh`'s preflight additionally reads
singular `protocol` as the primary, so a non-openai backend declared with just
`protocol: anthropic` gets treated as anthropic by the shell but as openai by
the manifest resolver — a split-brain that mis-routes. **Always write
`protocols` (plural).** Never rely on the singular form.

## Required fields

```yaml
model:
  name: <backend-model-id>          # the id the backend expects in requests
  protocols: [openai]               # see above
  backend_url_env: <PROVIDER>_BASE_URL   # NAME of the .env var holding the base URL
  backend_key_env: <PROVIDER>_API_KEY    # NAME of the .env var holding the API key
```

`backend_url_env` / `backend_key_env` hold **variable names**, not values. The
actual URL + key go into `.env` in Phase 3. Suggest `<PROVIDER>_BASE_URL` /
`<PROVIDER>_API_KEY`, and use a unique name per backend so credentials don't
bleed across models.

## Optional fields (from the template)

- `backend_headers:` — literal extra HTTP headers on every upstream request.
  **Only applied under `local_proxy`**; `direct` ignores them. Reserved headers
  (host/content-length/authorization/x-api-key) are dropped.
- `context_window:` — the model's *physical* token cap. If set, bench/job
  `context_window` must not exceed it (fail-fast).
- `params:` — default inference scalars, per-job overridable via
  `model_params_override`.

## Decision tree: does this model force `local_proxy`?

Ask the user whether the backend needs any non-standard sampling knobs
(`top_k`, `min_p`, `repetition_penalty`, `chat_template_kwargs`, thinking
toggles the harness can't forward natively, ...). If yes → those go under
`params.extra_body`, and **`extra_body` is injected only by the local proxy**:

```yaml
  params:
    temperature: 0.6
    extra_body:                     # => job MUST use model_connection: local_proxy
      top_k: 20
      chat_template_kwargs:
        enable_thinking: true
```

**Record whether `extra_body` was used** — it forces `local_proxy` in Phase 4
(`direct` fails fast). Set that automatically; don't make the user rediscover it.

Also force `local_proxy` when the harness protocol differs from the model's
`protocols` (protocol bridge). Same-protocol + no `extra_body` → `direct` is
viable, though `local_proxy` is still the recommended default (it adds request
logging / interception).

## Two shapes to recognize (browse existing files under `configs/models/`)

- A model with **no** `extra_body` and a single protocol → the job can use
  `direct` (or `local_proxy`).
- A model that carries `extra_body` (e.g. `top_k` / `min_p` /
  `chat_template_kwargs`) → the job **must** use `local_proxy`.

Read whatever model files already exist in the checkout for concrete patterns;
don't hardcode any specific slug or provider from this doc.

## Guardrails

- No real keys or URLs in the YAML — only env-var *names*. Secrets live in
  `.env` (Phase 3).
- Slug = short kebab-case; the job references it via `model: <provider>/<slug>`.
- Write only under `configs/models/`.
