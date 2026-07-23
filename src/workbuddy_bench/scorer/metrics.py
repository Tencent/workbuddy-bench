"""Job-level evaluation metrics for WorkBuddy Bench results.

This module is the single source of truth for aggregating verifier-reported
scores. The per-trial score is read from ``verifier/score.json`` using the
canonical numeric fields in this order: ``reward``, ``overall``,
``test_pass_rate``. ``tests_passed`` / ``tests_total`` are diagnostics and are
used only as a final compatibility fallback for old artifacts that lack a
numeric score field.

Definitions
-----------
- **reward** — mean of each trial's verifier-reported score.
- **pass_rate** — fraction of trials with a full verifier score (>= 1.0).

build_error / missing score.json
--------------------------------
A trial whose score.json is missing, unreadable, lacks any usable score, or has
``test_status == "build_error"`` contributes reward 0 and counts as not a full
pass. Harbor may still record a result.json for a verifier that crashed before
writing score.json; such trials are surfaced via ``n_trials`` but scored 0.

Aggregation
-----------
Per task, attempts are averaged first; then task-level values are averaged
across tasks. When every task has the same number of attempts, this is identical
to a flat mean over all trials.

Usage
-----
    python -m workbuddy_bench.scorer.metrics results/<job>/<run>
    python -m workbuddy_bench.scorer.metrics results/<job>/<run> --json
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Sequence

DEFINITIONS = {
    "reward": "mean of verifier-reported per-trial score; build_error -> 0",
    "pass_rate": "fraction of trials with verifier score >= 1.0",
    "score_source": "score.json reward > overall > test_pass_rate > tests_passed/tests_total fallback",
    "aggregation": "mean over attempts within each task, then mean over tasks",
    "denominator_rebasing": (
        "pure test-count attempts are re-scored as tests_passed/max(tests_total) "
        "across a task's attempts so a shrunken suite cannot inflate the ratio; "
        "composite LLM+rule scores are exempt"
    ),
}


def _int_or_none(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _unit_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0.0 or number > 1.0:
        return None
    return number


def _score_from_payload(data: dict) -> tuple[float | None, str]:
    for key in ("reward", "overall", "test_pass_rate"):
        score = _unit_float(data.get(key))
        if score is not None:
            return score, key

    passed = _int_or_none(data.get("tests_passed"))
    total = _int_or_none(data.get("tests_total"))
    if passed is not None and total and total > 0:
        return max(0.0, min(passed / total, 1.0)), "tests_passed_over_tests_total"
    return None, ""


def _is_pure_count_ratio(data: dict, score: float | None) -> bool:
    """True when ``score`` is a plain ``tests_passed/tests_total`` pass ratio.

    Only such scores may be re-based onto the max denominator across attempts
    (see ``_rebase_shrunken_denominators``). A composite score that blends an
    LLM rubric with the rule count must be left untouched: for it, ``tests_total``
    is only the rule component's count, so re-basing would corrupt the blend.
    Blends are detected by an ``llm_judge_component_score`` marker or by
    ``overall`` differing from ``test_pass_rate``. A pure count task keeps
    ``reward == overall == test_pass_rate == tests_passed/tests_total`` (modulo
    rounding), so we additionally require ``score`` to match the raw ratio.
    """
    if score is None:
        return False
    if "llm_judge_component_score" in data:
        return False
    overall = _unit_float(data.get("overall"))
    test_pass_rate = _unit_float(data.get("test_pass_rate"))
    if overall is not None and test_pass_rate is not None and abs(overall - test_pass_rate) > 1e-9:
        return False
    passed = _int_or_none(data.get("tests_passed"))
    total = _int_or_none(data.get("tests_total"))
    if passed is None or not total or total <= 0:
        return False
    ratio = max(0.0, min(passed / total, 1.0))
    # Tolerance covers the 4-decimal rounding applied to the stored score.
    return abs(score - ratio) < 5e-4


def _load_trials(run_dir: Path) -> dict[str, list[dict]]:
    """Return ``{task_name: [trial_record, ...]}`` discovered under ``run_dir``.

    Trial records carry the canonical score from ``<trial>/verifier/score.json``.
    ``tests_passed`` / ``tests_total`` are retained as diagnostics only.
    Mirrors the trial discovery/naming used by ``runner.sharded_eval``
    (``<task>__<id>`` dirs).
    """
    by_task: dict[str, list[dict]] = defaultdict(list)
    seen_trials: set[str] = set()

    def _record(trial_dir: Path, score_path: Path | None) -> None:
        trial_name = trial_dir.name
        if "__" not in trial_name or trial_name in seen_trials:
            return
        seen_trials.add(trial_name)
        score = None
        score_source = ""
        passed = total = None
        status = None
        pure_count_ratio = False
        if score_path is not None and score_path.is_file():
            try:
                data = json.loads(score_path.read_text())
                if not isinstance(data, dict):
                    data = {}
                score, score_source = _score_from_payload(data)
                passed = _int_or_none(data.get("tests_passed"))
                total = _int_or_none(data.get("tests_total"))
                status = data.get("test_status")
                pure_count_ratio = _is_pure_count_ratio(data, score)
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                score = None
                passed = total = None
        task_name = trial_name.rsplit("__", 1)[0]
        by_task[task_name].append(
            {
                "trial": trial_name,
                "score": score,
                "score_source": score_source,
                "tests_passed": passed,
                "tests_total": total,
                "test_status": status,
                "pure_count_ratio": pure_count_ratio,
            }
        )

    # Primary source: every trial's verifier/score.json.
    for score_path in run_dir.rglob("score.json"):
        if score_path.parent.name != "verifier":
            continue
        _record(score_path.parent.parent, score_path)

    # Catch trials that have a result.json but no score.json: record them so
    # they count as build errors, not as absent.
    # Skip the run dir's own job-level result.json (its dir == run_dir, and its
    # timestamp name like ``2026-06-23__05-14-20`` would otherwise pass the
    # ``__`` filter and masquerade as a trial).
    run_dir = run_dir.resolve()
    for result_path in run_dir.rglob("result.json"):
        trial_dir = result_path.parent
        if trial_dir.resolve() == run_dir:
            continue
        if "__" not in trial_dir.name:
            continue
        _record(trial_dir, trial_dir / "verifier" / "score.json")

    for recs in by_task.values():
        _rebase_shrunken_denominators(recs)

    return by_task


def _rebase_shrunken_denominators(recs: list[dict]) -> None:
    """Re-score pure-count attempts against the max ``tests_total`` in the task.

    An attempt cannot inflate its pass ratio by dropping (or failing to emit)
    the tests it did not pass: doing so shrinks its ``tests_total`` denominator.
    Across a task's attempts the honest test suite is the largest reported one,
    so every pure-count attempt is re-based to ``tests_passed / max(tests_total)``.
    Only attempts flagged ``pure_count_ratio`` participate — composite LLM+rule
    scores keep their reported value (their ``tests_total`` is not the whole
    denominator). Mutates ``recs`` in place; leaves ``tests_passed`` /
    ``tests_total`` diagnostics untouched.
    """
    eligible = [
        rec
        for rec in recs
        if rec.get("pure_count_ratio")
        and _int_or_none(rec.get("tests_total"))
    ]
    if len(eligible) < 2:
        return
    max_total = max(int(rec["tests_total"]) for rec in eligible)
    if max_total <= 0:
        return
    for rec in eligible:
        passed = _int_or_none(rec.get("tests_passed")) or 0
        rebased = max(0.0, min(passed / max_total, 1.0))
        if abs(rebased - float(rec["score"])) < 1e-9:
            continue
        rec["score"] = rebased
        rec["score_source"] = "tests_passed_over_max_tests_total"


def _is_build_error(rec: dict) -> bool:
    return rec["score"] is None or rec["test_status"] == "build_error"


def compute_job_metrics(
    run_dir: Path, expected_tasks: Sequence[str] | None = None
) -> dict:
    """Compute job-level reward / pass_rate (plus per-task & per-attempt detail).

    See the module docstring for the exact contract. ``run_dir`` is a single
    run directory (``results/<job>/<run>``).

    ``expected_tasks`` is the manifest's task list (``selected_tasks``). When
    given, any expected task that produced no trial artifacts (crashed launch,
    never ran) is scored 0 so it counts in the denominator instead of silently
    vanishing — otherwise a partial run reports the mean over only the tasks
    that happened to run, inflating the job score. Missing task names are also
    surfaced under ``missing_tasks``. When None, discovered tasks alone define
    the denominator (backward-compatible).
    """
    by_task = _load_trials(run_dir)

    missing_tasks: list[str] = []
    if expected_tasks:
        missing_tasks = sorted(set(expected_tasks) - set(by_task))
        for task in missing_tasks:
            # Synthesize a single zero-scored, build-error trial so the task
            # lands in every denominator (per-task mean and attempt 1 bucket).
            by_task[task].append(
                {
                    "trial": f"{task}__never_ran",
                    "score": None,
                    "score_source": "",
                    "tests_passed": None,
                    "tests_total": None,
                    "test_status": "build_error",
                    "pure_count_ratio": False,
                }
            )

    if not by_task:
        raise ValueError(f"no trials with score.json/result.json found under: {run_dir}")

    per_task: dict[str, dict] = {}
    # Per-attempt buckets: attempt index -> list of (reward, full_pass) across tasks.
    attempt_reward: dict[int, list[float]] = defaultdict(list)
    attempt_pass: dict[int, list[float]] = defaultdict(list)
    score_sources: Counter[str] = Counter()
    n_trials = 0

    for task in sorted(by_task):
        recs = sorted(by_task[task], key=lambda r: r["trial"])
        rewards: list[float] = []
        passes: list[float] = []
        attempts: list[dict] = []
        for i, rec in enumerate(recs):
            n_trials += 1
            build_error = _is_build_error(rec)
            if build_error:
                r = 0.0
                full = 0.0
                score_sources["missing_or_build_error"] += 1
            else:
                r = float(rec["score"])
                full = 1.0 if r >= 1.0 else 0.0
                score_sources[rec.get("score_source") or "unknown"] += 1
            rewards.append(r)
            passes.append(full)
            attempt_reward[i].append(r)
            attempt_pass[i].append(full)
            attempts.append(
                {
                    "trial": rec["trial"],
                    "score_source": rec.get("score_source") or "",
                    "tests_passed": rec["tests_passed"],
                    "tests_total": rec["tests_total"],
                    "reward": round(r, 4),
                    "full_pass": bool(full),
                    "build_error": build_error,
                }
            )
        per_task[task] = {
            "n_attempts": len(recs),
            "reward": round(statistics.fmean(rewards), 4),
            "pass_rate": round(statistics.fmean(passes), 4),
            "attempts": attempts,
        }

    reward = round(statistics.fmean(m["reward"] for m in per_task.values()), 4)
    pass_rate = round(statistics.fmean(m["pass_rate"] for m in per_task.values()), 4)

    max_k = max(len(m["attempts"]) for m in per_task.values())
    per_attempt = [
        {
            "attempt": i + 1,
            "n_tasks": len(attempt_reward[i]),
            "reward": round(statistics.fmean(attempt_reward[i]), 4) if attempt_reward[i] else 0.0,
            "pass_rate": round(statistics.fmean(attempt_pass[i]), 4) if attempt_pass[i] else 0.0,
        }
        for i in range(max_k)
    ]

    return {
        "run_dir": str(run_dir),
        "reward": reward,
        "pass_rate": pass_rate,
        "n_tasks": len(per_task),
        "n_trials": n_trials,
        "missing_tasks": missing_tasks,
        "attempts_per_task": sorted({len(m["attempts"]) for m in per_task.values()}),
        "score_sources": dict(score_sources),
        "per_attempt": per_attempt,
        "per_task": per_task,
        "definitions": DEFINITIONS,
    }


def _print_summary(metrics: dict) -> None:
    print(f"run_dir   : {metrics['run_dir']}")
    print(f"tasks     : {metrics['n_tasks']}  trials: {metrics['n_trials']}  "
          f"attempts/task: {metrics['attempts_per_task']}")
    print(f"reward    : {metrics['reward']:.4f}   (mean verifier-reported score)")
    print(f"pass_rate : {metrics['pass_rate']:.4f}   (fraction of full-score trials)")
    if metrics.get("missing_tasks"):
        missing = metrics["missing_tasks"]
        print(f"MISSING   : {len(missing)} expected task(s) produced no trials "
              f"(scored 0): {', '.join(missing)}")
    if metrics.get("score_sources"):
        print(f"sources   : {metrics['score_sources']}")
    print("per-attempt:")
    for a in metrics["per_attempt"]:
        print(f"  attempt {a['attempt']}: reward={a['reward']:.4f}  "
              f"pass_rate={a['pass_rate']:.4f}  (n_tasks={a['n_tasks']})")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compute job-level reward / pass_rate for one WorkBuddy Bench run."
    )
    parser.add_argument(
        "run_dir",
        type=Path,
        help="A single run directory, e.g. results/<job>/<run>.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the full metrics object as JSON (incl. per-task / per-attempt detail).",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Resolved manifest.json for this run. Its selected_tasks defines the "
             "expected task set so tasks that never ran are scored 0 instead of "
             "dropped from the denominator.",
    )
    args = parser.parse_args()

    expected_tasks: list[str] | None = None
    if args.manifest is not None:
        manifest = json.loads(args.manifest.read_text())
        # selected_tasks == [] means "no restriction" (whole dataset); we can
        # only rebase against an explicit list, so an empty list stays None.
        expected_tasks = manifest.get("selected_tasks") or None

    metrics = compute_job_metrics(args.run_dir, expected_tasks=expected_tasks)
    if args.json:
        print(json.dumps(metrics, indent=2, ensure_ascii=False))
    else:
        _print_summary(metrics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
