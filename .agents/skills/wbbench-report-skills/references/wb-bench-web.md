# WB-Bench-Web Report Workflow

Generate an analysis report from one `WB-Bench-Web` single Harbor run artifact
directory. Analyze only the evaluation results under the input path. Use
`WB-Bench-Web` as the display name, with no version suffix.

## Dataset Context

The public dataset id is `wb-bench-web`. If the user provides a WorkBuddy Bench
repository or dataset directory, read task metadata from a path shaped like
`/path/to/workbuddy-bench/datasets/wb-bench-web`. If no dataset source path is
provided, analyze only the `RUN_DIR` artifacts and do not assume a dataset source
path.

The current task set has 70 tasks:

- Categories: `page-interaction` 21 tasks, `data-visualization` 15 tasks,
  `visual-design` 9 tasks, `analytical-report` 7 tasks, `code-testing` 7 tasks,
  `page-implementation` 6 tasks, and `document-conversion` 5 tasks.
- Task modes: From Scratch 35 tasks, Bug Fix 8 tasks, Extend Existing 8 tasks,
  Review & Analysis 7 tasks, Test Generation 7 tasks, and Format Conversion
  5 tasks.
- Interaction complexity: no interaction, light interaction, single-flow state,
  multi-step workflows, persistence/offline behavior, and cross-state behavior.

Web tasks use the `CompositeVerifier` web profile. Evaluation artifacts may
include rule, VLM chat, and agent-judge evidence. Prefer structured `score.json`
and eval-report evidence. Use screenshots only as supporting evidence.

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
  report with `dataset.toml`, `task_taxonomy.tsv`, and task-level `task.toml`.
- Screenshot assets. Reuse only screenshots already present in the run artifact;
  do not execute generated HTML by default.

## Core Workflow

Run the shared steps in SKILL.md "Shared Workflow" (validate `RUN_DIR`, generate
`metrics.json`, write `report.md` to `REPORT_DIR`). The web-specific steps are:

1. Read task metadata when a dataset root is available:
   - `<DATASET_ROOT>/dataset.toml`
   - `<DATASET_ROOT>/task_taxonomy.tsv`
   - `<DATASET_ROOT>/tasks/<task-id>/task.toml`
2. For low-score, build-error, and high-variance tasks, read evidence from:
   - `verifier/score.json`
   - `verifier/reward.json`
   - `verifier/evidence/eval-report.json`
   - `verifier/results.xml`
   - `verifier/test_output.txt`
   - `verifier/agent.patch` or `code_diff/agent.patch`
   - `agent/cbc-output.txt`
3. If the user asks for per-task visual explanation, reuse existing screenshots
   first, such as:
   - `verifier/evidence/visual/*.png`
   - `verifier/evidence/screenshots/*.png`
   - `logs/screenshots/*.png`
   If no screenshot exists, state that screenshots were not generated. Do not
   rerun evaluation.

## Report Structure

Use these sections in order, translating headings into the report language when
appropriate:

1. Report title with `WB-Bench-Web` and localized evaluation-report wording.
2. Data and methodology check:
   - run path, model, dataset id or user-provided dataset path, task count, and
     attempts per task
   - `reward` and `pass_rate` semantics
   - missing `score.json`, build errors, or abnormal trial counts
3. Executive summary:
   - `reward`, `pass_rate`, full-score task count, and low-score task count
   - top/bottom tasks; if there are multiple attempts, tasks with the largest
     variance
4. Category, mode, and interaction breakdown:
   - aggregate by category: page interaction, data visualization, visual design,
     analytical reports, test generation, page implementation, and document
     conversion
   - aggregate by task mode: From Scratch, Bug Fix, Extend Existing, Review &
     Analysis, Test Generation, and Format Conversion
   - aggregate by interaction state complexity: no interaction, light
     interaction, single-flow state, multi-step workflows, persistence/offline
     behavior, and cross-state behavior
5. Failure mode attribution:
   - themes such as runtime state linkage, forms/multi-step flows, persistence
     recovery, chart semantics, visual hierarchy, test assertions, and document
     conversion completeness
   - each theme must cite task id, score, check item, or verifier output
6. Representative cases:
   - select 3 to 6 tasks covering strengths, weaknesses, and representative
     mid-score behavior
   - include screenshots or a note that screenshots are unavailable when useful
7. Improvement recommendations:
   - bind each recommendation to category, task mode, interaction complexity,
     and repeated failure signatures

## Writing Rules

- Match the report language to the user's explicit language request. If no
  language is specified, use the language of the user's request.
- Keep `WB-Bench-Web`, `wb-bench-web`, metric names, file paths, and JSON keys
  in canonical form.
- Use only metrics and artifact evidence under the input `RUN_DIR`.
- Use dataset id `wb-bench-web`; do not write dataset source paths that the user
  did not provide.
- Treat `reward` as the primary score and `pass_rate` as the all-tests-passed
  rate. Do not conflate them.
- Screenshots are not required evidence. Missing screenshots do not imply task
  failure.
- If local HTML execution is needed to capture screenshots from artifacts, get
  explicit user permission first.
- Bind every judgment to task id, category, task mode, score, check item, or
  verifier evidence.
