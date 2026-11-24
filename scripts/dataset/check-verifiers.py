#!/usr/bin/env python3
"""Check published dataset verifiers against matching invariants.

The deterministic Security verifiers ship inside the dataset archives (one
byte-identical ``tests/verify_findings.py`` per task directory), so matching
regressions survive every archive revision until someone probes them. This
tool runs three offline probe classes against an extracted subset:

1. **Groundtruth self-consistency.** Feed each task's own ``groundtruth.json``
   back through the verifier as findings; every component the verifier scores
   from a synthesizable field must award 1. A miss means the verifier rejects
   its own answer key (fields never read, unreachable thresholds).

2. **Identifier-prefix over-acceptance** on the shared-matcher fields
   (``cwe``/``flag``/``family``): replace a ``cwe-<digits>`` candidate with a
   shorter, different digit prefix (CWE-787 -> CWE-78). A different identifier
   must not keep the component at 1.

3. **Insignificant-whitespace over-rejection**: strip internal spaces from a
   candidate with internal whitespace. The component must not drop to 0
   (warn tier; ``--strict`` promotes it to a failure).

Probe runs execute the verifier file as shipped. ``--engine docker`` (default
when Docker is available) reuses the runner's isolation contract: network off,
read-only mounts, capabilities dropped. ``--engine local`` rewrites only the
three absolute path literals (``/workdir``/``/tests``/``/logs/verifier``) to
point at a temporary sandbox, and records both file hashes in the report.

Usage::

    python3 scripts/dataset/check-verifiers.py --dataset datasets/wb-bench-sec-v1.0
    python3 scripts/dataset/check-verifiers.py --dataset ... --engine local --strict

Exit status: 0 when no probe failed, 1 when any probe failed or a verifier run
errored, 2 on usage/environment errors.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

VERIFIER_NAMES = ("verify_findings.py", "verifier.py")
GROUNDTRUTH_NAME = "groundtruth.json"
DEFAULT_IMAGE = "python:3.11-slim"
DEFAULT_TIMEOUT_SEC = 120

# Absolute path literals used by the shipped verifier family, matched against
# sandbox locations for ``--engine local``. Each must appear exactly once in
# the shipped source; any other count aborts that task as a shim error.
PATH_LITERALS = ('"/workdir"', '"/tests"', '"/logs/verifier"')

# The verifier dispatches each scored component to one findings field; these
# are the components backed by the shared substring matcher and therefore the
# ones the boundary probes apply to.
MATCHER_COMPONENTS = {"cwe_correct": "cwe", "flag_correct": "flag", "family_correct": "family"}

CWE_SHAPE = re.compile(r"(?i)^cwe-\d+$")
INTERNAL_WHITESPACE = re.compile(r"\S\s+\S")
PROBE_SENTINEL = "zzz-probe-no-match-000"
EXPECTED_SUFFIX = "_expected"


def _to_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Findings synthesis


def synthesizable_field(component: str, gt: dict[str, Any]) -> str | None:
    """Return the findings field that feeds ``component``, or None.

    ``trigger_correct`` is deliberately unsupported: its groundtruth holds
    regex patterns, and a pattern does not necessarily match its own literal
    source, so no faithful "perfect answer" can be synthesized here.
    """
    if component in MATCHER_COMPONENTS:
        return MATCHER_COMPONENTS[component]
    fixed = {
        "severity_correct": "severity",
        "ioc_recall": "ioc",
        "ttp_correct": "ttp",
        "chain_correct": "attack_chain",
        "vuln_lines_correct": "vuln_lines",
        "idea_correct": "exploit_idea",
    }
    if component in fixed:
        return fixed[component]
    if component.endswith(("_resisted", "_detected")) and gt.get(component + EXPECTED_SUFFIX) is not None:
        return component
    if gt.get(component + EXPECTED_SUFFIX) is not None:
        return component
    return None


def self_consistency_findings(gt: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """Build the answer key as findings, mapping each component to its field."""
    findings: dict[str, Any] = {}
    mapping: dict[str, str] = {}
    unsupported: list[str] = []
    for component in (str(c) for c in _to_list(gt.get("keys"))):
        source = synthesizable_field(component, gt)
        if source is None:
            unsupported.append(component)
            continue
        mapping[component] = source
        if component == "severity_correct":
            findings[source] = gt.get("severity_score")
        elif component == "chain_correct":
            findings[source] = " ".join(str(k) for k in _to_list(gt.get("chain_keywords")) if k)
        elif component == "idea_correct":
            joined = " ".join(str(k) for k in _to_list(gt.get("idea_keywords")) if k)
            findings["exploit_idea"] = joined
            findings["trigger_summary"] = joined
            findings["trigger_condition"] = joined
        else:
            gt_key = component + EXPECTED_SUFFIX if gt.get(component + EXPECTED_SUFFIX) is not None else source
            findings[source] = gt.get(gt_key)
    return {k: v for k, v in findings.items() if v is not None}, mapping, unsupported


def cwe_prefix_probe(gt: dict[str, Any], field: str) -> dict[str, str] | None:
    """Longest different digit-prefix of a cwe-<digits> candidate, if any."""
    candidates = [str(v).strip() for v in _to_list(gt.get(field)) if v]
    for candidate in candidates:
        if not CWE_SHAPE.match(candidate):
            continue
        head, digits = candidate.split("-", 1)
        for cut in range(len(digits) - 1, 0, -1):
            mutant = f"{head}-{digits[:cut]}"
            if mutant not in candidates:
                return {"candidate": candidate, "mutant": mutant}
    return None


def whitespace_probe(gt: dict[str, Any], field: str) -> dict[str, str] | None:
    """First candidate carrying internal whitespace, collapsed."""
    for candidate in (str(v).strip() for v in _to_list(gt.get(field)) if v):
        if INTERNAL_WHITESPACE.search(candidate):
            collapsed = re.sub(r"\s+", "", candidate)
            if collapsed and collapsed != candidate:
                return {"candidate": candidate, "mutant": collapsed}
    return None


# ---------------------------------------------------------------------------
# Verifier execution


def run_docker(verifier_path: Path, findings: dict[str, Any], image: str, timeout: int) -> dict[str, Any]:
    """Run the shipped verifier in the runner's isolation contract."""
    with tempfile.TemporaryDirectory(prefix="wb-check-verifiers-") as tmp:
        root = Path(tmp)
        work = root / "work"
        logs = root / "logs" / "verifier"
        work.mkdir(parents=True)
        logs.mkdir(parents=True)
        (work / "findings.json").write_text(json.dumps(findings), encoding="utf-8")
        command = [
            "docker", "run", "--rm", "--pull", "never", "--network", "none",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{verifier_path.parent}:/tests:ro",
            "-v", f"{work}:/workdir:ro",
            "-v", f"{logs}:/logs/verifier",
            image, "python3", f"/tests/{verifier_path.name}",
        ]
        proc = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
        return _collect(proc, logs)


def run_local(verifier_path: Path, findings: dict[str, Any], python: str, timeout: int) -> dict[str, Any]:
    """Run the verifier under a disclosed path-shim (three literals rewritten)."""
    with tempfile.TemporaryDirectory(prefix="wb-check-verifiers-") as tmp:
        root = Path(tmp)
        work = root / "work"
        logs = root / "logs" / "verifier"
        work.mkdir(parents=True)
        logs.mkdir(parents=True)
        (work / "findings.json").write_text(json.dumps(findings), encoding="utf-8")
        replacements = {
            '"/workdir"': json.dumps(str(work)),
            '"/tests"': json.dumps(str(verifier_path.parent)),
            '"/logs/verifier"': json.dumps(str(logs)),
        }
        source = verifier_path.read_text(encoding="utf-8")
        for literal in PATH_LITERALS:
            count = source.count(literal)
            if count != 1:
                raise RuntimeError(f"path literal {literal} appears {count} times, expected 1")
            source = source.replace(literal, replacements[literal], 1)
        shim = root / "verifier-shimmed.py"
        shim.write_text(source, encoding="utf-8")
        proc = subprocess.run([python, str(shim)], text=True, capture_output=True, timeout=timeout, cwd=root)
        return _collect(proc, logs)


def _collect(proc: subprocess.CompletedProcess[str], logs: Path) -> dict[str, Any]:
    if proc.returncode != 0:
        return {"error": f"verifier exited {proc.returncode}", "stderr": proc.stderr[-400:]}
    rewards = logs / "rewards.json"
    if not rewards.is_file():
        return {"error": "verifier wrote no rewards.json", "stderr": proc.stderr[-400:]}
    try:
        return {"results": json.loads(rewards.read_text(encoding="utf-8"))}
    except json.JSONDecodeError as exc:
        return {"error": f"rewards.json is not valid JSON: {exc}"}


# ---------------------------------------------------------------------------
# Per-task probes


def check_task(task_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    tests_dir = task_dir / "tests"
    verifier_path = next((tests_dir / name for name in VERIFIER_NAMES if (tests_dir / name).is_file()), None)
    gt_path = tests_dir / GROUNDTRUTH_NAME
    report: dict[str, Any] = {
        "task": task_dir.name,
        "verifier_sha256": _sha256(verifier_path) if verifier_path else None,
        "engine": args.engine,
    }
    if verifier_path is None or not gt_path.is_file():
        report["skipped"] = "no verifier or groundtruth"
        return report
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    components = [str(c) for c in _to_list(gt.get("keys"))]

    findings, mapping, unsupported = self_consistency_findings(gt)

    # Probe 1: groundtruth self-consistency.
    base = _run(report, verifier_path, findings, args)
    if "error" in base:
        report["self_consistency"] = {"status": "error", "detail": base["error"]}
    else:
        results = base["results"]
        failures = {c: results.get(c) for c in mapping if results.get(c) != 1}
        report["self_consistency"] = {
            "status": "fail" if failures else "pass",
            "components": results,
            "failures": failures,
            "unsupported": unsupported,
        }

    # Probe 2: identifier-prefix over-acceptance. A minimal findings document
    # with sentinel values in the untouched matcher fields keeps lenient
    # fallback scanners from rescuing the probed component.
    report["cwe_prefix"] = []
    for component in components:
        field = MATCHER_COMPONENTS.get(component)
        probe = cwe_prefix_probe(gt, field) if field else None
        if probe is None:
            continue
        mutated = {f: PROBE_SENTINEL for f in MATCHER_COMPONENTS.values()}
        mutated[field] = probe["mutant"]
        observed = _component(_run(report, verifier_path, mutated, args), component)
        report["cwe_prefix"].append({
            "component": component, "field": field, **probe,
            "observed": observed, "expected": 0,
            "status": "pass" if observed == 0 else "fail",
        })

    # Probe 3: insignificant-whitespace over-rejection.
    report["whitespace"] = []
    for component in components:
        field = MATCHER_COMPONENTS.get(component)
        probe = whitespace_probe(gt, field) if field else None
        if probe is None:
            continue
        mutated = {f: PROBE_SENTINEL for f in MATCHER_COMPONENTS.values()}
        mutated[field] = probe["mutant"]
        observed = _component(_run(report, verifier_path, mutated, args), component)
        status = "pass" if observed == 1 else ("fail" if args.strict else "warn")
        report["whitespace"].append({
            "component": component, "field": field, **probe,
            "observed": observed, "expected": 1, "status": status,
        })
    return report


def _run(report: dict[str, Any], verifier_path: Path, findings: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    try:
        if args.engine == "docker":
            run = run_docker(verifier_path, findings, args.image, args.timeout_sec)
        else:
            run = run_local(verifier_path, findings, args.python, args.timeout_sec)
    except (subprocess.TimeoutExpired, RuntimeError, OSError) as exc:
        run = {"error": str(exc)}
    if "error" in run:
        report.setdefault("run_errors", []).append(run["error"])
    return run


def _component(run: dict[str, Any], component: str) -> Any:
    if "error" in run:
        return "error"
    return run["results"].get(component)


# ---------------------------------------------------------------------------
# Entry point


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe dataset verifiers for matching invariants.")
    parser.add_argument("--dataset", type=Path, required=True,
                        help="extracted subset directory (datasets/wb-bench-<name>-v<ver>)")
    parser.add_argument("--engine", choices=("docker", "local"), default=None,
                        help="docker (default when available) or a disclosed local path-shim")
    parser.add_argument("--image", default=DEFAULT_IMAGE, help="docker image for --engine docker")
    parser.add_argument("--python", default=sys.executable, help="python for --engine local")
    parser.add_argument("--timeout-sec", type=int, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument("--strict", action="store_true", help="treat whitespace warnings as failures")
    parser.add_argument("--json", type=Path, default=None, help="also write the full report here")
    args = parser.parse_args(argv)

    if args.engine is None:
        args.engine = "docker" if shutil.which("docker") and subprocess.run(
            ["docker", "info"], capture_output=True).returncode == 0 else "local"

    tasks_dir = args.dataset / "tasks"
    if not tasks_dir.is_dir():
        print(f"ERROR: no tasks/ under {args.dataset}", file=sys.stderr)
        return 2

    reports = []
    for task_dir in sorted(path for path in tasks_dir.iterdir() if path.is_dir()):
        report = check_task(task_dir, args)
        reports.append(report)
        print(f"[{report['engine']}] {report['task']}: {_headline(report)}", flush=True)

    failures = sum(_statuses(report).count("fail") for report in reports)
    warnings = sum(_statuses(report).count("warn") for report in reports)
    errors = sum(len(report.get("run_errors", [])) for report in reports)
    print(f"\n{len(reports)} tasks | fail {failures} | warn {warnings} | run errors {errors}")
    if args.json:
        args.json.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"full report: {args.json}")
    return 1 if failures or errors else 0


def _statuses(report: dict[str, Any]) -> list[str]:
    statuses = []
    for section in ("self_consistency", "cwe_prefix", "whitespace"):
        value = report.get(section)
        if isinstance(value, dict):
            statuses.append(str(value.get("status")))
        elif isinstance(value, list):
            statuses.extend(str(item.get("status")) for item in value)
    return statuses


def _headline(report: dict[str, Any]) -> str:
    if "skipped" in report:
        return f"skipped ({report['skipped']})"
    parts = []
    for section in ("self_consistency", "cwe_prefix", "whitespace"):
        value = report.get(section)
        if isinstance(value, dict):
            parts.append(f"{section}={value.get('status')}")
        elif isinstance(value, list) and value:
            worst = ("fail" if any(i.get("status") == "fail" for i in value)
                     else "warn" if any(i.get("status") == "warn" for i in value) else "pass")
            parts.append(f"{section}={worst}")
    return " ".join(parts) or "no probes"


if __name__ == "__main__":
    sys.exit(main())
