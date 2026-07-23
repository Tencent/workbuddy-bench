# WorkBuddyBench-Office Report Workflow

Generate an analysis report from one `WorkBuddyBench-Office` single-run
evaluation artifact directory. Analyze only the evaluation results under the
input path. Use `WorkBuddyBench-Office` as the display name, with no version
suffix.

## Dataset Context

The canonical runtime dataset id is `wb-bench-office-v1.0`. If the user provides
a WorkBuddy Bench repository or dataset directory, read task metadata from a
path shaped like
`/path/to/workbuddy-bench/datasets/wb-bench-office-v1.0`. If no dataset source
path is provided, analyze only the `RUN_DIR` artifacts and do not assume a
dataset source path.

The current task set has 50 tasks:

- `data-file-ops`: 24 tasks covering spreadsheets, JSON, multi-file data
  processing, reconciliation, extraction, aggregation, and rule validation.
- `doc-ops`: 17 tasks covering documents, reports, slides, timelines, briefing
  packs, and explanatory deliverables.
- `automation-workdir`: 9 tasks covering workspace automation, state recovery,
  notification/synchronization flows, and tool/mock-app coordination.

The publish difficulty field is `metadata.difficulty`, with 13 `easy`, 24
`medium`, and 13 `hard` tasks. Do not derive publish difficulty from historical
L-level labels embedded in task names or other provenance fields.

Office tasks declare a dataset CompositeVerifier contract in `dataset.toml` and
provide the dataset-specific implementation in `shared/verifier/plugin.py`.
Deterministic Rule grading evaluates submitted artifacts directly. When a
verifier-side LLM route is configured, task-specific rubrics evaluate textual
and structured evidence extracted from those artifacts, and the
CompositeVerifier applies each task's configured Rule/Judge score merge. A run
without that route reports the Rule component only.

## Input

Required:

- `RUN_DIR`: a single Harbor run artifact directory. Same standard layout as the
  other benchmarks: `<RUN_DIR>/result.json`, `config.json`, `job.log`, and
  `<task-id>__<attempt-id>/` trial subdirectories (see SKILL.md "Input Shape").
- `REPORT_DIR`: directory for the report deliverables (`metrics.json` and
  `report.md`). Resolve from the user request, or default to `<RUN_DIR>/report/`.

If the user does not provide `RUN_DIR`, ask them to provide a valid single-run
evaluation artifact directory. Do not guess a default path.

Optional:

- A compressed result artifact. Extract it first, then locate the concrete
  single-run `RUN_DIR`.
- A WorkBuddy Bench repository root or dataset directory, used only to enrich the
  report with `task.toml` metadata.

## Core Workflow

Run the shared steps in SKILL.md "Shared Workflow" (validate `RUN_DIR`, generate
`metrics.json`, write `report.md` to `REPORT_DIR`). The office-specific steps
are:

1. Read task metadata when a dataset root is available:
   - `<DATASET_ROOT>/tasks/<task-id>/task.toml`
   - Prefer `metadata.category`, `metadata.difficulty`, `metadata.tags`,
     `task.name`, and `task.keywords`.
2. Before interpreting scores, determine whether the run is Rule-only or
   Rule+Judge from the resolved run configuration and verifier artifacts. Do not
   compare the two modes as if they used the same scoring contract.
3. For low-score, build-error, and high-variance tasks, read evidence from:
   - `verifier/score.json` and `verifier/reward.json`
   - `verifier/results.xml` and `verifier/test_output.txt`
   - `verifier/artifact_manifest.json` and referenced textual or structured
     evidence
   - `verifier/llm_judge.json` when the rubric Judge ran
   - `verifier/agent.patch` or `code_diff/agent.patch`
   - `agent/trajectory.json`
   - `agent/cbc-output.txt` or `agent/cc-output.txt`
   - `agent/requests.jsonl` when full request/response recording was enabled

Treat missing optional files according to the run configuration: for example,
`verifier/llm_judge.json` is not expected in a Rule-only run, and
`agent/requests.jsonl` is not expected unless full I/O recording was enabled.

## Report Structure

Use these sections in order, translating headings into the report language when
appropriate:

1. Report title with `WorkBuddyBench-Office` and localized evaluation-report
   wording.
2. Data and methodology check:
   - run path, model, harness, canonical dataset id or user-provided dataset
     path, task count, and attempts per task
   - `reward` and `pass_rate` semantics
   - Rule-only versus Rule+Judge scoring mode
   - compare the run's scored task count with the expected 50 tasks; list
     missing tasks, missing `score.json`, build errors, and abnormal trial counts
3. Executive summary:
   - `reward`, `pass_rate`, full-score task count, and low-score task count
   - if there are multiple attempts, per-attempt reward/pass_rate and the tasks
     with the largest variance
4. Task-type breakdown:
   - aggregate means and low-score concentration by `data-file-ops`, `doc-ops`,
     and `automation-workdir`
   - aggregate by `metadata.difficulty`: `easy`, `medium`, and `hard`
5. Failure mode attribution:
   - themes such as data reading/format recognition, cross-file joins, rule
     priority, output schema, document structure, and toolchain/state recovery
   - each theme must cite task id, score, failed Rule checks, failed rubric items,
     verifier-stage failures, or artifact/evidence issues
6. Representative cases:
   - select 3 to 6 tasks covering strengths, weaknesses, and representative
     mid-score behavior
   - include task id, category, difficulty, reward/pass_rate, failure evidence,
     and artifact issues
7. Improvement recommendations:
   - bind each recommendation to task types and repeated failure signatures

## Writing Rules

- Match the report language to the user's explicit language request. If no
  language is specified, use the language of the user's request.
- Keep `WorkBuddyBench-Office`, `wb-bench-office-v1.0`, metric names, file paths,
  and JSON keys in canonical form.
- Use only metrics and artifact evidence under the input `RUN_DIR`.
- Use runtime dataset id `wb-bench-office-v1.0`; do not write dataset source
  paths that the user did not provide.
- Treat `reward` as the primary score. Treat `pass_rate` as the fraction of
  trials whose final verifier score is at least 1.0; do not describe it as a
  generic test pass percentage or conflate it with `reward`.
- Do not compare a Rule-only run directly with a Rule+Judge run without clearly
  identifying the scoring-contract difference.
- List build errors, missing expected tasks, or missing required verifier
  artifacts as evaluation completeness issues; do not directly attribute them
  to model capability weaknesses.
- Bind every judgment to task id, category, difficulty, score, Rule/rubric
  result, artifact evidence, or trajectory evidence.
