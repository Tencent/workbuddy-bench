---
name: wbbench-report-skills
description: Use when generating analysis reports for WB-Bench-Office, WB-Bench-Web, or WB-Bench-Code from a single Harbor evaluation run artifact directory; route to the matching reference workflow.
---

# WB-Bench Report Skills

This repository-local skill routes WB-Bench report generation to the matching
benchmark-specific workflow under `references/`:

- `references/wb-bench-office.md`: `WB-Bench-Office` office, data-file,
  document, and workdir task reports.
- `references/wb-bench-web.md`: `WB-Bench-Web` web, UI automation, visual,
  reporting, and test-generation task reports.
- `references/wb-bench-code.md`: `WB-Bench-Code` code task reports.

## Routing

When the user asks for a report for one of these benchmarks:

1. Identify the benchmark from the user request, Harbor `config.json`, archive
   name, run directory name, job directory name, or dataset id.
2. Read the matching reference workflow completely before doing analysis:
   - `references/wb-bench-office.md`
   - `references/wb-bench-web.md`
   - `references/wb-bench-code.md`
3. Use the public display name in report titles:
   - `WB-Bench-Office`
   - `WB-Bench-Web`
   - `WB-Bench-Code`
4. Use public dataset ids without version suffixes:
   - `wb-bench-office`
   - `wb-bench-web`
   - `wb-bench-code`
5. Keep Harbor input artifacts read-only and write outputs only to `REPORT_DIR`
   (see Shared Workflow; defaults to `<RUN_DIR>/report/`).

Fallback: if the benchmark cannot be identified, ask the user which of the three
report workflows to use.

If the user does not provide any path, ask them to provide a valid complete
single-run artifact directory `RUN_DIR`. Do not guess a default path or search
broad workspace or cluster directories.

## Input Shape

The user input is expected to be a single evaluation run artifact directory
`RUN_DIR`: one model, one dataset, one evaluation round.

A standard Harbor result tree has three nested levels; `RUN_DIR` is the middle
(run) level:

```
results/<job-name>/                     JOB level: only <timestamp>/ subdirs
  <timestamp>/                          RUN level  <-- this is RUN_DIR
    result.json  config.json  job.log  lock.json
    <task-id>__<attempt-id>/            TRIAL level
      result.json  config.json  trial.log  agent/  verifier/  artifacts/
```

`result.json` and `config.json` exist at BOTH the run and trial levels, so their
presence alone does not identify `RUN_DIR`. Use these discriminators:

- RUN level (`RUN_DIR`): has `job.log` and one or more
  `<task-id>__<attempt-id>/` subdirectories.
- JOB level: contains only `<timestamp>/` subdirectories and no `job.log`. If
  exactly one timestamp exists, descend into it; otherwise ask for the exact
  `RUN_DIR`.
- TRIAL level: has `trial.log` and `verifier/`. This is a single trial, not a
  run; move up one level.

If the user provides a parent `results/` or job directory, resolve a run only
when exactly one candidate exists. If multiple jobs, models, or timestamps
exist, ask the user to provide the exact `RUN_DIR`.

## Shared Workflow

`REPORT_DIR` is the directory this skill writes its report deliverables
(`metrics.json`, `report.md`) to. It is not a program argument; resolve it from
the user request, or default to `<RUN_DIR>/report/` when the user does not
specify one. Writing a new `report/` subdirectory under `RUN_DIR` only adds
files and does not alter existing Harbor artifacts.

1. Validate `RUN_DIR`. Do not select a trial directory.
2. Generate the core metric data:

```bash
cd /path/to/workbuddy-bench
uv run python -m workbuddy_bench.scorer.metrics <RUN_DIR> --json > <REPORT_DIR>/metrics.json
```

3. Read the benchmark-specific steps in the matching reference workflow: task
   metadata sources (only when the user provides a dataset root; use the public
   dataset id, e.g. `/path/to/workbuddy-bench/datasets/<dataset-id>`) and the
   evidence files to read for low-score, build-error, and high-variance tasks.
4. Write `<REPORT_DIR>/report.md` in the user-specified language. If the user
   does not specify a language, match the language of the user's request. Keep
   benchmark names, dataset ids, metric field names, file paths, and JSON keys in
   their canonical form.

Run identity (run path, model/harness, dataset, task count) comes from the
run's own `config.json`, the Harbor `manifest.json` when available, and the
`metrics.json` produced above. Do not create a separate inventory file.

## Metrics Output

`REPORT_DIR` ends up holding exactly two deliverables:

```
<REPORT_DIR>/
  metrics.json   # scorer output (schema below)
  report.md      # the analysis report
```

`metrics.json` is emitted by `workbuddy_bench.scorer.metrics --json`. Its
top-level keys are:

- `run_dir`: resolved run path.
- `reward`: primary score; mean of per-task reward (build_error counts as 0).
- `pass_rate`: fraction of trials with verifier score >= 1.0.
- `n_tasks`, `n_trials`: task and trial counts actually scored.
- `missing_tasks`: expected tasks that produced no trials (only populated when
  run with `--manifest`; these are scored 0 so they count in the denominator).
- `attempts_per_task`: sorted list of distinct attempt counts across tasks.
- `score_sources`: counts of where each trial score came from.
- `per_attempt`: list of `{attempt, n_tasks, reward, pass_rate}`.
- `per_task`: map `task -> {n_attempts, reward, pass_rate, attempts[]}`, where
  each attempt is `{trial, score_source, tests_passed, tests_total, reward,
  full_pass, build_error}`.
- `definitions`: inline glossary for `reward` and `pass_rate`.

Treat `reward` as the primary score and `pass_rate` as the all-tests-passed
rate; do not conflate them.

## Boundaries

- These workflows are report-generation skills, not a new evaluator runtime.
- Do not alter Harbor artifacts or rerun expensive evaluation jobs unless the
  user explicitly requests it.
- Analyze only the user-provided single-run artifact directory `RUN_DIR`.
- Do not assume or write internal source paths. If repository context is needed,
  use `/path/to/workbuddy-bench` style placeholders or the user-provided path.
