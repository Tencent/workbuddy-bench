#!/usr/bin/env python3
"""
White-box LLM judge for completed WorkBuddy-Bench trials.

The judge is intentionally not an agent. It deterministically builds a compact
context from existing task/result artifacts, sends one structured prompt to a
judge model, and writes the weighted score back in the same places as the legacy
judge.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import statistics
import tempfile
import time
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from workbuddy_bench.runner.model_endpoints import host_reachable_url, openai_api_base_url
from workbuddy_bench.runner.model_params import flatten_params
from workbuddy_bench.scorer.scorer import score as compute_reward


@dataclass(frozen=True)
class JudgeBackend:
    """Resolved judge endpoint + model identity for a run.

    The judge model is not hardcoded here: ``api_base`` / ``api_key`` / ``model``
    / ``params`` come from the resolved manifest ``llm_judge`` block, which
    resolve_manifest derived from a ``configs/models/<slug>.yaml`` slug. When the
    run uses the bench proxy (``via_proxy``), ``api_base`` points at the host
    proxy and ``model`` is the proxy route key (= the judge model slug); the
    proxy injects the model's extra_body and rewrites the backend model name.
    Sampling knobs (max_tokens / temperature) live in ``params`` (model.yaml),
    never inlined in call_llm.
    """

    api_base: str
    api_key: str
    model: str
    params: dict[str, Any]
    via_proxy: bool = False


# Default sampling fallbacks used only when the judge model.params omits them.
_DEFAULT_JUDGE_MAX_OUTPUT_TOKENS = 2048
_DEFAULT_JUDGE_TEMPERATURE = 0.1

MAX_CONCURRENT = int(os.environ.get("LLM_JUDGE_MAX_CONCURRENT", "4"))
RETRY_SLEEP_SEC = float(os.environ.get("LLM_JUDGE_RETRY_SLEEP", "3"))

MAX_PROMPT_CHARS = int(os.environ.get("LLM_JUDGE_MAX_PROMPT_CHARS", "52000"))
MAX_PATCH_CHARS = int(os.environ.get("LLM_JUDGE_MAX_PATCH_CHARS", "14000"))
MAX_TEST_FILE_CHARS = int(os.environ.get("LLM_JUDGE_MAX_TEST_FILE_CHARS", "9000"))
MAX_TEST_TOTAL_CHARS = int(os.environ.get("LLM_JUDGE_MAX_TEST_TOTAL_CHARS", "22000"))
MAX_FAILURE_CHARS = int(os.environ.get("LLM_JUDGE_MAX_FAILURE_CHARS", "12000"))
MAX_AGENT_RESPONSE_CHARS = int(os.environ.get("LLM_JUDGE_MAX_AGENT_RESPONSE_CHARS", "2500"))
PARSE_RETRIES = int(os.environ.get("LLM_JUDGE_PARSE_RETRIES", "1"))
CONTRADICTION_RETRIES = int(os.environ.get("LLM_JUDGE_CONTRADICTION_RETRIES", "1"))
CONTRADICTION_REWARD_THRESHOLD = float(os.environ.get("LLM_JUDGE_CONTRADICTION_REWARD_THRESHOLD", "0.8"))
CONTRADICTION_SCORE_THRESHOLD = float(os.environ.get("LLM_JUDGE_CONTRADICTION_SCORE_THRESHOLD", "0.2"))
ZERO_TPR_INTERFACE_THRESHOLD = float(os.environ.get("LLM_JUDGE_ZERO_TPR_INTERFACE_THRESHOLD", "0.2"))
ZERO_TPR_INTEGRATION_THRESHOLD = float(os.environ.get("LLM_JUDGE_ZERO_TPR_INTEGRATION_THRESHOLD", "0.2"))
ZERO_TPR_LOW_ALIGNMENT_CAP = float(os.environ.get("LLM_JUDGE_ZERO_TPR_LOW_ALIGNMENT_CAP", "0.45"))

WEIGHTS: dict[str, float] = {
    "intent_coverage": 0.30,
    "semantic_correctness": 0.30,
    "interface_alignment": 0.15,
    "test_implied_edge_cases": 0.10,
    "integration_fit": 0.05,
    "regression_risk": 0.05,
    "code_quality": 0.05,
}

SCORE_FIELDS = tuple(WEIGHTS.keys())
FAILURE_MODES = {
    "fully_correct",
    "interface_mismatch_only",
    "partial_functionality",
    "wrong_behavior",
    "hardcoded_tests",
    "test_only_escape",
    "missing_artifact",
    "destructive_or_regressive",
    "judge_uncertain",
}

LOW_SCORE_EXEMPT_FAILURE_MODES = {
    "hardcoded_tests",
    "test_only_escape",
    "missing_artifact",
    "destructive_or_regressive",
}

_PARSE_STATUS_KEY = "_judge_parse_status"


JUDGE_PROMPT_TEMPLATE = """\
You are a strict but fair white-box evaluator for WorkBuddy-Bench.

The agent solved the task from the user instruction only. The benchmark tests
may include implicit names, signatures, paths, or entrypoints that were not
clear in the instruction. Your job is to distinguish semantic implementation
quality from benchmark contract alignment.

Use all provided white-box evidence:
- user instruction
- task metadata
- gold patch
- verifier tests and verifier code snippets
- agent patch
- test results and failure output

Do not judge by patch similarity. Judge whether the agent implemented the
functionality requested by the instruction, while separately scoring whether it
matched the test-implied interface/entrypoint contract.

Scoring dimensions, all floats in [0, 1]:
- intent_coverage: coverage of explicit user requirements.
- semantic_correctness: whether the implemented behavior is functionally right,
  even if names/signatures differ from tests.
- interface_alignment: alignment with test-implied function names, parameters,
  files, CLI/API entrypoints, output paths, and artifact formats.
- test_implied_edge_cases: coverage of edge cases revealed by tests/gold patch.
- integration_fit: whether the implementation is wired into the existing repo
  and required artifact locations rather than isolated code.
- regression_risk: 1 means low risk; penalize broad unrelated changes, broken
  compatibility, unsafe behavior, hardcoded test answers, or destructive edits.
- code_quality: readability, simplicity, maintainability, and local style.

Weighted score_overall must be:
0.30*intent_coverage + 0.30*semantic_correctness +
0.15*interface_alignment + 0.10*test_implied_edge_cases +
0.05*integration_fit + 0.05*regression_risk + 0.05*code_quality.

Also compute latent_functional_score:
0.45*intent_coverage + 0.45*semantic_correctness +
0.10*test_implied_edge_cases.

Choose one failure_mode:
- fully_correct
- interface_mismatch_only
- partial_functionality
- wrong_behavior
- hardcoded_tests
- test_only_escape
- missing_artifact
- destructive_or_regressive
- judge_uncertain

Important calibration:
- If tests fail mostly because the benchmark expected an unstated function name,
  parameter name, file path, or CLI entrypoint, semantic_correctness can be high
  while interface_alignment is low. Use failure_mode=interface_mismatch_only.
- If the agent only changed tests when source/product/report artifacts were
  required, use failure_mode=test_only_escape and cap score_overall at 0.25.
- If the agent hardcoded visible tests or fixture values instead of implementing
  the requested behavior, use failure_mode=hardcoded_tests and cap score_overall
  at 0.35.
- If a required artifact is missing or agent_patch is empty, score low unless
  the task explicitly required no code/artifact changes.
- Passing tests are strong evidence, but still check for hardcoding, regressions,
  overbroad changes, and mismatch with the user's instruction.

Return JSON only, with exactly this shape:
{{
  "scores": {{
    "intent_coverage": 0.0,
    "semantic_correctness": 0.0,
    "interface_alignment": 0.0,
    "test_implied_edge_cases": 0.0,
    "integration_fit": 0.0,
    "regression_risk": 0.0,
    "code_quality": 0.0,
    "score_overall": 0.0,
    "latent_functional_score": 0.0
  }},
  "failure_mode": "judge_uncertain",
  "is_instruction_ambiguous": false,
  "would_adapter_likely_fix": false,
  "interface_mismatch": {{
    "expected": "",
    "agent_provided": "",
    "explanation": ""
  }},
  "evidence": ["short evidence item 1", "short evidence item 2"],
  "rationale": "brief explanation"
}}

White-box context:
{context_json}
"""


@dataclass
class TextBlock:
    text: str
    truncated: bool = False
    original_chars: int = 0


def _read_text(path: Path, limit: int | None = None) -> TextBlock:
    if not path.exists() or not path.is_file():
        return TextBlock("")
    text = path.read_text(errors="replace")
    original = len(text)
    if limit is not None and original > limit:
        text = text[:limit] + f"\n...[truncated {original - limit} chars]"
        return TextBlock(text, True, original)
    return TextBlock(text, False, original)


def _safe_json_load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(errors="replace"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _safe_toml_load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(errors="replace"))
    except Exception:
        return {}


def _clamp_score(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    if number < 0:
        return 0.0
    if number > 1:
        return 1.0
    return round(number, 4)


def _reward_score_or_none(payload: dict[str, Any]) -> float | None:
    """Return a canonical verifier score, or None when no score field exists.

    Keep this order aligned with ``scorer.metrics``: ``reward`` and ``overall``
    are final scores, while ``test_pass_rate`` is often the deterministic
    component retained for diagnostics by composite verifiers such as Office.
    """

    for key in ("reward", "overall", "test_pass_rate"):
        if key in payload and payload.get(key) is not None:
            return _clamp_score(payload.get(key), default=0.0)
    passed = payload.get("tests_passed")
    total = payload.get("tests_total")
    try:
        passed_f = float(passed)
        total_f = float(total)
    except (TypeError, ValueError):
        return None
    if total_f <= 0:
        return None
    return _clamp_score(passed_f / total_f, default=0.0)


def _reward_score(payload: dict[str, Any], *, default: float = 0.0) -> float:
    """Return a normalized verifier score from legacy or CompositeVerifier payloads."""

    score = _reward_score_or_none(payload)
    return default if score is None else score


def _first_reward_score(*payloads: dict[str, Any], default: float = 0.0) -> float:
    for payload in payloads:
        if not payload:
            continue
        score = _reward_score_or_none(payload)
        if score is not None:
            return score
    return default


def weighted_score(scores: dict[str, Any]) -> float:
    return round(sum(WEIGHTS[field] * _clamp_score(scores.get(field)) for field in SCORE_FIELDS), 4)


def latent_functional_score(scores: dict[str, Any]) -> float:
    return round(
        0.45 * _clamp_score(scores.get("intent_coverage"))
        + 0.45 * _clamp_score(scores.get("semantic_correctness"))
        + 0.10 * _clamp_score(scores.get("test_implied_edge_cases")),
        4,
    )


def extract_message_text(message: dict[str, Any]) -> str:
    """Return only the assistant's visible response content for JSON parsing."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
                continue
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type in (None, "text", "output_text") and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(parts)
    return ""


def parse_json_response(text: str | None) -> dict[str, Any] | None:
    text = (text or "").strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(1).strip())
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass
    return None


def _strip_json_string_literals(text: str) -> str:
    chars: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                in_string = False
                chars.append(char)
                continue
            chars.append(" ")
            continue
        chars.append(char)
        if char == '"':
            in_string = True
    return "".join(chars)


def _find_json_object_spans(text: str) -> list[tuple[int, int]]:
    stripped = _strip_json_string_literals(text)
    stack: list[int] = []
    spans: list[tuple[int, int]] = []
    for index, char in enumerate(stripped):
        if char == "{":
            stack.append(index)
        elif char == "}" and stack:
            start = stack.pop()
            if not stack:
                spans.append((start, index + 1))
    return spans


def recover_json_response(text: str | None) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None

    for start, end in _find_json_object_spans(text):
        candidate = text[start:end]
        parsed = parse_json_response(candidate)
        if isinstance(parsed, dict):
            return parsed

    score_match = re.search(r'"scores"\s*:\s*\{', text)
    if not score_match:
        return None

    score_start = text.find("{", score_match.end() - 1)
    if score_start == -1:
        return None
    for start, end in _find_json_object_spans(text[score_start:]):
        if start != 0:
            continue
        scores = parse_json_response(text[score_start : score_start + end])
        if not isinstance(scores, dict):
            continue
        recovered: dict[str, Any] = {"scores": scores, _PARSE_STATUS_KEY: "recovered"}
        mode_match = re.search(r'"failure_mode"\s*:\s*"([^"]+)"', text)
        if mode_match:
            recovered["failure_mode"] = mode_match.group(1)
        for key in ("is_instruction_ambiguous", "would_adapter_likely_fix"):
            bool_match = re.search(rf'"{key}"\s*:\s*(true|false)', text, flags=re.IGNORECASE)
            if bool_match:
                recovered[key] = bool_match.group(1).lower() == "true"
        return recovered

    return None


def parse_or_recover_json_response(text: str | None) -> tuple[dict[str, Any] | None, str]:
    parsed = parse_json_response(text)
    if parsed is not None:
        parsed[_PARSE_STATUS_KEY] = "parsed"
        return parsed, "parsed"

    recovered = recover_json_response(text)
    if recovered is not None:
        recovered.setdefault(_PARSE_STATUS_KEY, "recovered")
        return recovered, "recovered"

    return None, "failed"


def _judge_request_params(backend: JudgeBackend) -> tuple[int, float]:
    """Resolve (max_tokens, temperature) from the judge model.params.

    Sampling knobs come from ``configs/models/<slug>.yaml`` (model.params),
    surfaced through the manifest. Accepts either
    ``max_output_tokens`` (model.yaml convention) or ``max_tokens``; falls back
    to module defaults when unset.
    """
    params = backend.params or {}
    max_tokens = params.get("max_output_tokens", params.get("max_tokens"))
    try:
        max_tokens = int(max_tokens) if max_tokens is not None else _DEFAULT_JUDGE_MAX_OUTPUT_TOKENS
    except (TypeError, ValueError):
        max_tokens = _DEFAULT_JUDGE_MAX_OUTPUT_TOKENS
    temperature = params.get("temperature", _DEFAULT_JUDGE_TEMPERATURE)
    try:
        temperature = float(temperature)
    except (TypeError, ValueError):
        temperature = _DEFAULT_JUDGE_TEMPERATURE
    return max_tokens, temperature


async def call_llm(
    client: httpx.AsyncClient,
    backend: JudgeBackend,
    prompt: str,
    retries: int = 2,
) -> str:
    max_tokens, temperature = _judge_request_params(backend)
    body: dict[str, Any] = {
        "model": backend.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    # The judge honors the model's full params from configs/models/<slug>.yaml:
    # top-level sampling knobs (top_p, ...) plus the extra_body sub-block,
    # flattened into a flat request body. Under proxy transport the proxy injects
    # them; on direct transport we inline them here. Flattened params win over the
    # max_tokens/temperature fallbacks set above, so an explicit config value
    # takes precedence.
    if not backend.via_proxy:
        injected = flatten_params(backend.params or {})
        if injected:
            body.update(injected)

    for attempt in range(retries + 1):
        try:
            response = await client.post(
                f"{backend.api_base}/chat/completions",
                json=body,
                headers={
                    "Authorization": f"Bearer {backend.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=120.0,
            )
            response.raise_for_status()
            try:
                data = response.json()
            except (json.JSONDecodeError, ValueError) as exc:
                preview = response.text[:200] if response.text else "(empty)"
                raise RuntimeError(
                    f"Non-JSON response from {backend.api_base}/chat/completions "
                    f"(status {response.status_code}): {preview}"
                ) from exc
            return extract_message_text(data["choices"][0]["message"])
        except Exception as exc:
            if attempt < retries:
                await asyncio.sleep(RETRY_SLEEP_SEC)
                continue
            raise RuntimeError(f"LLM API call failed after {retries + 1} attempts: {exc}") from exc


def _patch_files(patch_text: str) -> list[str]:
    return re.findall(r"^diff --git a/(.*?) b/", patch_text, flags=re.MULTILINE)


def _is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith("_tests.py")
        or name == "conftest.py"
        or "/tests/" in path
        or "/test/" in path
        or ("test" in name.lower() and name.endswith(".py"))
    )


def check_test_only_escape(agent_patch: str, gold_patch: str) -> float:
    if not agent_patch.strip():
        return 1.0
    gold_files = _patch_files(gold_patch)
    if gold_files and all(_is_test_file(path) for path in gold_files):
        return 1.0
    agent_files = _patch_files(agent_patch)
    return 0.0 if agent_files and all(_is_test_file(path) for path in agent_files) else 1.0


def _extract_junit_failures(xml_path: Path, limit: int = 10) -> list[dict[str, str]]:
    if not xml_path.exists():
        return []
    try:
        root = ET.parse(xml_path).getroot()
    except (ET.ParseError, OSError):
        return []

    failures: list[dict[str, str]] = []
    for testcase in root.iter("testcase"):
        failure_node = testcase.find("failure")
        if failure_node is None:
            failure_node = testcase.find("error")
        if failure_node is None:
            continue
        text = failure_node.text or failure_node.get("message") or ""
        failures.append(
            {
                "classname": testcase.get("classname", ""),
                "name": testcase.get("name", ""),
                "message": failure_node.get("message", ""),
                "details": text[:1200],
            }
        )
        if len(failures) >= limit:
            break
    return failures


def _collect_test_files(task_root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    candidates: list[Path] = []
    for rel in ("tests", "environment"):
        base = task_root / rel
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix in {".pyc", ".gz", ".tar", ".zip"}:
                continue
            if path.name in {"gold.patch", "snapshot.tar.gz"}:
                continue
            if path.suffix in {".py", ".sh", ".json", ".toml", ".yaml", ".yml", ".txt", ".md"}:
                candidates.append(path)

    files: list[dict[str, Any]] = []
    total_chars = 0
    truncated_total = False
    for path in candidates:
        if total_chars >= MAX_TEST_TOTAL_CHARS:
            truncated_total = True
            break
        remaining = MAX_TEST_TOTAL_CHARS - total_chars
        limit = min(MAX_TEST_FILE_CHARS, remaining)
        block = _read_text(path, limit=limit)
        rel_path = str(path.relative_to(task_root))
        files.append(
            {
                "path": rel_path,
                "content": block.text,
                "truncated": block.truncated,
                "original_chars": block.original_chars or len(block.text),
            }
        )
        total_chars += len(block.text)

    return files, {
        "n_files_included": len(files),
        "n_files_available": len(candidates),
        "truncated_total": truncated_total,
        "max_total_chars": MAX_TEST_TOTAL_CHARS,
    }


def _repo_root_for_judge() -> Path:
    # src/workbuddy_bench/scorer/llm_judge.py -> repo root
    return Path(__file__).resolve().parents[3]


def _persistent_task_root(recorded: Path) -> Path | None:
    """Map a recorded (possibly-gone) task.path to the persistent dataset dir.

    A finished run records ``task.path`` as the runtime staged copy
    (``.workspace/tmp/staged/<run>/<dataset>/tasks/<task>``), which is cleaned up
    after the run, so post-hoc judging can't read gold.patch/instruction there.
    The same task lives permanently under ``datasets/<dataset>/tasks/<task>``, so
    we extract the trailing ``<dataset>/tasks/<task>`` and re-root it there.
    """
    parts = recorded.parts
    if "tasks" not in parts:
        return None
    i = parts.index("tasks")
    if i == 0 or i + 1 >= len(parts):
        return None
    dataset, task = parts[i - 1], parts[i + 1]
    candidate = _repo_root_for_judge() / "datasets" / dataset / "tasks" / task
    return candidate if candidate.is_dir() else None


def _task_root_from_trial(task_name: str, trial_dir: Path) -> Path:
    trial_result = _safe_json_load(trial_dir / "result.json")
    task_path = ((trial_result.get("config") or {}).get("task") or {}).get("path")
    if not task_path:
        config = _safe_json_load(trial_dir / "config.json")
        task_path = ((config.get("task") or {}).get("path") or config.get("task_path"))

    if task_path:
        recorded = Path(task_path)
        # Prefer the recorded path when it still exists (live run / same host);
        # otherwise re-root to the persistent dataset copy (post-hoc judging).
        if recorded.is_dir():
            return recorded
        persistent = _persistent_task_root(recorded)
        if persistent is not None:
            return persistent
        return recorded

    return Path("tasks") / task_name


def find_trials(job_dirs: list[str]) -> list[dict[str, str]]:
    trials: list[dict[str, str]] = []
    for job_dir in job_dirs:
        if not os.path.isdir(job_dir):
            continue
        for entry in os.listdir(job_dir):
            full = os.path.join(job_dir, entry)
            if not os.path.isdir(full):
                continue
            parts = entry.rsplit("__", 1)
            if len(parts) != 2:
                continue
            verifier_dir = os.path.join(full, "verifier")
            if os.path.isdir(verifier_dir):
                trials.append({
                    "task_name": parts[0],
                    "attempt_id": parts[1],
                    "trial_name": entry,
                    "trial_dir": full,
                    "verifier_dir": verifier_dir,
                    "job_dir": job_dir,
                })
    return sorted(trials, key=lambda item: (item["job_dir"], item["trial_name"]))


def load_trial_data(task_name: str, trial_info: dict[str, str]) -> dict[str, Any] | None:
    trial_dir = Path(trial_info["trial_dir"])
    verifier_dir = Path(trial_info["verifier_dir"])
    task_root = _task_root_from_trial(task_name, trial_dir)

    agent_patch_full = _read_text(verifier_dir / "agent.patch")
    gold_patch_full = _read_text(task_root / "tests" / "gold.patch")
    agent_patch = _read_text(verifier_dir / "agent.patch", limit=MAX_PATCH_CHARS)
    gold_patch = _read_text(task_root / "tests" / "gold.patch", limit=MAX_PATCH_CHARS)
    instruction = _read_text(task_root / "instruction.md", limit=8000)
    # The agent transcript filename differs per harness: cbc_agent writes
    # ``agent/cbc-output.txt``, cc_agent writes ``agent/cc-output.txt``. Try each
    # so both harness result dirs are judged with the agent response populated.
    agent_response = TextBlock(text="")
    for fname in ("cbc-output.txt", "cc-output.txt"):
        agent_response = _read_text(trial_dir / "agent" / fname, limit=MAX_AGENT_RESPONSE_CHARS)
        if agent_response.text:
            break
    test_output = _read_text(verifier_dir / "test_output.txt", limit=MAX_FAILURE_CHARS)
    test_stdout = _read_text(verifier_dir / "test-stdout.txt", limit=MAX_FAILURE_CHARS)
    reward = _safe_json_load(verifier_dir / "reward.json")
    score_payload = _safe_json_load(verifier_dir / "score.json")
    verifier_score = _first_reward_score(score_payload, reward)
    verifier_status = score_payload.get("test_status") or reward.get("test_status")
    task_toml = _safe_toml_load(task_root / "task.toml")
    test_files, test_file_meta = _collect_test_files(task_root)

    conversation_turns = 0
    conv_path = trial_dir / "conversation.jsonl"
    if conv_path.exists():
        try:
            conversation_turns = sum(1 for line in conv_path.open(errors="replace") if line.strip())
        except OSError:
            conversation_turns = 0

    context = {
        "task": task_name,
        "task_root": str(task_root),
        "metadata": task_toml.get("metadata", {}),
        "task_config": task_toml.get("task", {}),
        "instruction": instruction.text,
        "gold_patch": gold_patch.text,
        "agent_patch": agent_patch.text,
        "agent_patch_files": _patch_files(agent_patch.text),
        "gold_patch_files": _patch_files(gold_patch.text),
        "verifier_tests": test_files,
        "test_result": {
            "overall_reward": verifier_score,
            "test_pass_rate": verifier_score,
            "test_status": verifier_status,
            "tests_passed": score_payload.get("tests_passed", reward.get("tests_passed")),
            "tests_total": score_payload.get("tests_total", reward.get("tests_total")),
            "heldout_pass_rate": reward.get("heldout_pass_rate"),
            "failure_summary": _extract_junit_failures(verifier_dir / "results.xml"),
            "test_output": test_output.text or test_stdout.text,
        },
        "diagnostics": {
            "file_hit_rate": reward.get("file_hit_rate"),
            "diff_coverage": reward.get("diff_coverage"),
            "agent_files_changed": reward.get("agent_files_changed"),
            "agent_lines_added": reward.get("agent_lines_added"),
            "gold_files_changed": reward.get("gold_files_changed"),
            "gold_lines_added": reward.get("gold_lines_added"),
            "conversation_turns": conversation_turns,
            "test_only_escape_rule": check_test_only_escape(agent_patch_full.text, gold_patch_full.text),
        },
        "agent_response_excerpt": agent_response.text,
    }

    truncation = {
        "instruction": instruction.truncated,
        "gold_patch": gold_patch.truncated,
        "agent_patch": agent_patch.truncated,
        "test_output": test_output.truncated or test_stdout.truncated,
        "agent_response": agent_response.truncated,
        "test_files": test_file_meta,
    }

    return {
        "task_name": task_name,
        "attempt_id": trial_info.get("attempt_id", ""),
        "trial_name": trial_info.get("trial_name", trial_dir.name),
        "trial_dir": str(trial_dir),
        "verifier_dir": str(verifier_dir),
        "reward_path": str(verifier_dir / "reward.json"),
        "score_path": str(verifier_dir / "score.json"),
        "task_root": str(task_root),
        "instruction": instruction.text,
        "gold_patch": gold_patch.text,
        "agent_patch": agent_patch_full.text,
        "agent_patch_context": agent_patch.text,
        "test_pass_rate": verifier_score,
        "overall_reward": verifier_score,
        "test_status": verifier_status,
        "gold_patch_full": gold_patch_full.text,
        "context": context,
        "context_truncation": truncation,
    }


_NO_REFERENCE_NOTE = (
    "NO REFERENCE SOLUTION is available for this task (gold_patch is empty — the "
    "dataset shipped no oracle). Do NOT invent or assume a reference. Judge "
    "intent_coverage / semantic_correctness from the user instruction and the "
    "verifier tests ALONE. Lean on the test results as the primary correctness "
    "signal; when uncertain, prefer failure_mode=judge_uncertain over guessing. "
    "interface_alignment and test_implied_edge_cases still apply (they derive "
    "from the tests, not the gold patch)."
)


def build_prompt(context: dict[str, Any]) -> tuple[str, bool]:
    # No-reference mode: when the dataset shipped no gold patch, tell the judge
    # explicitly so it does not hallucinate a reference. Only triggers for
    # reference-free datasets; sets that ship a gold.patch per task never hit it.
    context = dict(context)
    if not str(context.get("gold_patch") or "").strip():
        context["reference_availability"] = _NO_REFERENCE_NOTE

    context_json = json.dumps(context, ensure_ascii=False, indent=2)
    prompt = JUDGE_PROMPT_TEMPLATE.format(context_json=context_json)
    if len(prompt) <= MAX_PROMPT_CHARS:
        return prompt, False

    compact = dict(context)
    compact["verifier_tests"] = [
        {
            "path": item.get("path"),
            "content": str(item.get("content", ""))[:3500],
            "truncated": True,
        }
        for item in compact.get("verifier_tests", [])[:6]
    ]
    compact["gold_patch"] = str(compact.get("gold_patch", ""))[:9000] + "\n...[prompt compacted]"
    compact["agent_patch"] = str(compact.get("agent_patch", ""))[:9000] + "\n...[prompt compacted]"
    if "test_result" in compact:
        compact["test_result"] = dict(compact["test_result"])
        compact["test_result"]["test_output"] = str(compact["test_result"].get("test_output", ""))[:6000]
    compact_json = json.dumps(compact, ensure_ascii=False, indent=2)
    prompt = JUDGE_PROMPT_TEMPLATE.format(context_json=compact_json)
    if len(prompt) > MAX_PROMPT_CHARS:
        prompt = prompt[:MAX_PROMPT_CHARS] + "\n...[prompt hard-truncated]"
    return prompt, True


def normalize_judge_data(
    data: dict[str, Any] | None,
    *,
    empty_patch: bool = False,
    test_pass_rate: float | None = None,
) -> dict[str, Any]:
    if not data:
        data = {}
    parse_status = str(data.get(_PARSE_STATUS_KEY) or "parsed")
    scores_in = data.get("scores") if isinstance(data.get("scores"), dict) else data
    scores = {field: _clamp_score(scores_in.get(field)) for field in SCORE_FIELDS}
    if empty_patch:
        for field in SCORE_FIELDS:
            scores[field] = 0.0

    score_overall = weighted_score(scores)
    latent_score = latent_functional_score(scores)
    failure_mode = str(data.get("failure_mode") or "judge_uncertain")
    if failure_mode not in FAILURE_MODES:
        failure_mode = "judge_uncertain"

    if failure_mode == "test_only_escape":
        score_overall = min(score_overall, 0.25)
    elif failure_mode == "hardcoded_tests":
        score_overall = min(score_overall, 0.35)
    elif failure_mode in {"missing_artifact", "destructive_or_regressive"}:
        score_overall = min(score_overall, 0.4)

    cap_reasons: list[str] = []
    tpr = _clamp_score(test_pass_rate, default=0.0) if test_pass_rate is not None else None
    if (
        tpr is not None
        and tpr <= 0.0
        and _clamp_score(scores.get("interface_alignment")) < ZERO_TPR_INTERFACE_THRESHOLD
        and _clamp_score(scores.get("integration_fit")) < ZERO_TPR_INTEGRATION_THRESHOLD
    ):
        capped = min(score_overall, ZERO_TPR_LOW_ALIGNMENT_CAP)
        if capped < score_overall:
            cap_reasons.append("zero_test_pass_low_interface_low_integration")
        score_overall = capped

    if empty_patch:
        score_overall = 0.0
        latent_score = 0.0
        failure_mode = "missing_artifact"

    scores["score_overall"] = round(score_overall, 4)
    scores["latent_functional_score"] = round(latent_score, 4)

    interface_mismatch = data.get("interface_mismatch")
    if not isinstance(interface_mismatch, dict):
        interface_mismatch = {"expected": "", "agent_provided": "", "explanation": ""}

    evidence = data.get("evidence")
    if not isinstance(evidence, list):
        evidence = []
    evidence = [str(item)[:300] for item in evidence[:6]]

    return {
        "scores": scores,
        "failure_mode": failure_mode,
        "is_instruction_ambiguous": bool(data.get("is_instruction_ambiguous", False)),
        "would_adapter_likely_fix": bool(data.get("would_adapter_likely_fix", False)),
        "interface_mismatch": {
            "expected": str(interface_mismatch.get("expected", ""))[:500],
            "agent_provided": str(interface_mismatch.get("agent_provided", ""))[:500],
            "explanation": str(interface_mismatch.get("explanation", ""))[:1000],
        },
        "evidence": evidence,
        "rationale": str(data.get("rationale", ""))[:1500],
        "parse_status": parse_status,
        "cap_reasons": cap_reasons,
    }


def is_judge_contradiction(trial_data: dict[str, Any], judge_result: dict[str, Any]) -> bool:
    reward_score = _clamp_score(
        trial_data.get("overall_reward", trial_data.get("test_pass_rate")),
        default=0.0,
    )
    judge_score = _clamp_score(judge_result.get("llm_judge"), default=0.0)
    failure_mode = judge_result.get("failure_mode")
    return (
        reward_score >= CONTRADICTION_REWARD_THRESHOLD
        and judge_score <= CONTRADICTION_SCORE_THRESHOLD
        and failure_mode not in LOW_SCORE_EXEMPT_FAILURE_MODES
    )


def mark_unscored(
    base_result: dict[str, Any],
    *,
    error: str,
    raw_response: str = "",
    parse_status: str = "failed",
    attempts: int = 0,
) -> dict[str, Any]:
    normalized = normalize_judge_data({}, test_pass_rate=base_result.get("test_pass_rate"))
    return {
        **base_result,
        **normalized,
        "score_overall": None,
        "llm_judge": None,
        "failure_mode": "judge_uncertain",
        "error": error,
        "raw_response": raw_response[:4000],
        "parse_status": parse_status,
        "judge_attempts": attempts,
    }


def is_scored_result(result: dict[str, Any]) -> bool:
    return result.get("error") is None and isinstance(result.get("llm_judge"), (int, float))


async def judge_task(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    backend: JudgeBackend,
    trial_data: dict[str, Any],
) -> dict[str, Any]:
    async with sem:
        task_name = trial_data["task_name"]
        trial_name = trial_data.get("trial_name") or task_name
        trial_key = trial_data.get("trial_dir") or trial_name
        empty_patch = not trial_data["agent_patch"].strip()
        prompt, prompt_compacted = build_prompt(trial_data["context"])
        base_result = {
            "task": task_name,
            "trial": trial_name,
            "trial_key": trial_key,
            "attempt_id": trial_data.get("attempt_id", ""),
            "judge_schema": "whitebox-v1",
            "context": {
                "task_root": trial_data["task_root"],
                "truncation": trial_data["context_truncation"],
                "prompt_chars": len(prompt),
                "prompt_compacted": prompt_compacted,
            },
            "test_pass_rate": trial_data["test_pass_rate"],
            "error": None,
        }

        if empty_patch:
            normalized = normalize_judge_data(
                {},
                empty_patch=True,
                test_pass_rate=trial_data["test_pass_rate"],
            )
            return {
                **base_result,
                **normalized,
                "score_overall": 0.0,
                "llm_judge": 0.0,
                "raw_response": "",
                "parse_status": "skipped_empty_patch",
                "judge_attempts": 0,
                "judge_contradiction": False,
            }

        try:
            last_response = ""
            last_parse_status = "failed"
            parse_attempts = PARSE_RETRIES + 1
            max_attempts = parse_attempts + CONTRADICTION_RETRIES
            parse_failures = 0
            contradiction_failures = 0
            for attempt in range(1, max_attempts + 1):
                last_response = await call_llm(client, backend, prompt)
                parsed, parse_status = parse_or_recover_json_response(last_response)
                last_parse_status = parse_status
                if parsed is None:
                    parse_failures += 1
                    if parse_failures >= parse_attempts:
                        break
                    continue

                normalized = normalize_judge_data(
                    parsed,
                    test_pass_rate=trial_data["test_pass_rate"],
                )
                score_overall = normalized["scores"]["score_overall"]
                result = {
                    **base_result,
                    **normalized,
                    "score_overall": score_overall,
                    "llm_judge": score_overall,
                    "raw_response": last_response[:4000] if parse_status != "parsed" else "",
                    "parse_status": parse_status,
                    "judge_attempts": attempt,
                    "judge_contradiction": False,
                }

                if not is_judge_contradiction(trial_data, result):
                    return result
                contradiction_failures += 1
                if contradiction_failures <= CONTRADICTION_RETRIES:
                    last_parse_status = "contradiction_retry"
                    continue

                result["judge_contradiction"] = True
                result["error"] = (
                    "judge_contradiction: high reward but very low llm_judge; "
                    "score excluded from summary and write-back"
                )
                result["parse_status"] = parse_status
                return result

            return mark_unscored(
                base_result,
                error=f"judge_parse_failed after {parse_failures} attempts",
                raw_response=last_response,
                parse_status=last_parse_status,
                attempts=parse_failures,
            )
        except Exception as exc:
            return mark_unscored(
                base_result,
                error=str(exc),
                parse_status="api_error",
            )


def merge_reward(
    trial_data: dict[str, Any],
    judge_result: dict[str, Any],
    *,
    judge_model: str,
) -> dict[str, Any]:
    reward_path = Path(trial_data["reward_path"])
    existing_reward = _safe_json_load(reward_path)
    base_score = _clamp_score(trial_data.get("test_pass_rate"), default=_reward_score(existing_reward))

    merged = compute_reward(
        trial_data["agent_patch"],
        trial_data["gold_patch_full"],
        base_score,
        wall_time=existing_reward.get("wall_time_sec"),
        tests_passed=existing_reward.get("tests_passed"),
        tests_total=existing_reward.get("tests_total"),
        llm_judge_score=judge_result.get("llm_judge"),
        explicit_test_status=trial_data.get("test_status") or existing_reward.get("test_status"),
    )
    merged["overall"] = base_score
    merged["test_pass_rate"] = base_score
    merged["reward"] = base_score
    for key in (
        "heldout_pass_rate",
        "heldout_passed",
        "heldout_total",
    ):
        if key in existing_reward:
            merged[key] = existing_reward[key]

    merged["llm_judge"] = round(float(judge_result.get("llm_judge", 0.0)), 4)
    merged["score_overall"] = round(float(judge_result.get("score_overall", 0.0)), 4)
    merged["judge_scores"] = judge_result.get("scores")
    merged["latent_functional_score"] = (judge_result.get("scores") or {}).get("latent_functional_score")
    merged["failure_mode"] = judge_result.get("failure_mode")
    merged["is_instruction_ambiguous"] = judge_result.get("is_instruction_ambiguous")
    merged["would_adapter_likely_fix"] = judge_result.get("would_adapter_likely_fix")
    merged["interface_mismatch"] = judge_result.get("interface_mismatch")
    merged["judge_parse_status"] = judge_result.get("parse_status")
    merged["judge_cap_reasons"] = judge_result.get("cap_reasons", [])
    merged["judge_contradiction"] = bool(judge_result.get("judge_contradiction", False))
    merged["judge_attempts"] = judge_result.get("judge_attempts")
    merged["judge_model"] = judge_model
    merged["judge_schema"] = judge_result.get("judge_schema", "whitebox-v1")
    return merged


def write_back_reward(trial_data: dict[str, Any], merged_reward: dict[str, Any]) -> None:
    reward_path = Path(trial_data["reward_path"])
    reward_path.parent.mkdir(parents=True, exist_ok=True)
    _replace_json(reward_path, merged_reward)
    score_path = Path(trial_data.get("score_path") or reward_path.with_name("score.json"))
    existing_score = _safe_json_load(score_path)
    merged_score = dict(existing_score)
    merged_score.update(merged_reward)
    _replace_json(score_path, merged_score)


def _replace_json(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def refresh_job_result(job_dir: Path) -> None:
    trial_scores: list[dict[str, Any]] = []
    for trial_dir in sorted(path for path in job_dir.iterdir() if path.is_dir() and "__" in path.name):
        score_path = trial_dir / "verifier" / "score.json"
        reward_path = trial_dir / "verifier" / "reward.json"
        score = _safe_json_load(score_path)
        reward = _safe_json_load(reward_path)
        if score or reward:
            trial_scores.append({"score": score, "reward": reward})
    if not trial_scores:
        return

    result_path = job_dir / "result.json"
    result_data = _safe_json_load(result_path)
    mean_overall = sum(
        _first_reward_score(item["score"], item["reward"]) for item in trial_scores
    ) / len(trial_scores)
    eval_bucket = _result_eval_bucket(result_data)
    metrics = eval_bucket.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        metrics = [{}]
        eval_bucket["metrics"] = metrics
    if not isinstance(metrics[0], dict):
        metrics[0] = {}
    metrics[0]["mean"] = mean_overall
    result_path.write_text(json.dumps(result_data, indent=2, ensure_ascii=False))


def _result_eval_bucket(result_data: dict[str, Any]) -> dict[str, Any]:
    evals = result_data.setdefault("stats", {}).setdefault("evals", {})
    if not isinstance(evals, dict):
        result_data.setdefault("stats", {})["evals"] = evals = {}
    if not evals:
        evals["tasks"] = {}
        return evals["tasks"]
    if len(evals) == 1:
        key = next(iter(evals))
        if not isinstance(evals[key], dict):
            evals[key] = {}
        return evals[key]
    task_keys = sorted(key for key in evals if str(key).endswith("__tasks"))
    if task_keys:
        key = task_keys[0]
    else:
        key = sorted(evals)[0]
    if not isinstance(evals[key], dict):
        evals[key] = {}
    return evals[key]


async def run_judge(
    job_dirs: list[str],
    output_path: str,
    *,
    write_back: bool = False,
    backend: JudgeBackend,
) -> dict[str, Any]:
    print(f"[LLM Judge] Jobs: {job_dirs}")
    transport = "proxy" if backend.via_proxy else "direct"
    print(
        f"[LLM Judge] API: {backend.api_base}, Judge Model: {backend.model}, "
        f"Transport: {transport}"
    )

    if not backend.model:
        raise RuntimeError(
            "LLM judge model is not configured. Resolve a judge slug via "
            "configs/models/<slug>.yaml (manifest or job config)."
        )
    if not backend.api_base:
        raise RuntimeError(
            "LLM judge API base is not resolved. Set the judge model slug's "
            "backend_url_env in .env or route through the bench proxy."
        )

    trials = find_trials(job_dirs)
    print(f"[LLM Judge] Found {len(trials)} trial(s)")
    tasks_to_judge = [
        data
        for trial in trials
        if (data := load_trial_data(trial["task_name"], trial)) is not None
    ]
    print(f"[LLM Judge] Loaded {len(tasks_to_judge)} trial(s) for judging")

    if any(td["agent_patch"].strip() for td in tasks_to_judge) and not backend.api_key.strip():
        raise RuntimeError(
            "LLM judge API key is missing. Under proxy routing the proxy supplies "
            "the upstream key; for direct runs set the judge model slug's "
            "backend_key_env in .env."
        )

    sem = asyncio.Semaphore(MAX_CONCURRENT)
    results: list[dict[str, Any]] = []
    results_by_trial: dict[str, dict[str, Any]] = {}

    async with httpx.AsyncClient() as client:
        coros = [judge_task(client, sem, backend, trial_data) for trial_data in tasks_to_judge]
        total = len(coros)
        for done, coro in enumerate(asyncio.as_completed(coros), start=1):
            result = await coro
            results.append(result)
            results_by_trial[result["trial_key"]] = result
            status = "OK" if result.get("error") is None else f"ERROR: {str(result['error'])[:60]}"
            mode = result.get("failure_mode", "?")
            judge_score = result.get("llm_judge")
            if isinstance(judge_score, (int, float)):
                score_text = f"{judge_score:.3f}"
            else:
                score_text = "NA"
            print(
                f"  [{done}/{total}] {result['trial']}: "
                f"judge={score_text} mode={mode} ({status})"
            )

    results.sort(key=lambda item: (item["task"], item.get("trial", "")))
    scores = [float(item["llm_judge"]) for item in results if is_scored_result(item)]
    summary = {
        "judge_model": backend.model,
        "judge_schema": "whitebox-v1",
        "api_base": backend.api_base,
        "job_dirs": job_dirs,
        "n_tasks": len({item["task"] for item in results}),
        "n_trials": len(results),
        "n_scored": len(scores),
        "n_errors": len(results) - len(scores),
        "mean_llm_judge": round(sum(scores) / len(scores), 4) if scores else 0,
        "median_llm_judge": round(statistics.median(scores), 4) if scores else 0,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "weights": WEIGHTS,
        "tasks": results,
    }

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\n[LLM Judge] Results saved to {output_path}")
    print(f"[LLM Judge] Mean llm_judge: {summary['mean_llm_judge']:.4f} ({summary['n_scored']}/{summary['n_trials']} trials scored)")

    if write_back:
        print("[LLM Judge] Writing back merged reward.json files")
        touched_jobs: set[Path] = set()
        for trial_data in tasks_to_judge:
            judge_result = results_by_trial.get(trial_data["trial_dir"])
            if not judge_result or not is_scored_result(judge_result):
                continue
            merged_reward = merge_reward(trial_data, judge_result, judge_model=backend.model)
            write_back_reward(trial_data, merged_reward)
            touched_jobs.add(Path(trial_data["trial_dir"]).parent)

        for job_dir in sorted(touched_jobs):
            refresh_job_result(job_dir)
            print(f"[LLM Judge] Refreshed {job_dir / 'result.json'}")

    return summary


def backend_from_resolved_judge(
    resolved: dict[str, Any],
    *,
    proxy_url: str = "",
) -> JudgeBackend:
    """Build a JudgeBackend from a resolved ``llm_judge`` block.

    Accepts the ``llm_judge`` dict from ``resolve_manifest`` / manifest JSON,
    or the output of ``resolve_llm_judge`` from a job config. When
    ``proxy_url`` is set, routes through the bench proxy by model slug.
    """
    if not resolved.get("enabled"):
        raise RuntimeError("llm_judge.enabled is false; nothing to judge.")
    mode = str(resolved.get("mode") or "host_side")
    if mode != "host_side":
        raise RuntimeError(
            f"llm_judge.mode is {mode!r}; the host-side judge only runs "
            "mode: host_side. in_container judges run inside the dataset "
            "verifier and cannot be re-run post-hoc (their in-container "
            "artifacts are gone)."
        )

    model_name = str(resolved.get("model") or "")
    model_slug = str(resolved.get("model_slug") or "")
    params = resolved.get("params") if isinstance(resolved.get("params"), dict) else {}

    if proxy_url:
        # Proxy routing addresses the judge by its route key = the model slug.
        # The backend model id is not a route key, so there is no fallback:
        # an unresolved slug is a hard error.
        slug = model_slug
        if not slug:
            raise RuntimeError("judge model_slug is unresolved.")
        return JudgeBackend(
            api_base=openai_api_base_url(host_reachable_url(proxy_url)),
            api_key=os.environ.get("BENCH_PROXY_API_KEY", "dummy-for-proxy"),
            model=slug,
            params=params,
            via_proxy=True,
        )

    api_base = str(resolved.get("api_base") or "")
    api_key_env = str(resolved.get("api_key_env") or "")
    api_key = os.environ.get(api_key_env, "") if api_key_env else ""
    return JudgeBackend(
        api_base=openai_api_base_url(api_base) if api_base else "",
        api_key=api_key,
        model=model_name,
        params=params,
        via_proxy=False,
    )


def backend_from_manifest_data(manifest: dict[str, Any]) -> JudgeBackend:
    """Build a JudgeBackend from an already-parsed run manifest (slug-driven).

    Reads ``manifest['llm_judge']`` and proxy routing from ``connection.proxy_url``.
    """
    judge = manifest.get("llm_judge") or {}
    connection = manifest.get("connection") or {}
    proxy_url = str(connection.get("proxy_url") or manifest.get("proxy_url") or "")
    return backend_from_resolved_judge(judge, proxy_url=proxy_url)
