"""Load per-benchmark bench config from the ``configs/bench/`` directory.

The bench layer carries harbor-runtime invariants and per-benchmark defaults.
It is split across files so each benchmark (dataset) owns its own tuning:

* ``configs/bench/_default.yaml`` — the shared base (required). Holds bench-wide
  infra that never varies by benchmark (``jobs_dir``, ``environment``,
  ``agent_user``/``verifier_user``, ``n_concurrent_trials``, ``quiet``) plus the
  fallback values for the per-benchmark keys.
* ``configs/bench/<dataset_id>.yaml`` — per-benchmark overrides (optional),
  deep-merged on top of ``_default``. ``<dataset_id>`` is the name of the
  directory holding that dataset's ``dataset.toml`` (e.g. ``v1.1_verified``,
  ``v2.1``), matching ``[dataset] id``.

Resolution order (later wins): ``_default.yaml`` < ``<dataset_id>.yaml`` < a
job YAML's own keys / override blocks (applied by the job layer downstream).

This module is the single source of truth all bench readers go through
(``prepare_job``, ``resolve_manifest``, ``scripts/run.sh``) so the merge
semantics stay consistent.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

from workbuddy_bench.runner.config_loaders import (
    deep_merge,
    load_dataset_runtime_contract,
    load_yaml,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def dataset_id_for(dataset: str | Path, *, repo_root: Path | None = None) -> str:
    """Return the benchmark id for a ``dataset:`` path, or "" if undeterminable.

    The id is the name of the directory containing the dataset's
    ``dataset.toml`` (resolved by ``load_dataset_runtime_contract``), which
    matches the dataset's ``[dataset] id`` and the per-benchmark bench filename.
    """
    if not dataset:
        return ""
    root = repo_root or _repo_root()
    contract = load_dataset_runtime_contract(dataset, repo_root=root)
    if contract.dataset_root is not None:
        return contract.dataset_root.name
    return ""


def load_bench(
    configs_dir: Path,
    dataset: str | Path | None = None,
    *,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Resolve the bench config for ``dataset`` from ``configs/bench/``.

    Loads ``_default.yaml`` (required) and deep-merges
    ``<dataset_id>.yaml`` on top when the dataset resolves to a benchmark id
    with a matching file. An unknown/absent dataset yields ``_default`` alone.
    """
    bench_dir = configs_dir / "bench"
    base = load_yaml(bench_dir / "_default.yaml")
    ds_id = dataset_id_for(dataset, repo_root=repo_root) if dataset else ""
    if ds_id:
        ds_file = bench_dir / f"{ds_id}.yaml"
        if ds_file.is_file():
            base = deep_merge(base, load_yaml(ds_file))
    return base


def _emit_user_vars(job_config: Path) -> int:
    """Print shell-quoted AGENT_USER/VERIFIER_USER for run.sh.

    Resolves bench (``_default`` + per-dataset) for the job's dataset, then
    applies job-level ``agent_user``/``verifier_user`` overrides.
    """
    import shlex

    job = load_yaml(job_config)
    configs_dir = job_config.parent.parent  # configs/jobs/<job>.yaml → configs/
    bench = load_bench(configs_dir, job.get("dataset"))

    def emit(name: str, value: Any) -> None:
        print(f"{name}={shlex.quote(str(value or ''))}")

    emit("AGENT_USER", job.get("agent_user", bench.get("agent_user")))
    emit("VERIFIER_USER", job.get("verifier_user", bench.get("verifier_user")))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve bench config (configs/bench/) for a job."
    )
    parser.add_argument("job_config", type=Path)
    parser.add_argument(
        "--emit-user-vars",
        action="store_true",
        help="Print shell-quoted AGENT_USER/VERIFIER_USER for run.sh.",
    )
    args = parser.parse_args()
    if args.emit_user_vars:
        return _emit_user_vars(args.job_config)
    # Default: dump the resolved bench config as YAML.
    job = load_yaml(args.job_config)
    configs_dir = args.job_config.parent.parent
    bench = load_bench(configs_dir, job.get("dataset"))
    yaml.safe_dump(bench, sys.stdout, sort_keys=False, allow_unicode=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
