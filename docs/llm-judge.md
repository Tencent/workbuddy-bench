# LLM Judge: Design Notes

The host-side LLM judge (`src/workbuddy_bench/scorer/llm_judge.py`) runs as an
optional post-run step for datasets graded by a test suite. It only applies when
`llm_judge.enabled: true` and `llm_judge.mode: host_side`. In-container judges
(Web / Office) do not use it.

Its purpose is narrow: tests sometimes fail because the agent chose a different
contract (a function name, file path or output format) that the instruction never
specified. The judge corrects for that without overriding the objective test
signal.

## Approaches compared

We compared three designs on the same code tasks and solver models.

| Approach | What the LLM outputs | Score | Outcome |
|---|---|---|---|
| Absolute scoring (original) | Weighted scores over several quality dimensions | Weighted sum | Rejected |
| Recovery, unified | One recovery rate across all failed tests | `tpr + rate × (1 − tpr)` | Viable, not adopted |
| Recovery, per-test | A verdict per failed test | `tpr + recovered / total` | **Adopted** |

### Absolute scoring (original)

This was the judge's original design. The model scored the solution directly,
with no anchor to the test results. Scores tracked how a solution *looked* more
than what it did: solvers with long, well-explained trajectories were scored
consistently higher than solvers with the same test outcomes, and the model
ranking was distorted. Changing the judge model, merging or reweighting
dimensions, and adding hard caps did not fix this. An absolute LLM score is
contaminated by process style.

### Recovery, unified

The test pass rate (tpr) became the base score, and the LLM only estimated what
share of the failed tests were contract mismatches rather than real defects. This
restored a ranking consistent with the tests at one call per trial. Two weaknesses
remained:

- When failure details were missing, the model inferred a high recovery rate from
  overall context and lifted low-tpr trials far above their test results.
- A single aggregate rate cannot be checked against individual tests.

### Recovery, per-test (current)

Each failed test is judged on its own, and only tests with a clear
contract-mismatch explanation are recovered.

- **Strictest**: recovery rates were lower than the unified estimate, and a
  verdict defaults to "real defect" when in doubt.
- **No hallucinated recovery**: without failure details there is nothing to
  judge, so the score stays at tpr.
- **Auditable**: every verdict carries a reason.
- **Consistent ranking**: model order matches the test results, and larger
  recoveries went to solvers whose failures were more often interface-related.

The cost is more LLM calls (one per failed test), so judge concurrency needs to
be kept moderate. High concurrency against a rate-limited endpoint produced many
failed verdicts in our runs.

## Current scoring rules

```
score = min(tpr + recovered_tests / total_tests, 1.0)
```

- `tpr = 1.0`: score 1.0, no LLM call.
- Empty agent patch: score 0.0.
- No failure details and no test counts: score = tpr.
- JUnit details missing but test counts present: failed entries are synthesized
  from the raw test output and judged.
- `tpr = 0`: recovery is capped at 0.85.
- A failed test counts as recovered only if the model explicitly returns
  `"is_test_too_strict": true`. Verdicts that fail to parse are not recovered.

The judge sees the instruction, the gold and agent patches, the single failed test
and aggregate test counts. It does not see the agent trajectory or the full test
sources.

## Usage

Enable it per job:

```yaml
llm_judge_override:
  enabled: true
  model: <judge-model-slug>   # configs/models/<slug>.yaml
```

Concurrency is controlled by `LLM_JUDGE_MAX_CONCURRENT` (default 4). Results are
written to `llm_judge_summary.json`, with per-test verdicts under
`tasks[].per_test.verdicts`.
