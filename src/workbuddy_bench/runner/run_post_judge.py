"""Run the host-side white-box LLM judge over Harbor job dirs.

Two entry shapes share one judge implementation (``scorer/llm_judge.run_judge``);
the judge model/endpoint/params come from a ``configs/models/<slug>.yaml`` slug
in every case — nothing hardcoded.

1. Automatic (during a run, called by scripts/run.sh non-sharded path)::

       python3 -m workbuddy_bench.runner.run_post_judge \
           --manifest <instance>/manifest.json \
           --runtime-config <generated>/<job>.yaml

   Uses the resolved manifest (slug + proxy routing). Must run while the
   job-private proxy is still alive (before run.sh's EXIT trap tears it down).

2. Standalone (post-hoc, judge already-finished result dirs)::

       python3 -m workbuddy_bench.runner.run_post_judge \
           --job-config configs/jobs/<job>.yaml \
           --jobs results/<dir> [results/<dir2> ...] [--no-write-back]

   Resolves the judge slug from the job's bench + llm_judge_override (so the
   same switch/slug as a live run), then judges the given result dirs through
   the shared proxy route registered by ``scripts/judge/run-judge.sh``. Use this to
   add judge metrics to a run that was executed with the judge off.

Mode handling (shared contract): only ``mode: host_side`` judges run here. An
``in_container`` judge (e.g. web-auto's visual rubric) runs inside the dataset
verifier during the trial and cannot be re-judged post-hoc (its in-container
screenshots are gone) — the standalone path reports this and exits cleanly.

A disabled judge is a no-op. A judge failure is logged; it returns non-zero only
with ``--strict`` (run.sh treats it as non-fatal so the verifier reward stays
the gate).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import yaml

from workbuddy_bench.runner.resolve_manifest import resolve_llm_judge
from workbuddy_bench.runner.sharded_eval import find_harbor_job_dirs
from workbuddy_bench.scorer.llm_judge import (
    JudgeBackend,
    backend_from_manifest_data,
    backend_from_resolved_judge,
    run_judge,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _job_root_from_runtime_config(runtime_config: Path) -> Path:
    config = yaml.safe_load(runtime_config.read_text()) or {}
    jobs_dir = Path(config["jobs_dir"])
    if not jobs_dir.is_absolute():
        jobs_dir = _repo_root() / jobs_dir
    return jobs_dir.resolve()


# 1. Automatic path (manifest + runtime config)
def run_post_judge(manifest_path: Path, runtime_config: Path) -> int:
    manifest = json.loads(manifest_path.read_text())
    judge = manifest.get("llm_judge") or {}
    if not judge.get("enabled"):
        print("[post-judge] Disabled by resolved llm_judge config.")
        return 0
    if str(judge.get("mode") or "host_side") != "host_side":
        print(
            f"[post-judge] llm_judge.mode={judge.get('mode')!r} runs inside the "
            "dataset verifier (in_container); no host-side post-judge."
        )
        return 0

    job_root = _job_root_from_runtime_config(runtime_config)
    job_dirs = find_harbor_job_dirs(job_root)
    if not job_dirs:
        print(f"[post-judge] No Harbor job dirs found under {job_root}; skipping.")
        return 0

    backend = backend_from_manifest_data(manifest)
    return _judge_dirs(backend, job_dirs, write_back=True)


# 2. Standalone path (job config + explicit result dirs)
def _backend_from_job_config(
    job_config: Path, *, proxy_url: str = ""
) -> JudgeBackend:
    """Resolve a judge backend from a job config's slug (same contract as a live run)."""
    job = yaml.safe_load(job_config.read_text()) or {}
    configs_dir = job_config.resolve().parent.parent  # configs/jobs/.. -> configs
    resolved = resolve_llm_judge(configs_dir, job, job_config)
    try:
        return backend_from_resolved_judge(resolved, proxy_url=proxy_url)
    except RuntimeError as exc:
        raise RuntimeError(f"{job_config}: {exc}") from exc


def run_standalone(
    job_config: Path, job_dirs: list[str], *, write_back: bool, proxy_url: str = ""
) -> int:
    backend = _backend_from_job_config(job_config, proxy_url=proxy_url)
    resolved_dirs: list[Path] = []
    for raw in job_dirs:
        p = Path(raw)
        if not p.is_absolute():
            p = _repo_root() / raw
        # Accept either a job root (timestamped dirs underneath) or a concrete
        # job dir (trial folders directly underneath).
        discovered = find_harbor_job_dirs(p)
        if discovered:
            resolved_dirs.extend(discovered)
        elif any(sub.is_dir() and "__" in sub.name for sub in p.iterdir() if p.is_dir()):
            resolved_dirs.append(p)
        else:
            print(f"[judge] No trial dirs found under {p}; skipping.")
    if not resolved_dirs:
        print("[judge] No judgeable job dirs resolved from --jobs.")
        return 1
    return _judge_dirs(backend, resolved_dirs, write_back=write_back)


# shared executor
def _judge_dirs(backend: JudgeBackend, job_dirs: list[Path], *, write_back: bool) -> int:
    print(
        f"[judge] Judging {len(job_dirs)} job dir(s) with model={backend.model} "
        f"via {'proxy' if backend.via_proxy else 'direct'} (write_back={write_back})."
    )
    failures = 0
    for job_dir in job_dirs:
        output_path = str(job_dir / "llm_judge_summary.json")
        try:
            asyncio.run(
                run_judge([str(job_dir)], output_path, write_back=write_back, backend=backend)
            )
        except Exception as exc:  # noqa: BLE001 — log per-job, keep going
            failures += 1
            print(f"[judge] Judge failed for {job_dir}: {exc}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, help="Resolved run manifest (automatic path).")
    parser.add_argument(
        "--runtime-config", type=Path, help="Generated runtime job YAML (automatic path)."
    )
    parser.add_argument(
        "--job-config", type=Path, help="configs/jobs/<job>.yaml (standalone path)."
    )
    parser.add_argument(
        "--jobs", nargs="+", help="Result dirs to judge standalone (e.g. results/<dir>)."
    )
    parser.add_argument(
        "--no-write-back",
        action="store_true",
        help="Compute the judge summary without merging scores into reward.json.",
    )
    parser.add_argument(
        "--proxy-url",
        default="",
        help="Route the judge through this shared proxy. The judge is "
        "addressed by its slug route; the proxy injects extra_body and holds the "
        "upstream key. run-judge.sh always sets this after starting/registering "
        "the proxy. Empty = resolve the backend from the slug and call it directly "
        "(used by the automatic --manifest path).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return non-zero if any job's judge failed (default: best-effort, 0).",
    )
    parser.add_argument(
        "--print-judge-slug",
        action="store_true",
        help="Resolve the judge slug from --job-config and print '<slug> <enabled>', "
        "then exit. Lets run-judge.sh get the slug without re-implementing the "
        "resolution in shell.",
    )
    args = parser.parse_args()

    if args.print_judge_slug:
        if not args.job_config:
            parser.error("--print-judge-slug requires --job-config")
        job = yaml.safe_load(args.job_config.read_text()) or {}
        configs_dir = args.job_config.resolve().parent.parent
        resolved = resolve_llm_judge(configs_dir, job, args.job_config)
        slug = str(resolved.get("model_slug") or "") or "-"
        enabled = "1" if resolved.get("enabled") else "0"
        print(f"{slug} {enabled}")
        return 0

    if args.manifest and args.runtime_config:
        rc = run_post_judge(args.manifest, args.runtime_config)
    elif args.job_config and args.jobs:
        try:
            rc = run_standalone(
                args.job_config, args.jobs,
                write_back=not args.no_write_back,
                proxy_url=args.proxy_url,
            )
        except (RuntimeError, FileNotFoundError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    else:
        parser.error(
            "provide either (--manifest + --runtime-config) for the automatic "
            "path, or (--job-config + --jobs) for the standalone post-hoc path."
        )

    return rc if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
