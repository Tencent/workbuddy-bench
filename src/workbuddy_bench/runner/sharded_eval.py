#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import textwrap
import time
import tomllib
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import yaml

from workbuddy_bench.runner.prepare_tasks import (
    ensure_composite_verifier_contract,
    ensure_host_gateway_compose,
)
from workbuddy_bench.runner.prepare_job import compose_job_config

# Harbor names its job dir from ``datetime.now()`` truncated to seconds, so
# launching all shards within the same wall-clock second causes
# ``FileExistsError: Job directory ... already exists`` on N-1 of them. Stagger
# by this many seconds between shard launches to guarantee unique seconds.
LAUNCH_STAGGER_SEC = 1.1

try:
    from dirhash import dirhash
except ImportError:
    def dirhash(path: Path, algorithm: str) -> str:
        hasher = hashlib.new(algorithm)
        for file_path in sorted(p for p in Path(path).rglob("*") if p.is_file()):
            rel = str(file_path.relative_to(path)).encode()
            content = file_path.read_bytes()
            # Length-prefix each field so distinct (path, content) layouts can't
            # alias by concatenation (e.g. file "ab"/b"c" vs "a"/b"bc").
            hasher.update(len(rel).to_bytes(8, "big"))
            hasher.update(rel)
            hasher.update(len(content).to_bytes(8, "big"))
            hasher.update(content)
        return hasher.hexdigest()


DIFFICULTY_WEIGHT = {
    "easy": 1,
    "medium": 2,
    "hard": 3,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Harbor evaluations in parallel shards with optional resume support."
    )
    parser.add_argument("--config", required=True, help="Harbor job config path.")
    parser.add_argument(
        "--shards",
        type=int,
        default=2,
        help="Number of Harbor jobs to launch in parallel.",
    )
    parser.add_argument(
        "--per-shard-concurrency",
        type=int,
        default=None,
        help=(
            "Value passed to Harbor -n for each shard. When omitted, defaults to "
            "the job's resolved n_concurrent_trials (orchestrator_override), "
            "falling back to 2."
        ),
    )
    parser.add_argument(
        "--resume-job",
        action="append",
        default=[],
        help=(
            "Existing Harbor job directory whose completed results should be reused when "
            "their task_checksum matches the current task directory. Can be passed multiple times."
        ),
    )
    parser.add_argument(
        "--no-force-build",
        action="store_true",
        help="Pass --no-force-build to Harbor.",
    )
    parser.add_argument(
        "--disable-verification",
        action="store_true",
        help="Pass --disable-verification to Harbor.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print the shard plan and commands without launching Harbor.",
    )
    parser.add_argument(
        "--post-judge",
        action="store_true",
        help="After all shards finish, run LLM-as-Judge and merge scores into reward.json.",
    )
    parser.add_argument(
        "--model-name",
        default=None,
        help="Model route name / label for LLM judge output. Optional if --manifest is provided.",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help=(
            "Path to resolved run manifest JSON. When provided, dataset and "
            "model_route are read from the manifest, and it is passed through "
            "to prepare_job for model_route injection."
        ),
    )
    return parser.parse_args()


def load_tasks(tasks_dir: Path, include_tasks: set[str] | None = None) -> dict[str, dict[str, object]]:
    tasks: dict[str, dict[str, object]] = {}
    for task_dir in sorted(tasks_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        if include_tasks is not None and task_dir.name not in include_tasks:
            continue
        config = tomllib.loads((task_dir / "task.toml").read_text())
        difficulty = config.get("metadata", {}).get("difficulty", "medium")
        tasks[task_dir.name] = {
            "difficulty": difficulty,
            "weight": DIFFICULTY_WEIGHT.get(difficulty, 2),
            "checksum": dirhash(task_dir, "sha256"),
        }
    return tasks


def load_completed_tasks(
    repo_root: Path,
    tasks: dict[str, dict[str, object]],
    resume_jobs: list[str],
    n_attempts: int = 1,
) -> tuple[set[str], dict[str, tuple[str, float]], dict[str, list[Path]]]:
    """Return tasks that already have ``n_attempts`` matching-checksum trials.

    Harbor re-runs *all* attempts for an included task, so a task is only treated
    as done once it has accumulated at least ``n_attempts`` scored trials with the
    current task checksum. A task with too few attempts is left in ``remaining`` so
    it is re-run in full rather than being silently skipped under-sampled.

    Returns ``(completed, source, trial_dirs)`` where ``trial_dirs`` maps each
    completed task to the resume trial directories to reuse, so the caller can
    link them into the current job root.
    """
    counts: dict[str, int] = defaultdict(int)
    completed: set[str] = set()
    source: dict[str, tuple[str, float]] = {}
    trial_dirs: dict[str, list[Path]] = defaultdict(list)
    for job in resume_jobs:
        job_dir = (repo_root / job).resolve() if not Path(job).is_absolute() else Path(job)
        if not job_dir.exists():
            print(f"warning: resume job not found: {job}", file=sys.stderr)
            continue
        # Harbor writes trials either directly under the job dir
        # (<task>__<id>/result.json) or nested one level deeper for named jobs
        # (<timestamp>/<task>__<id>/result.json). rglob handles both layouts; the
        # "__" filter plus the tasks-membership check below reject non-trial hits.
        for result_path in job_dir.rglob("result.json"):
            parent_name = result_path.parent.name
            if "__" not in parent_name:
                continue
            try:
                data = json.loads(result_path.read_text())
            except json.JSONDecodeError:
                continue
            reward = ((data.get("verifier_result") or {}).get("rewards") or {}).get("reward")
            if reward is None:
                continue
            task_name = parent_name.split("__", 1)[0]
            if task_name not in tasks:
                continue
            if data.get("task_checksum") != tasks[task_name]["checksum"]:
                continue
            counts[task_name] += 1
            trial_dirs[task_name].append(result_path.parent)
            # Record the first matching trial as the reuse source (for logging).
            source.setdefault(task_name, (str(job_dir), reward))
    need = max(1, int(n_attempts))
    for task_name, count in counts.items():
        if count >= need:
            completed.add(task_name)
    # Only keep reuse-source / trial-dir entries for tasks that actually cleared
    # the attempt bar, so the run log never claims to reuse a task it re-runs.
    source = {name: src for name, src in source.items() if name in completed}
    trial_dirs = {name: dirs for name, dirs in trial_dirs.items() if name in completed}
    return completed, source, trial_dirs


def build_shards(tasks: dict[str, dict[str, object]], shard_count: int) -> list[list[str]]:
    shards: list[list[str]] = [[] for _ in range(shard_count)]
    shard_weights = [0 for _ in range(shard_count)]
    ordered = sorted(
        tasks.items(),
        key=lambda item: (-int(item[1]["weight"]), item[0]),
    )
    for task_name, task_info in ordered:
        index = min(range(shard_count), key=lambda i: (shard_weights[i], len(shards[i]), i))
        shards[index].append(task_name)
        shard_weights[index] += int(task_info["weight"])
    return [sorted(shard) for shard in shards if shard]


def llm_judge_config_from_manifest(
    manifest_data: dict[str, object],
    *,
    cli_post_judge: bool = False,
) -> dict[str, object]:
    """Return resolved post-run LLM judge settings for this manifest.

    The manifest's ``llm_judge`` block is already fully resolved by
    resolve_manifest (api_base/api_key_env/model are populated from a
    configs/models/<slug>.yaml only when enabled), so this is a pass-through.
    No endpoint or judge model id is defaulted here.
    """
    llm_judge = manifest_data.get("llm_judge")
    if isinstance(llm_judge, dict):
        params = llm_judge.get("params")
        return {
            "enabled": bool(llm_judge.get("enabled", False)),
            "mode": str(llm_judge.get("mode") or "host_side"),
            "api_base": str(llm_judge.get("api_base") or ""),
            "api_key_env": str(llm_judge.get("api_key_env") or ""),
            "model": str(llm_judge.get("model") or ""),
            "model_slug": str(llm_judge.get("model_slug") or ""),
            "params": params if isinstance(params, dict) else {},
        }
    return {
        "enabled": bool(cli_post_judge),
        "mode": "host_side",
        "api_base": "",
        "api_key_env": "",
        "model": "",
        "model_slug": "",
        "params": {},
    }


def should_run_llm_judge(
    manifest_data: dict[str, object],
    *,
    cli_post_judge: bool = False,
) -> bool:
    """Return whether the host-side post-run LLM judge should run."""
    cfg = llm_judge_config_from_manifest(manifest_data, cli_post_judge=cli_post_judge)
    return bool(cfg["enabled"]) and cfg["mode"] == "host_side"


def build_command(
    config: str,
    tasks_dir: str,
    include_tasks: list[str],
    per_shard_concurrency: int,
    no_force_build: bool,
    disable_verification: bool = False,
) -> list[str]:
    command = [
        "harbor",
        "run",
        "-c",
        config,
        "--path",
        tasks_dir,
        "-n",
        str(per_shard_concurrency),
    ]
    if no_force_build:
        command.append("--no-force-build")
    if disable_verification:
        command.append("--disable-verification")
    for task_name in include_tasks:
        command.extend(["-i", task_name])
    return command


def _launch_root_for(harbor_job_root: Path, manifest_data: dict | None) -> Path:
    launch_id = None
    if manifest_data:
        raw = manifest_data.get("instance_id")
        if isinstance(raw, str) and raw.strip():
            launch_id = raw.strip().replace("/", "_")
    if not launch_id:
        launch_id = f"sharded-launch-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    return harbor_job_root / ".launches" / launch_id


def main() -> int:
    args = parse_args()
    if args.shards < 1:
        raise SystemExit("--shards must be >= 1")
    if args.per_shard_concurrency is not None and args.per_shard_concurrency < 1:
        raise SystemExit("--per-shard-concurrency must be >= 1")

    repo_root = Path(__file__).resolve().parent.parent.parent.parent

    # Load manifest as the source of truth for dataset/model/task selection.
    if not args.manifest:
        raise SystemExit("--manifest is required; run definitions come from resolved job manifests")
    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = repo_root / manifest_path
    if not manifest_path.is_file():
        raise SystemExit(f"manifest not found: {manifest_path}")
    manifest_data: dict = json.loads(manifest_path.read_text())
    post_judge_config = llm_judge_config_from_manifest(
        manifest_data,
        cli_post_judge=args.post_judge,
    )
    post_judge_enabled = should_run_llm_judge(
        manifest_data,
        cli_post_judge=args.post_judge,
    ) and not args.disable_verification

    effective_tasks_dir = manifest_data.get("dataset")
    if not effective_tasks_dir:
        raise SystemExit("manifest is missing required 'dataset' key")
    tasks_dir = (repo_root / effective_tasks_dir).resolve()
    if not tasks_dir.exists():
        raise SystemExit(f"tasks directory not found: {tasks_dir}")

    # Resolve model name: manifest > CLI arg
    effective_model_name = args.model_name
    if manifest_data:
        effective_model_name = manifest_data.get("model_route", effective_model_name)
    if not effective_model_name:
        effective_model_name = "unknown"

    changed = ensure_host_gateway_compose(tasks_dir)
    print(f"prepared_task_compose_overrides={changed}")
    composite_contract_changed = ensure_composite_verifier_contract(tasks_dir)
    print(f"prepared_task_composite_contract={composite_contract_changed}")
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = repo_root / config_path
    runtime_config_path = compose_job_config(
        config_path, repo_root / ".workspace" / "data" / "generated" / "jobs",
        manifest_path=manifest_path,
    )
    try:
        runtime_config = str(runtime_config_path.relative_to(repo_root))
    except ValueError:
        runtime_config = str(runtime_config_path)
    if runtime_config != args.config:
        print(f"runtime_config={runtime_config}")
    harbor_job_root = _job_root_from_config(runtime_config_path, repo_root)
    n_attempts = _n_attempts_from_config(runtime_config_path)

    # Concurrency precedence: explicit --per-shard-concurrency (CLI / SHARD_CONCURRENCY
    # env) > job's resolved n_concurrent_trials (orchestrator_override) > 2.
    if args.per_shard_concurrency is None:
        per_shard_concurrency = _n_concurrent_from_config(runtime_config_path) or 2
        print(f"per_shard_concurrency={per_shard_concurrency} (from job n_concurrent_trials)")
    else:
        per_shard_concurrency = args.per_shard_concurrency
        print(f"per_shard_concurrency={per_shard_concurrency} (explicit)")
    existing_harbor_job_dirs = set(find_harbor_job_dirs(harbor_job_root))

    # Job-level task_selection is resolved into manifest.selected_tasks. When it
    # is empty there is no restriction and every task under the job dataset runs.
    selected = manifest_data.get("selected_tasks") or []
    include_tasks = set(selected) if selected else None
    if include_tasks:
        print(f"task_selection={manifest_data.get('task_selection')}")
    tasks = load_tasks(tasks_dir, include_tasks)
    completed, completed_source, completed_trial_dirs = load_completed_tasks(
        repo_root, tasks, args.resume_job, n_attempts
    )
    remaining = {name: info for name, info in tasks.items() if name not in completed}

    print(f"tasks_dir={effective_tasks_dir}")
    print(f"total_tasks={len(tasks)}")
    print(f"completed_current_checksum={len(completed)}")
    print(f"remaining={len(remaining)}")
    if completed:
        for task_name in sorted(completed):
            job_dir, reward = completed_source[task_name]
            print(f"reuse {task_name} from {job_dir} reward={reward}")

    # Link reused resume trials into the current job root so the post-run judge
    # and metrics (rooted there) include them. Done before the no-remaining
    # early return so a pure-resume run still surfaces its reused results.
    if completed_trial_dirs and not args.dry_run:
        n_linked = _link_resumed_trials(harbor_job_root, completed_trial_dirs)
        print(f"linked_resumed_trials={n_linked}")

    if not remaining:
        print("No remaining tasks to run.")
        return 0

    shards = build_shards(remaining, args.shards)
    launch_root = _launch_root_for(harbor_job_root, manifest_data)
    launch_root.mkdir(parents=True, exist_ok=True)

    plan = []
    for index, shard in enumerate(shards, start=1):
        difficulty_counts: dict[str, int] = defaultdict(int)
        for task_name in shard:
            difficulty_counts[str(remaining[task_name]["difficulty"])] += 1
        command = build_command(
            config=runtime_config,
            tasks_dir=effective_tasks_dir,
            include_tasks=shard,
            per_shard_concurrency=per_shard_concurrency,
            no_force_build=args.no_force_build,
            disable_verification=args.disable_verification,
        )
        plan.append(
            {
                "shard": index,
                "task_count": len(shard),
                "difficulty_counts": dict(sorted(difficulty_counts.items())),
                "tasks": shard,
                "command": command,
            }
        )

    plan_path = launch_root / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=2))
    print(f"plan_file={plan_path}")
    for item in plan:
        print(
            textwrap.dedent(
                f"""
                shard={item['shard']}
                task_count={item['task_count']}
                difficulty_counts={item['difficulty_counts']}
                tasks={' '.join(item['tasks'])}
                command={' '.join(item['command'])}
                """
            ).strip()
        )

    if args.dry_run:
        return 0

    processes: list[tuple[int, subprocess.Popen[bytes], Path]] = []
    for i, item in enumerate(plan):
        if i > 0:
            time.sleep(LAUNCH_STAGGER_SEC)
        log_path = launch_root / f"shard-{item['shard']:02d}.log"
        # The child inherits a duplicate fd, so close the parent's handle after Popen.
        with log_path.open("wb") as log_file:
            process = subprocess.Popen(
                item["command"],
                cwd=repo_root,
                env=os.environ.copy(),
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        processes.append((item["shard"], process, log_path))
        print(f"started shard={item['shard']} pid={process.pid} log={log_path}")

    exit_code = 0
    for shard, process, log_path in processes:
        code = process.wait()
        print(f"finished shard={shard} exit_code={code} log={log_path}")
        if code != 0:
            exit_code = code

    if args.disable_verification:
        print("[post-judge] Disabled because --disable-verification requested rollout-only.")
    elif post_judge_enabled and exit_code == 0:
        _run_post_judge(
            manifest_data=manifest_data,
            harbor_job_root=harbor_job_root,
        )
    elif not post_judge_enabled:
        mode = str(post_judge_config.get("mode") or "host_side")
        if post_judge_config.get("enabled") and mode != "host_side":
            print(
                f"[post-judge] Skipped: llm_judge.mode={mode!r} is not host_side."
            )
        else:
            print("[post-judge] Disabled by resolved llm_judge config.")
    else:
        # post_judge_enabled but a shard failed: fail closed (don't judge a
        # partial run) but say so, otherwise the skip is silent and looks like
        # the judge never ran for an unknown reason.
        print(
            f"[post-judge] Skipped: {sum(1 for s, p, _ in processes if p.returncode != 0)} "
            f"shard(s) exited non-zero (exit_code={exit_code}); "
            "completed trials were left unjudged."
        )

    return exit_code


def _run_post_judge(*, manifest_data: dict, harbor_job_root: Path) -> None:
    """Run the host-side white-box LLM judge over this run's Harbor job dirs.

    The judge model/endpoint/params + proxy routing come from the resolved
    manifest's ``llm_judge`` block; nothing is hardcoded here.
    diagnostics back into each trial's reward.json. A judge failure is logged
    but does not fail the run (the verifier reward is already the gate).
    """
    from workbuddy_bench.scorer.llm_judge import (
        JudgeBackend,
        backend_from_manifest_data,
        run_judge,
    )

    job_dirs = find_harbor_job_dirs(harbor_job_root)
    if not job_dirs:
        print(f"[post-judge] No Harbor job dirs found under {harbor_job_root}; skipping.")
        return

    try:
        backend: JudgeBackend = backend_from_manifest_data(manifest_data or {})
    except Exception as exc:  # noqa: BLE001 - judge config issues must not fail the run
        print(f"[post-judge] Could not resolve judge backend: {exc}; skipping.")
        return

    print(
        f"[post-judge] Judging {len(job_dirs)} job dir(s) with "
        f"model={backend.model} via {'proxy' if backend.via_proxy else 'direct'}."
    )
    for job_dir in job_dirs:
        output_path = str(job_dir / "llm_judge_summary.json")
        try:
            asyncio.run(
                run_judge(
                    [str(job_dir)],
                    output_path,
                    write_back=True,
                    backend=backend,
                )
            )
        except Exception as exc:  # noqa: BLE001 - log the judge failure without failing the run
            print(f"[post-judge] Judge failed for {job_dir}: {exc}")


def _job_root_from_config(config_path: Path, repo_root: Path) -> Path:
    """Return the configured Harbor jobs root for a runtime YAML config."""
    with config_path.open() as f:
        config = yaml.safe_load(f) or {}
    jobs_dir = Path(config["jobs_dir"])
    if not jobs_dir.is_absolute():
        jobs_dir = repo_root / jobs_dir
    return jobs_dir.resolve()


def _n_attempts_from_config(config_path: Path) -> int:
    """Return the resolved ``n_attempts`` from a runtime YAML config (>= 1)."""
    with config_path.open() as f:
        config = yaml.safe_load(f) or {}
    try:
        return max(1, int(config.get("n_attempts", 1)))
    except (TypeError, ValueError):
        return 1


def _n_concurrent_from_config(config_path: Path) -> int | None:
    """Return the resolved ``n_concurrent_trials`` from a runtime YAML config.

    ``prepare_job`` flattens ``orchestrator_override.n_concurrent_trials`` to a
    top-level ``n_concurrent_trials`` key, so the job's concurrency intent is
    readable here without re-merging layers. Returns ``None`` when unset or
    invalid so callers can fall back to their own default.
    """
    with config_path.open() as f:
        config = yaml.safe_load(f) or {}
    raw = config.get("n_concurrent_trials")
    if raw is None:
        return None
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return None


def _link_resumed_trials(
    harbor_job_root: Path,
    trial_dirs: dict[str, list[Path]],
) -> int:
    """Symlink reused resume trials into the current job root so they are scored.

    Resumed tasks are dropped from execution but their results live in the
    ``--resume-job`` directories, which the post-run judge / metrics (rooted at
    ``harbor_job_root``) never scan. Linking each reused ``<task>__<id>`` trial
    into a dedicated ``resumed-<...>`` job dir under the current root makes
    ``find_harbor_job_dirs`` pick them up without copying data or touching the
    source. Returns the number of trials linked.
    """
    linked = 0
    if not trial_dirs:
        return 0
    dest_job = harbor_job_root / "resumed-trials"
    dest_job.mkdir(parents=True, exist_ok=True)
    for task_name in sorted(trial_dirs):
        for src in trial_dirs[task_name]:
            src = src.resolve()
            dest = dest_job / src.name
            if dest.exists() or dest.is_symlink():
                continue  # already linked (idempotent across re-runs)
            try:
                dest.symlink_to(src, target_is_directory=True)
                linked += 1
            except OSError as exc:
                print(f"warning: could not link resumed trial {src}: {exc}", file=sys.stderr)
    return linked


def _find_harbor_job_dirs(job_root: Path) -> list[Path]:
    """Discover timestamped Harbor job dirs under a configured jobs root.

    Harbor typically creates timestamped job dirs like ``2026-04-20__12-30-00``
    under the configured ``jobs_dir``.  A real Harbor job dir has trial folders
    like ``add-lru-cache__NvXkUdR`` directly underneath it.
    """
    found: list[Path] = []
    if not job_root.exists():
        return found
    for d in sorted(job_root.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name.startswith("sharded-launch-"):
            continue
        has_trials = any(
            sub.is_dir() and "__" in sub.name
            for sub in d.iterdir()
        )
        if has_trials:
            found.append(d)
    return found


def find_harbor_job_dirs(job_root: Path) -> list[Path]:
    """Public wrapper used by standalone post-judge entrypoints."""

    return _find_harbor_job_dirs(job_root)


if __name__ == "__main__":
    raise SystemExit(main())
