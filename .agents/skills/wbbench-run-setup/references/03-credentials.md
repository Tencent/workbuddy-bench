# Phase 3 — Credentials (.env)

Goal: `.env` defines the URL + key for the env-var *names* chosen in Phase 2.

The project fixes **no** env-var names. Whatever `backend_url_env` /
`backend_key_env` you wrote in the model YAML are exactly the two lines `.env`
must define. Read `.env.example` for the pattern (it's a documented template,
not a required-var list).

## Steps

1. For each of `backend_url_env` and `backend_key_env`, add a matching line to
   `.env`:

   ```
   MY_PROVIDER_BASE_URL=https://backend.example.com/v1
   MY_PROVIDER_API_KEY=sk-...
   ```

2. Offer the user both ways and let them choose:
   - they paste the value and you write it in, or
   - you write the var name with a placeholder and they fill it themselves.

## Base-URL gotcha

Some backends mount chat completions under a sub-path. The base URL must include
that sub-path, e.g. `https://backend.example.com/openapi` — not just the host.
When a run later 404s on the model call, a truncated base URL is the first
suspect.

## Guardrails (hard rules)

- Secrets live **only** in `.env`. Never write a real key or URL into any YAML.
- Never `git add .env` or otherwise commit it. Do not echo secret values back in
  responses.
- Before moving to dry-run, verify every env-var name the model references
  actually exists in `.env` — a missing name surfaces as an auth/connection
  failure much later and is annoying to trace.
