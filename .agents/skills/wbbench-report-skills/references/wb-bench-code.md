# WB-Bench-Code Report Workflow

Generate an analysis report from one `WB-Bench-Code` single Harbor run artifact
directory. Analyze only the evaluation results under the input path. Use
`WB-Bench-Code` as the display name, with no version suffix.

## Dataset Context

The public dataset id is `wb-bench-code`. If the user provides a WorkBuddy Bench
repository or dataset directory, read task metadata from a path shaped like
`/path/to/workbuddy-bench/datasets/wb-bench-code`. If no dataset source path is
provided, analyze only the `RUN_DIR` artifacts and do not assume a dataset source
path.

The current task set has 80 tasks:

- High-frequency categories: `bug-fix` 10 tasks and `feature` 10 tasks.
- Structured coding capability categories: `api-contract`, `schema-behavior`,
  `testing`, `refactor`, `performance`, `reliability`,
  `security-hardening`, and `repo-understanding`.
- Data and product categories: `data-quality`, `data-reporting`,
  `feature-pipeline`, `model-evaluation`, `product-analytics`, and
  `product-policy`.
- Migration and tool categories: `python-port` and `tool-behavior`.

Task artifacts include product code, test code, data/model reports,
configuration tools, and repository-understanding analysis. Scoring uses the
`CompositeVerifier` code profile. Core evidence comes from
hidden/unit/integration/script verifier output, patches, and `score.json`.

## Input

Required:

- `RUN_DIR`: a single Harbor run directory. Standard layout:
  `<RUN_DIR>/result.json`, `config.json`, `job.log`, and
  `<task-id>__<attempt-id>/` trial subdirectories (see SKILL.md "Input Shape").
- `REPORT_DIR`: directory for the report deliverables (`metrics.json` and
  `report.md`). Resolve from the user request, or default to `<RUN_DIR>/report/`.

If the user does not provide `RUN_DIR`, ask them to provide a valid single Harbor
run artifact directory. Do not guess a default path.

Optional:

- A compressed result artifact. Extract it first, then locate the concrete
  single-run `RUN_DIR`.
- A WorkBuddy Bench repository root or dataset directory, used only to enrich the
  report with `dataset.toml` and task-level `task.toml`.

## Core Workflow

Run the shared steps in SKILL.md "Shared Workflow" (validate `RUN_DIR`, generate
`metrics.json`, write `report.md` to `REPORT_DIR`). The code-specific steps are:

1. Read task metadata when a dataset root is available:
   - `<DATASET_ROOT>/dataset.toml`
   - `<DATASET_ROOT>/tasks/<task-id>/task.toml`
   - Prefer `metadata.category`, `metadata.subcategory`,
     `metadata.difficulty`, `metadata.complexity`, `metadata.intent`,
     `metadata.artifact_type`, `metadata.edit_mode`, and
     `metadata.verification_mode`.
2. For low-score, build-error, and high-variance tasks, read evidence from:
   - `verifier/score.json`
   - `verifier/reward.json`
   - `verifier/results.xml`
   - `verifier/test_output.txt`
   - `verifier/agent.patch` or `code_diff/agent.patch`
   - `agent/cbc-output.txt`
   - `conversation.jsonl` or `proxy/summary.json`
3. When writing `report.md`, use patches and verifier evidence in addition to
   `metrics.json`.

## Report Structure

Use these sections in order, translating headings into the report language when
appropriate:

1. Report title with `WB-Bench-Code` and localized evaluation-report wording.
2. Data and methodology check:
   - run path, model, dataset id or user-provided dataset path, task count, and
     attempts per task
   - `reward` and `pass_rate` semantics
   - missing `score.json`, build errors, or abnormal trial counts
3. Executive summary:
   - `reward`, `pass_rate`, full-score task count, and low-score task count
   - top/bottom tasks; if there are multiple attempts, tasks with the largest
     variance
4. Coding capability breakdown:
   - aggregate by category: bug-fix, feature, api-contract, data-quality,
     data-reporting, feature-pipeline, model-evaluation, performance,
     repo-understanding, schema-behavior, security-hardening, testing, and
     related categories
   - aggregate by difficulty/complexity, artifact_type, edit_mode, and
     verification_mode
5. Correctness and failure modes:
   - themes such as failed tests, build errors, API contracts, schema behavior,
     security boundaries, data processing, performance caching,
     async/reliability, and repository understanding
   - each theme must cite task id, score, failed test, stack trace, or verifier
     output
6. Patch localization and trajectory:
   - patch file count, added/deleted lines, whether target files were modified,
     whether test-only escape occurred, and whether patches are missing
   - when trajectory data is available, summarize assistant turns, tool calls,
     invalid requests, or retry signals
7. Representative cases:
   - select 3 to 6 tasks covering strengths, weaknesses, and representative
     mid-score behavior
   - include task id, category, difficulty, reward/pass_rate, patch details, and
     failure evidence
8. Improvement recommendations:
   - bind each recommendation to category, verification_mode, and repeated
     failure signatures

## Writing Rules

- Match the report language to the user's explicit language request. If no
  language is specified, use the language of the user's request.
- Keep `WB-Bench-Code`, `wb-bench-code`, metric names, file paths, and JSON keys
  in canonical form.
- Use only metrics and artifact evidence under the input `RUN_DIR`.
- Use dataset id `wb-bench-code`; do not write dataset source paths that the
  user did not provide.
- Treat `reward` as the primary score and `pass_rate` as the all-tests-passed
  rate. Do not conflate them.
- Do not rely only on final averages. Attribute low-score tasks using failed
  tests, patches, and output logs.
- Do not describe test-only changes as valid fixes unless the task itself is
  Test Generation.
- Bind every judgment to task id, category, verification_mode, score, failed
  tests, or patch evidence.
