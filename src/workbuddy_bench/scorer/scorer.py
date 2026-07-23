#!/usr/bin/env python3
"""
WorkBuddy Bench Scorer v2 - Correctness-first scoring.

Overall score = test_pass_rate (sole contributor).
Structural diff metrics (file_hit_rate, diff_coverage) are computed and
reported as diagnostic fields for analysis, but do not affect the overall score.
LLM-as-Judge scores can be merged in post-processing.

Usage (called by test.sh inside verifier container):
    python3 scorer.py --agent-patch agent.patch --gold-patch gold.patch \
        --test-pass 0.875 --output reward.json [--wall-time 48.5] \
        [--tests-passed 7 --tests-total 8]
"""

import argparse
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

# Paths to exclude from patch analysis (build artifacts, caches)
_IGNORE_DIRS = {"__pycache__", ".pytest_cache", ".git", "node_modules"}


def _should_ignore_path(path: str) -> bool:
    """Check if a file path should be excluded from patch analysis."""
    parts = path.split("/")
    if any(part in _IGNORE_DIRS for part in parts):
        return True
    if path.endswith(".pyc"):
        return True
    return False


def parse_patch_files(patch_text: str) -> dict[str, list[str]]:
    """Extract modified files and their added lines from a unified diff."""
    files: dict[str, list[str]] = {}
    current_file = None
    for line in patch_text.splitlines():
        # Skip binary file markers
        if line.startswith("Binary files"):
            current_file = None
            continue
        match = re.match(r"^diff --git a/(.*) b/(.*)", line)
        if match:
            dest_path = match.group(2)
            if _should_ignore_path(dest_path):
                current_file = None
                continue
            current_file = dest_path
            files[current_file] = []
            continue
        if current_file and line.startswith("+") and not line.startswith("+++"):
            files[current_file].append(line[1:])
    return files


def normalize_line(line: str) -> str:
    """Normalize a code line for fuzzy comparison."""
    normalized = line.strip()
    if not normalized:
        return ""
    # Strip trailing inline comments (Python style)
    normalized = re.sub(r"#.*$", "", normalized).rstrip()
    if not normalized:
        return ""
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = normalized.replace("'", '"')
    # Normalize spacing around parens/brackets
    normalized = normalized.replace("( ", "(").replace(" )", ")")
    normalized = normalized.replace("[ ", "[").replace(" ]", "]")
    return normalized


def compute_file_hit_rate(agent_files: set[str], gold_files: set[str]) -> float:
    if not gold_files:
        return 1.0
    return len(agent_files & gold_files) / len(gold_files)


def compute_diff_coverage(
    agent_patch: dict[str, list[str]],
    gold_patch: dict[str, list[str]],
    fuzzy: bool = True,
) -> float:
    """Fraction of gold added-lines covered by agent's patch."""
    if not gold_patch:
        return 1.0
    scores = []
    for fname, gold_lines in gold_patch.items():
        if not gold_lines:
            scores.append(1.0)
            continue
        agent_lines = agent_patch.get(fname, [])
        if fuzzy:
            agent_set = {normalize_line(line) for line in agent_lines if line.strip()}
            gold_set = {normalize_line(line) for line in gold_lines if line.strip()}
        else:
            agent_set = set(agent_lines)
            gold_set = set(gold_lines)
        if not gold_set:
            scores.append(1.0)
            continue
        overlap = len(agent_set & gold_set)
        scores.append(overlap / len(gold_set))
    return sum(scores) / len(scores) if scores else 0.0


def score(
    agent_patch_text: str,
    gold_patch_text: str,
    test_pass: float,
    wall_time: float | None = None,
    tests_passed: int | None = None,
    tests_total: int | None = None,
    llm_judge_score: float | None = None,
    explicit_test_status: str | None = None,
) -> dict:
    """Compute reward and diagnostic metrics.

    ``overall`` is the verifier/scorer reward for this task. LLM judge, if
    supplied by offline post-processing, is reported as a separate diagnostic
    field and never changes overall.
    """
    agent_files = parse_patch_files(agent_patch_text)
    gold_files = parse_patch_files(gold_patch_text)

    file_hit_rate = compute_file_hit_rate(set(agent_files.keys()), set(gold_files.keys()))
    diff_coverage_exact = compute_diff_coverage(agent_files, gold_files, fuzzy=False)
    diff_coverage_fuzzy = compute_diff_coverage(agent_files, gold_files, fuzzy=True)

    tpr = float(test_pass)
    overall = tpr

    # Determine test status
    if explicit_test_status is not None:
        test_status = explicit_test_status
    elif tests_total is not None and tests_total == 0:
        test_status = "build_error"
    elif tpr >= 1.0:
        test_status = "full_pass"
    elif tpr > 0:
        test_status = "partial_pass"
    else:
        test_status = "no_pass"

    result = {
        "test_pass_rate": round(tpr, 4),
        "test_status": test_status,
        "overall": round(overall, 4),
        # Diagnostic fields (not in overall)
        "file_hit_rate": round(file_hit_rate, 4),
        "diff_coverage": round(diff_coverage_fuzzy, 4),
        "diff_coverage_exact": round(diff_coverage_exact, 4),
        "agent_files_changed": len(agent_files),
        "agent_lines_added": sum(len(v) for v in agent_files.values()),
        "gold_files_changed": len(gold_files),
        "gold_lines_added": sum(len(v) for v in gold_files.values()),
    }
    if tests_passed is not None:
        result["tests_passed"] = tests_passed
    if tests_total is not None:
        result["tests_total"] = tests_total
    if llm_judge_score is not None:
        result["llm_judge"] = round(float(llm_judge_score), 4)
    if wall_time is not None:
        result["wall_time_sec"] = round(wall_time, 2)
    return result


def parse_junit_xml(xml_path: str) -> tuple[int, int, int]:
    """Parse JUnit XML to extract passed, failed, error counts.

    Returns:
        (passed, failed, errors) tuple
    """
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        # Handle both <testsuites> and <testsuite> as root
        if root.tag == "testsuites":
            suites = root.findall("testsuite")
        else:
            suites = [root]

        total_tests = 0
        total_failures = 0
        total_errors = 0
        total_skipped = 0
        for suite in suites:
            total_tests += int(suite.get("tests", 0))
            total_failures += int(suite.get("failures", 0))
            total_errors += int(suite.get("errors", 0))
            total_skipped += int(suite.get("skipped", 0))

        passed = max(0, total_tests - total_failures - total_errors - total_skipped)
        return passed, total_failures, total_errors
    except (ET.ParseError, FileNotFoundError, ValueError):
        return 0, 0, 0


def parse_junit_testcases(xml_path: str) -> list[dict]:
    """Parse JUnit XML into one record per ``<testcase>``.

    Returns a list of ``{"classname", "name", "status", "detail"}`` dicts, where
    ``status`` is one of ``pass`` / ``fail`` / ``error`` / ``skipped``. This is
    the testcase-level companion to :func:`parse_junit_xml` (which returns only
    suite aggregates); it lets the unit-test judge expand a pytest run into atomic
    items. Returns ``[]`` on a missing/unparseable file or a results file that
    carries only suite-level attributes (no ``<testcase>`` children).
    """
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except (ET.ParseError, FileNotFoundError, ValueError):
        return []

    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
    cases: list[dict] = []
    for suite in suites:
        for case in suite.findall("testcase"):
            failure = case.find("failure")
            error = case.find("error")
            skipped = case.find("skipped")
            if error is not None:
                status, node = "error", error
            elif failure is not None:
                status, node = "fail", failure
            elif skipped is not None:
                status, node = "skipped", skipped
            else:
                status, node = "pass", None
            detail = None
            if node is not None:
                detail = node.get("message") or (node.text or "").strip() or None
            cases.append(
                {
                    "classname": case.get("classname", ""),
                    "name": case.get("name", ""),
                    "status": status,
                    "detail": detail,
                }
            )
    return cases


def main():
    parser = argparse.ArgumentParser(description="WorkBuddy Scorer v2")
    parser.add_argument("--agent-patch", required=True)
    parser.add_argument("--gold-patch", required=True)
    parser.add_argument("--test-pass", type=float, default=None,
                        help="Test pass rate (0-1). Overridden by --junit-xml if provided.")
    parser.add_argument("--junit-xml", default=None,
                        help="Path to JUnit XML results file (preferred over --test-pass)")
    parser.add_argument("--tests-passed", type=int, default=None)
    parser.add_argument("--tests-total", type=int, default=None)
    parser.add_argument("--test-status", default=None)
    parser.add_argument("--wall-time", type=float, default=None)
    parser.add_argument("--llm-judge", type=float, default=None,
                        help="Pre-computed LLM judge score (0-1)")
    parser.add_argument("--heldout-pass", type=float, default=None,
                        help="Held-out test pass rate (0-1), reported but not in overall")
    parser.add_argument("--heldout-passed", type=int, default=None)
    parser.add_argument("--heldout-total", type=int, default=None)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    # Determine test results from JUnit XML or CLI args
    tests_passed = args.tests_passed
    tests_total = args.tests_total
    test_pass = args.test_pass

    if args.junit_xml and Path(args.junit_xml).exists():
        passed, failed, errors = parse_junit_xml(args.junit_xml)
        tests_passed = passed
        tests_total = passed + failed + errors
        test_pass = round(passed / tests_total, 4) if tests_total > 0 else 0.0

    if test_pass is None:
        test_pass = 0.0

    agent_text = Path(args.agent_patch).read_text() if Path(args.agent_patch).exists() else ""
    gold_text = Path(args.gold_patch).read_text() if Path(args.gold_patch).exists() else ""
    result = score(
        agent_text, gold_text, test_pass,
        wall_time=args.wall_time,
        tests_passed=tests_passed,
        tests_total=tests_total,
        llm_judge_score=args.llm_judge,
        explicit_test_status=args.test_status,
    )

    if args.heldout_pass is not None:
        result["heldout_pass_rate"] = round(args.heldout_pass, 4)
    if args.heldout_passed is not None:
        result["heldout_passed"] = args.heldout_passed
    if args.heldout_total is not None:
        result["heldout_total"] = args.heldout_total

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
