"""Split the run-level proxy request log into per-trial files after a run.

The bench proxy is a run-level shared process: it logs every request to a single
``scripts/logs/proxy/<instance_id>.jsonl`` and cannot know each trial's
``results/<slug>/<ts>/<trial>/`` path (Harbor decides that). But each record
carries a ``trial_id`` (the harness prefixes it onto the bearer token as
``{trial}::{route}``), so after the run we fan the records out by ``trial_id``
into each trial's ``agent/requests.jsonl``.

Only meaningful for ``model_connection: local_proxy`` runs with ``record_full_io``
on (the only case that produces this log). Any other case is a no-op.

Attribution: a record whose ``trial_id`` matches a discovered trial dir is moved
there. A record whose ``trial_id`` equals the run-level ``instance_id`` (a legacy
request whose token carried no trial), or whose trial dir cannot be found, is
"unattributed" and stays in the source file. If every record was attributed, the
source ``<instance_id>.jsonl`` is deleted; otherwise the unattributed lines are
written back and the file is kept.

Called automatically by scripts/run.sh after all evaluation + judging finishes
(covers both the sharded and non-sharded paths — they share one instance_id).
Failures are non-fatal (logged, returns 0) so they never gate a run.

    python3 -m workbuddy_bench.runner.split_proxy_log --manifest <instance>/manifest.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

from workbuddy_bench.runner.sharded_eval import find_harbor_job_dirs


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _trial_dirs_by_name(job_root: Path) -> dict[str, Path]:
    """Map trial_name -> trial dir under results/<slug>/<ts>/<trial>/."""
    by_name: dict[str, Path] = {}
    for run_dir in find_harbor_job_dirs(job_root):
        for sub in run_dir.iterdir():
            if sub.is_dir() and "__" in sub.name:
                by_name[sub.name] = sub
    return by_name


def _shard_filename(name: str) -> str:
    """Return a collision-free single path segment for a trial's log shard.

    A pure char-substitution mapping is not injective (``a:x`` and ``a_x`` would
    collide onto the same file, cross-contaminating trials), so append a hash of
    the full trial_id to keep distinct ids on distinct files.
    """
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in name)
    return f"{safe}.{digest}"


def split_proxy_log(
    manifest_path: Path,
    log_dir: Path | None = None,
    results_root: Path | None = None,
) -> int:
    manifest = json.loads(manifest_path.read_text())

    if str(manifest.get("model_connection") or "") != "local_proxy":
        print("[split-proxy-log] not a local_proxy run; nothing to split.")
        return 0
    if not manifest.get("record_full_io"):
        print("[split-proxy-log] record_full_io off; no per-request log to split.")
        return 0

    instance_id = str(manifest.get("instance_id") or "")
    job_slug = str(manifest.get("job_slug") or "")
    if not instance_id or not job_slug:
        print("[split-proxy-log] manifest missing instance_id/job_slug; skipping.")
        return 0

    log_dir = log_dir or (_repo_root() / "scripts" / "logs" / "proxy")
    src = log_dir / f"{instance_id}.jsonl"
    if not src.is_file():
        print(f"[split-proxy-log] no proxy log at {src}; nothing to split.")
        return 0

    results_root = results_root or (_repo_root() / "results")
    job_root = results_root / job_slug
    trial_dirs = _trial_dirs_by_name(job_root)
    if not trial_dirs:
        print(f"[split-proxy-log] no trial dirs under {job_root}; leaving log in place.")
        return 0

    tmp_dir = log_dir / f".split-{_shard_filename(instance_id)}.tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    tmp_files: dict[str, object] = {}
    unattributed_tmp = tmp_dir / "_unattributed.jsonl"
    attributed = 0
    unattributed = 0
    per_trial_counts: dict[str, int] = {}

    def _fh_for_trial(trial_id: str):
        fh = tmp_files.get(trial_id)
        if fh is None:
            fh = open(tmp_dir / f"{_shard_filename(trial_id)}.jsonl", "a", encoding="utf-8")
            tmp_files[trial_id] = fh
        return fh

    try:
        with open(src, "r", encoding="utf-8") as in_fh, open(
            unattributed_tmp, "w", encoding="utf-8"
        ) as unattr_fh:
            for raw_line in in_fh:
                line = raw_line.rstrip("\n")
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    unattr_fh.write(line + "\n")
                    unattributed += 1
                    continue
                trial_id = rec.get("trial_id") or (rec.get("meta") or {}).get("trial_id") or ""
                # A record tagged with the run-level id never carried a trial
                # (legacy token) and cannot be attributed to a single trial.
                target = trial_dirs.get(trial_id) if trial_id and trial_id != instance_id else None
                if target is None:
                    unattr_fh.write(line + "\n")
                    unattributed += 1
                else:
                    _fh_for_trial(trial_id).write(line + "\n")
                    attributed += 1
                    per_trial_counts[trial_id] = per_trial_counts.get(trial_id, 0) + 1
    finally:
        for fh in tmp_files.values():
            fh.close()

    for trial_id in per_trial_counts:
        trial_dir = trial_dirs[trial_id]
        agent_dir = trial_dir / "agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / f"{_shard_filename(trial_id)}.jsonl"
        with open(tmp_path, "r", encoding="utf-8") as in_fh, open(
            agent_dir / "requests.jsonl", "a", encoding="utf-8"
        ) as out_fh:
            shutil.copyfileobj(in_fh, out_fh, length=1024 * 1024)

    if unattributed:
        # Keep the source file, containing only what we could not attribute.
        shutil.move(str(unattributed_tmp), src)
        print(
            f"[split-proxy-log] wrote {attributed} record(s) to {len(per_trial_counts)} "
            f"trial(s); {unattributed} unattributed record(s) kept in {src}."
        )
    else:
        src.unlink()
        print(
            f"[split-proxy-log] wrote {attributed} record(s) to {len(per_trial_counts)} "
            f"trial(s); removed {src}."
        )
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="Resolved run manifest JSON.")
    parser.add_argument(
        "--log-dir", type=Path, default=None,
        help="Proxy log dir (default: <repo>/scripts/logs/proxy).",
    )
    args = parser.parse_args()
    try:
        return split_proxy_log(args.manifest, args.log_dir)
    except Exception as exc:  # non-fatal: never gate a run
        print(f"[split-proxy-log] WARNING: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
