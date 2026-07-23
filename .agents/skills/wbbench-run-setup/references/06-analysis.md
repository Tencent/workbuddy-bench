# Phase 6 — Metrics + trajectory analysis

Do **not** reimplement reporting here. Once the run finishes, hand off to the
**`wbbench-report-skills`** skill (invoke it via the Skill tool). That skill
owns metric computation and trajectory analysis and routes to the matching
benchmark-specific workflow (WB-Bench-Office / WB-Bench-Web / WB-Bench-Code).

## What to pass

A single evaluation run artifact directory `RUN_DIR` — one model, one dataset,
one round. For a run launched here that's:

```
<bench.jobs_dir>/<slug>/<timestamp>/
```

(bench `jobs_dir` defaults to `results`, so typically
`results/<slug>/<timestamp>/`). Point at the timestamped run dir itself — the
one containing `config.json` / `result.json` / `job.log` / trial dirs — not a
parent `results/` and not a single trial dir.

If several timestamps exist under the slug, pass the exact one; the report skill
refuses to guess when a parent contains multiple runs.

## Boundary

`wbbench-report-skills` treats Harbor artifacts as read-only and writes only to
a user-specified output directory. Let it produce the metrics report and
trajectory analysis; this setup skill's job ends at a completed run.
