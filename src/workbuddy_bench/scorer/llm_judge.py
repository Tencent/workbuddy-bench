#!/usr/bin/env python3
"""
White-box LLM judge for completed WorkBuddy-Bench trials (host-side post-judge).

For every FAILED verifier test the judge asks the model whether the failure is
a real defect or the test being too strict (an unstated contract detail). Tests
judged too strict are recovered:

    score = tpr + (recovered / total_tests)

The passed portion stays anchored on the objective test pass rate (tpr); tpr=1.0
short-circuits to a full score without any LLM call.
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
MAX_FAILURE_CHARS = int(os.environ.get("LLM_JUDGE_MAX_FAILURE_CHARS", "12000"))
PARSE_RETRIES = int(os.environ.get("LLM_JUDGE_PARSE_RETRIES", "1"))

PER_TEST_JUDGE_PROMPT_TEMPLATE = """\
You are a strict but fair white-box reviewer for WorkBuddy-Bench.

One verifier test FAILED. Your ONLY job is to decide whether that failure is a
*real implementation defect* or merely the test being *too strict* — i.e. the
agent implemented the requested behavior correctly but under a different
contract (function/parameter name, file path, output format, entrypoint) that
the user instruction never specified.

The instruction is deliberately ambiguous and gives the agent implementation
freedom. If the agent's implementation would satisfy the user's request and the
test only fails because of an unstated contract detail, the test is TOO STRICT
and should be counted as recovered. If the agent's implementation is genuinely
wrong, missing functionality, or behaves incorrectly, the failure is a REAL
DEFECT and must not be recovered.

Use only the provided evidence — there is NO agent trajectory; judge purely
from the agent's final patch against this one failed test.
- user instruction
- gold patch (reference solution)
- agent patch (what the agent actually changed)
- the single failed test: name, failure message, failure details
- test_pass_rate (fraction of tests that passed)

Output:
- is_test_too_strict: boolean — true if this failure is a contract/interface
  mismatch (recoverable), false if it is a real defect. When in doubt, false.
- would_adapter_likely_fix: boolean — whether a thin adapter (renaming /
  reshaping the agent's output to the test-implied contract) would make THIS
  test pass.
- reason: short explanation.

Return JSON only, with exactly this shape:
{{
  "is_test_too_strict": false,
  "would_adapter_likely_fix": false,
  "reason": "brief explanation"
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

    return None


def parse_or_recover_json_response(text: str | None) -> tuple[dict[str, Any] | None, str]:
    parsed = parse_json_response(text)
    if parsed is not None:
        return parsed, "parsed"

    recovered = recover_json_response(text)
    if recovered is not None:
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

    test_output = _read_text(verifier_dir / "test_output.txt", limit=MAX_FAILURE_CHARS)
    test_stdout = _read_text(verifier_dir / "test-stdout.txt", limit=MAX_FAILURE_CHARS)
    reward = _safe_json_load(verifier_dir / "reward.json")
    score_payload = _safe_json_load(verifier_dir / "score.json")
    verifier_score = _first_reward_score(score_payload, reward)
    verifier_status = score_payload.get("test_status") or reward.get("test_status")
    task_toml = _safe_toml_load(task_root / "task.toml")

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
    }

    truncation = {
        "instruction": instruction.truncated,
        "gold_patch": gold_patch.truncated,
        "agent_patch": agent_patch.truncated,
        "test_output": test_output.truncated or test_stdout.truncated,
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


def is_scored_result(result: dict[str, Any]) -> bool:
    return result.get("error") is None and isinstance(result.get("llm_judge"), (int, float))


def _per_test_failed_tests(trial_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Enumerate the failed tests for per-test judging.

    Prefers the JUnit failure summary (name/message/details per test). If the
    summary is empty but the test counts say some tests failed, synthesize one
    entry per failed test from the raw test output.
    """
    ctx = trial_data.get("context") or {}
    test_result = ctx.get("test_result") or {}
    failures = test_result.get("failure_summary") or []
    if isinstance(failures, list) and failures:
        out = []
        for f in failures:
            if not isinstance(f, dict):
                continue
            out.append({
                "classname": str(f.get("classname") or ""),
                "name": str(f.get("name") or ""),
                "message": str(f.get("message") or "")[:500],
                "details": str(f.get("details") or "")[:1200],
            })
        if out:
            return out

    # Fallback: synthesize from counts + raw output.
    passed = test_result.get("tests_passed")
    total = test_result.get("tests_total")
    try:
        passed_f, total_f = int(passed), int(total)
    except (TypeError, ValueError):
        return []
    n_failed = max(total_f - passed_f, 0)
    if n_failed <= 0:
        return []
    test_output = str(test_result.get("test_output") or "")
    return [
        {
            "classname": "",
            "name": f"failed_test_{i}",
            "message": "(test failure details unavailable in JUnit summary)",
            "details": test_output[:1500],
        }
        for i in range(min(n_failed, 10))
    ]


def _build_per_test_prompt(
    context: dict[str, Any],
    failed_test: dict[str, Any],
) -> str:
    """Build a single-failed-test prompt for per-test judging."""
    test_result = context.get("test_result") or {}
    ctx = dict(context)
    ctx["failed_test"] = failed_test
    # Only aggregate counts; the full failure list is never sent.
    ctx["test_result"] = {
        key: test_result.get(key) for key in ("test_pass_rate", "tests_passed", "tests_total")
    }
    context_json = json.dumps(ctx, ensure_ascii=False, indent=2)
    prompt = PER_TEST_JUDGE_PROMPT_TEMPLATE.format(context_json=context_json)
    return prompt[:MAX_PROMPT_CHARS]


def _judge_result(
    base_result: dict[str, Any],
    *,
    score: float | None,
    tpr: float,
    failure_mode: str,
    parse_status: str,
    recovery_rate: float = 0.0,
    judge_attempts: int = 0,
    **extra: Any,
) -> dict[str, Any]:
    return {
        **base_result,
        "scores": {"recovery_rate": recovery_rate, "test_pass_rate": tpr, "score_overall": score},
        "recovery_rate": recovery_rate,
        "score_overall": score,
        "llm_judge": score,
        "failure_mode": failure_mode,
        "would_adapter_likely_fix": False,
        "evidence": [],
        "rationale": "",
        "parse_status": parse_status,
        "judge_attempts": judge_attempts,
        "cap_reasons": [],
        **extra,
    }


async def _judge_failed_tests(
    client: httpx.AsyncClient,
    backend: JudgeBackend,
    trial_data: dict[str, Any],
    base_result: dict[str, Any],
    tpr: float,
) -> dict[str, Any]:
    """Judge each failed test independently; add back the too-strict ones.

    Score = tpr + (n_recovered / total_tests).
    """
    failed_tests = _per_test_failed_tests(trial_data)
    if not failed_tests:
        return _judge_result(
            base_result,
            score=tpr,
            tpr=tpr,
            failure_mode="judge_uncertain",
            parse_status="no_failure_detail",
            per_test={"n_failed": 0, "n_recovered": 0, "verdicts": []},
        )

    test_result = (trial_data.get("context") or {}).get("test_result") or {}
    try:
        total_tests = int(test_result.get("tests_total") or 0)
    except (TypeError, ValueError):
        total_tests = 0
    if total_tests <= 0:
        # No test counts: estimate the total from the failed share.
        total_tests = round(len(failed_tests) / max(1.0 - tpr, 0.01))

    async def _judge_one(failed: dict[str, Any]) -> dict[str, Any]:
        prompt = _build_per_test_prompt(trial_data["context"], failed)
        verdict: dict[str, Any] = {"test": failed.get("name") or "", "recovered": False, "error": None}
        try:
            parse_status = "failed"
            for _ in range(PARSE_RETRIES + 1):
                parsed, parse_status = parse_or_recover_json_response(await call_llm(client, backend, prompt))
                if parsed is not None:
                    verdict["recovered"] = parsed.get("is_test_too_strict") is True
                    verdict["adapter_likely_fix"] = parsed.get("would_adapter_likely_fix") is True
                    verdict["reason"] = str(parsed.get("reason") or "")[:500]
                    verdict["parse_status"] = parse_status
                    return verdict
            verdict["error"] = f"judge_parse_failed ({parse_status})"
        except Exception as exc:  # noqa: BLE001
            verdict["error"] = str(exc)[:300]
        return verdict

    verdicts = await asyncio.gather(*(_judge_one(f) for f in failed_tests))
    judged = [v for v in verdicts if not v["error"]]
    n_recovered = sum(1 for v in judged if v["recovered"])

    score = min(tpr + n_recovered / total_tests, 1.0)
    cap_reasons: list[str] = []
    if tpr <= 0.0 and score > 0.85:
        score = 0.85
        cap_reasons.append("zero_tpr_recovery_capped")

    if judged and n_recovered == len(judged):
        failure_mode = "fully_correct"
    elif n_recovered:
        failure_mode = "interface_mismatch_only"
    else:
        failure_mode = "partial_functionality"

    return _judge_result(
        base_result,
        score=round(score, 4),
        tpr=tpr,
        failure_mode=failure_mode,
        parse_status="per_test",
        recovery_rate=round(n_recovered / max(len(judged), 1), 4),
        judge_attempts=len(verdicts),
        would_adapter_likely_fix=any(v.get("adapter_likely_fix") for v in verdicts),
        evidence=[f"{v['test']}: {'recovered' if v['recovered'] else 'defect'}" for v in verdicts[:8]],
        rationale=f"{n_recovered}/{len(judged)} failed tests judged as too strict (recovered)",
        cap_reasons=cap_reasons,
        per_test={
            "n_failed": len(failed_tests),
            "n_failed_judged": len(judged),
            "n_recovered": n_recovered,
            "total_tests": total_tests,
            "verdicts": verdicts,
        },
    )


async def judge_task(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    backend: JudgeBackend,
    trial_data: dict[str, Any],
) -> dict[str, Any]:
    async with sem:
        task_name = trial_data["task_name"]
        trial_name = trial_data.get("trial_name") or task_name
        base_result = {
            "task": task_name,
            "trial": trial_name,
            "trial_key": trial_data.get("trial_dir") or trial_name,
            "attempt_id": trial_data.get("attempt_id", ""),
            "judge_schema": "per_test",
            "context": {
                "task_root": trial_data["task_root"],
                "truncation": trial_data["context_truncation"],
            },
            "test_pass_rate": trial_data["test_pass_rate"],
            "error": None,
        }
        tpr = _clamp_score(trial_data["test_pass_rate"])

        if not trial_data["agent_patch"].strip():
            return _judge_result(
                base_result,
                score=0.0,
                tpr=tpr,
                failure_mode="missing_artifact",
                parse_status="skipped_empty_patch",
            )

        # All tests pass: nothing to recover, skip the LLM.
        if tpr >= 1.0:
            return _judge_result(
                base_result,
                score=1.0,
                tpr=1.0,
                failure_mode="fully_correct",
                parse_status="skipped_full_pass",
                evidence=["all verifier tests pass (tpr=1.0)"],
                rationale="All verifier tests pass; full score awarded without LLM judging.",
            )

        try:
            return await _judge_failed_tests(client, backend, trial_data, base_result, tpr)
        except Exception as exc:  # noqa: BLE001
            return _judge_result(
                base_result,
                score=None,
                tpr=tpr,
                failure_mode="judge_uncertain",
                parse_status="api_error",
                error=f"per_test_judge_failed: {exc}",
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
    merged["failure_mode"] = judge_result.get("failure_mode")
    merged["would_adapter_likely_fix"] = judge_result.get("would_adapter_likely_fix")
    merged["judge_parse_status"] = judge_result.get("parse_status")
    merged["judge_cap_reasons"] = judge_result.get("cap_reasons", [])
    merged["judge_attempts"] = judge_result.get("judge_attempts")
    merged["judge_model"] = judge_model
    merged["judge_schema"] = judge_result.get("judge_schema", "per_test")
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
        "judge_schema": "per_test",
        "api_base": backend.api_base,
        "job_dirs": job_dirs,
        "n_tasks": len({item["task"] for item in results}),
        "n_trials": len(results),
        "n_scored": len(scores),
        "n_errors": len(results) - len(scores),
        "mean_llm_judge": round(sum(scores) / len(scores), 4) if scores else 0,
        "median_llm_judge": round(statistics.median(scores), 4) if scores else 0,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
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
