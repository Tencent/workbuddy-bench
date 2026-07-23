"""CbcAgent — CodeBuddy Code (cbc) harness for the external bench.

Follows harbor's installed-agent pattern
(``harbor.agents.installed.claude_code.ClaudeCode``). Runs cbc non-interactive
(``cbc -p --output-format stream-json``); the final ``result`` event carries
``usage`` token counts, which ``populate_context_post_run`` parses to fill
``context.n_input_tokens / n_output_tokens / n_cache_tokens / cost_usd`` so the
harbor job rolls up token stats.

The eval model's extra_body (top_p / min_p / chat_template_kwargs, ...) can only
be injected by the bench proxy, so external runs go through it. prepare_job
injects a ``connection`` kwarg ({mode, proxy_url, model_route}) resolved from the
manifest:

- ``local_proxy``: cbc's ``models.json`` points ``url`` at the host proxy
  (``proxy_url``) and uses the route slug as the model id; the proxy rewrites
  ``body["model"]`` to the real backend name and injects extra_body. Sampling
  params cbc supports natively (temperature / maxOutputTokens) are still written
  to ``models.json`` in this mode, but the proxy's request handler is the source
  of truth: its ``body.update()`` overrides them (and adds params cbc cannot
  express, e.g. top_p), so a value written here is harmlessly superseded.
- ``direct``: cbc talks to ``CBC_BASE_URL`` / ``CBC_API_KEY`` with the real
  model name.

All behavioral knobs (max_turns / model_params / settings preset) come from
harness/model config via kwargs — nothing is hardcoded here.
"""

from __future__ import annotations

import base64
import copy
import json
import shlex
from pathlib import Path

from harbor.agents.installed.base import BaseInstalledAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trajectories.agent import Agent
from harbor.models.trajectories.final_metrics import FinalMetrics
from harbor.models.trajectories.metrics import Metrics
from harbor.models.trajectories.step import Step
from harbor.models.trajectories.tool_call import ToolCall
from harbor.models.trajectories.trajectory import Trajectory

from workbuddy_bench.agents._usage import cached_input_tokens
from workbuddy_bench.agents._agent_user import ensure_agent_user
from workbuddy_bench.runner.config_loaders import (
    CBC_AUTOCOMPACT_WINDOW_MAX as _AUTOCOMPACT_WINDOW_MAX,
    CBC_AUTOCOMPACT_WINDOW_MIN as _AUTOCOMPACT_WINDOW_MIN,
    cbc_uses_autocompact_window_env as _supports_autocompact_window_env,
)

# cbc's stream-json output, tee'd here inside the container (under /logs/agent).
_OUTPUT_FILENAME = "cbc-output.txt"

# Auto-compaction mechanism changed across cbc versions, so the context window
# translates to a different cbc config knob depending on the running version:
#
#   route A (< 2.103.4, e.g. 2.93.5): the only trigger is
#       inputTokens/maxInputTokens >= pct; no CODEBUDDY_AUTO_COMPACT_WINDOW env,
#       and compaction is hard-dependent on maxInputTokens (absent → NaN → never
#       fires). So the window must go to maxInputTokens (window×pct); pct is
#       CODEBUDDY_AUTOCOMPACT_PCT_OVERRIDE.
#   route B (>= 2.103.4, e.g. 2.109.x):
#       triggerAt = maxInputTokens ? maxInputTokens*pct : getAutoCompactWindow().
#       The two are mutually exclusive. We want an absolute-token window (no pct),
#       so we omit maxInputTokens and carry it via CODEBUDDY_AUTO_COMPACT_WINDOW.
#
# The route decision + clamp bounds live in config_loaders (imported above), the
# single source of truth shared with resolve_manifest's audit block.


class CbcAgent(BaseInstalledAgent):
    """CodeBuddy Code CLI agent (installed-CLI harness) for harbor."""

    SUPPORTS_ATIF: bool = True  # emits an ATIF trajectory.json + fills token context.

    def __init__(self, logs_dir: Path, *args, **kwargs):
        # Harness knobs (bench-defined kwargs, not cbc env vars).
        # CBC_VERSION: npm version to install (None → use image's / latest).
        self._cbc_version: str | None = kwargs.pop("CBC_VERSION", None)
        # CBC_MAX_TURNS: agent kwarg (not a cbc env) → cbc `--max-turns` flag.
        # cbc has no built-in default; bench config sets it (default 512 here).
        # Pass None to leave it unbounded.
        mt = kwargs.pop("CBC_MAX_TURNS", 512)
        self._max_turns: int | None = int(mt) if mt is not None else None
        # Disabled tools come from the settings preset's permissions.deny (single
        # visible source of truth), not a kwarg. The preset is version-mapped —
        # see presets/settings/<group>.json (each versions/<v>.yaml's settings_file).
        # model.params: only maxOutputTokens/temperature are forwardable to cbc
        # via models.json. Other sampling params (top_p/top_k/min_p/...) and
        # chat_template_kwargs are not injectable here — they require the proxy.
        model_params = kwargs.pop("model_params", None) or {}
        self._max_output_tokens = model_params.get("max_output_tokens")
        self._temperature = model_params.get("temperature")
        # thinking_enabled is a behaviour toggle (not a sampling param). The
        # settings preset defaults alwaysThinkingEnabled=true, so we must
        # distinguish "unset" (inherit preset) from an explicit true/false:
        #   unset          → leave the preset's alwaysThinkingEnabled untouched
        #   explicit true  → force alwaysThinkingEnabled=true
        #   explicit false → force alwaysThinkingEnabled=false (:nothink models)
        # The unset/false distinction matters: without it an explicit false can
        # never override the preset's default true, so :nothink keeps thinking on.
        self._thinking_enabled_set = "thinking_enabled" in model_params
        self._thinking_enabled = bool(model_params.get("thinking_enabled", False))
        # connection: {mode, proxy_url, model_route} injected by prepare_job from
        # the resolved manifest. Decides proxy vs direct addressing in run().
        connection = kwargs.pop("connection", None) or {}
        self._conn_mode = str(connection.get("mode") or "direct")
        self._proxy_url = str(connection.get("proxy_url") or "")
        # Split-mount path where the harness CLI image is mounted (harness
        # config ``mount.path``). install() links its launchers onto PATH from
        # here at run time. Defaults to the codebuddy-code mount target.
        self._mount_path = str(kwargs.pop("mount_path", None) or "/opt/codebuddy-code")
        # Deterministic settings.json preset (parsed dict), resolved from the
        # harness ``settings_file`` by prepare_job. This is the base written into
        # ~/.codebuddy/settings.json; run() overlays dynamic items onto a copy.
        # Fail-fast: the settings preset is version-mapped per versions/<v>.yaml
        # (its settings_file field) and carries the tool deny list. A missing/empty
        # preset means the version YAML forgot its `settings_file` mapping — refuse
        # rather than silently run with no deny list (e.g. WebSearch re-enabled).
        sp = kwargs.pop("settings_preset", None)
        if not (isinstance(sp, dict) and sp):
            raise ValueError(
                "cbc settings_preset is missing/empty — the harness version config "
                "must set `harness.settings_file` to its mapped preset "
                "(each versions/<v>.yaml; see CONFIG.md for the version-to-preset "
                "mapping). "
                "Running without a deny list would silently re-enable disabled tools."
            )
        self._settings_preset: dict = dict(sp)
        # models.json preset: static model fields (vendor / supports* /
        # maxOutputTokens). run() overlays the dynamic fields (id/name/url/apiKey
        # and maxInputTokens = context window).
        mp = kwargs.pop("models_preset", None)
        self._models_preset: dict = dict(mp) if isinstance(mp, dict) else {}
        # Context window + compaction percent (harness-agnostic, from manifest).
        # run() translates these to cbc's native config: the WINDOW becomes
        # models.json maxInputTokens (cbc's canonical compaction source), and the
        # PERCENT becomes CODEBUDDY_AUTOCOMPACT_PCT_OVERRIDE env (no models/
        # settings field expresses it). None → cbc's own default.
        cw = kwargs.pop("context_window", None)
        self._context_window: int | None = int(cw) if cw is not None else None
        pct = kwargs.pop("context_compact_pct", None)
        self._compact_pct: int | None = int(pct) if pct is not None else None
        # instance_id is carried only so Harbor records it in config.json (offline
        # key for tying this trial to proxy logs); the agent does not use it.
        kwargs.pop("instance_id", None)
        # Per-trial id (``trial_name`` = ``{task}__{uuid}``, unique per trial),
        # prefixed onto the bearer token under local_proxy so the proxy can
        # attribute each request log line to one trial (instance_id is run-level).
        # The trial_name is not passed to the agent as a kwarg; it is only
        # recoverable from logs_dir, whose parent dir is named after it.
        self._session_id = self._trial_id_from_logs_dir(logs_dir)
        super().__init__(logs_dir, *args, version=self._cbc_version, **kwargs)

    @staticmethod
    def _trial_id_from_logs_dir(logs_dir: Path | None) -> str:
        # logs_dir is ``.../<trial_name>/agent``; the parent's name is the
        # per-trial id. Empty when the path is shaped differently, in which case
        # the bare route token is sent instead.
        try:
            p = Path(logs_dir)
        except TypeError:
            return ""
        return p.parent.name if p.name == "agent" else ""

    @staticmethod
    def name() -> str:
        return "cbc"

    def get_version_command(self) -> str | None:
        return "cbc --version"

    # ── install ──────────────────────────────────────────────────
    async def install(self, environment: BaseEnvironment) -> None:
        # System deps (root). procps mirrors claude-code's tree-kill needs.
        await self.exec_as_root(
            environment,
            command=(
                "command -v curl >/dev/null 2>&1 && command -v ps >/dev/null 2>&1 && exit 0; "
                "if command -v apk &> /dev/null; then"
                "  apk add --no-cache curl bash nodejs npm procps;"
                " elif command -v apt-get &> /dev/null; then"
                "  apt-get update && apt-get install -y curl procps;"
                " elif command -v yum &> /dev/null; then"
                "  yum install -y curl procps-ng;"
                " fi;"
                " true"
            ),
            env={"DEBIAN_FRONTEND": "noninteractive"},
        )
        # Wire the split-mount launcher onto PATH at run time, then install only
        # if cbc is still absent. The task image is harness-free (no baked
        # symlinks) and harbor overrides the container CMD with `sleep infinity`,
        # so a Dockerfile ENTRYPOINT never runs — this install() exec is the only
        # post-start hook. We symlink whichever launchers actually exist under the
        # mount (no dead links), then fall back to npm only on a bare image. When
        # a version is pinned the package spec includes it; --force avoids EEXIST.
        ver = self._cbc_version or ""
        pkg = "@tencent-ai/codebuddy-code" + (f"@{ver}" if ver else "")
        mount_bin = f"{self._mount_path.rstrip('/')}/bin"
        await self.exec_as_root(
            environment,
            command=(
                'export PATH="/usr/local/bin:$PATH"; '
                # Link the mounted launcher(s) onto PATH (idempotent; skip missing).
                f'for b in cbc codebuddy; do '
                f'  [ -x "{mount_bin}/$b" ] && ln -sf "{mount_bin}/$b" "/usr/local/bin/$b"; '
                f'done; true; '
                "if command -v cbc >/dev/null 2>&1 || command -v codebuddy >/dev/null 2>&1; then "
                "echo 'cbc present (split-mount/image), skipping npm install'; "
                f"else npm install -g --force {pkg}; fi && "
                "(cbc --version || codebuddy --version)"
            ),
        )
        # If a non-root agent_user is configured, materialize it at runtime (root)
        # so no task image / dataset edit is needed. cbc runs fine as root, but the
        # bench may force non-root for cross-harness consistency (cbc and cc in the
        # same env removes a confound). No-op when running as root.
        await ensure_agent_user(self, environment)

    # ── run ──────────────────────────────────────────────────────
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        instruction = self.render_instruction(instruction)
        escaped_instruction = shlex.quote(instruction)

        model = self.model_name  # route slug under local_proxy; real id under direct

        if self._conn_mode == "local_proxy":
            # The bench proxy speaks OpenAI on /v1/chat/completions, keys routes
            # by slug, and rewrites body["model"] + injects extra_body. cbc only
            # needs to address the route; the api key is a dummy for the hop.
            url = self._ensure_chat_completions_url(self._proxy_url)
            api_key = self._get_env("CBC_API_KEY") or "proxy"
            # Prefix the per-trial session id onto the token as ``{trial}::{route}``.
            # The proxy splits on ``::``: the trial half is logged for per-trial
            # request attribution, the route half selects the route (its strict
            # slug match is unaffected). Skip when there is no session id.
            if self._session_id:
                api_key = f"{self._session_id}::{model}"
        else:
            # Direct: cbc talks to the model backend named by CBC_BASE_URL.
            api_key = self._get_env("CBC_API_KEY") or ""
            base_url = self._get_env("CBC_BASE_URL") or ""
            url = self._ensure_chat_completions_url(base_url)

        # cbc reads its model/endpoint from ~/.codebuddy/models.json. Start from
        # the static preset (vendor / supports* / maxOutputTokens) and overlay the
        # per-run dynamic fields (id/name/url/apiKey).
        models_entry: dict = dict(self._models_preset)
        models_entry.update({
            "id": model,
            "name": model,
            "apiKey": api_key,
            "url": url,
        })
        # Context WINDOW → compaction config is version-dependent (see
        # _supports_autocompact_window_env). Route A (legacy < 2.103.4) carries the
        # window via models.json maxInputTokens (window×pct). Route B (>= 2.103.4)
        # carries it via CODEBUDDY_AUTO_COMPACT_WINDOW env (absolute, no pct) and
        # deliberately omits maxInputTokens so the env is honoured — see run()'s
        # env_prefix. Only write maxInputTokens here on route A.
        self._use_autocompact_window_env = _supports_autocompact_window_env(
            self._cbc_version
        )
        if self._context_window is not None and not self._use_autocompact_window_env:
            models_entry["maxInputTokens"] = int(self._context_window)
        # Sampling/output params cbc supports natively (temperature, maxOutputTokens)
        # are written to models.json in both modes. Under local_proxy the proxy is
        # the complete source of truth — it injects the full param set (incl. ones
        # cbc's models.json cannot express, e.g. top_p) and its body.update()
        # overrides whatever cbc sends, so a value here is harmlessly superseded.
        # Under direct (no proxy) cbc must carry them itself; params cbc cannot
        # express are simply unavailable in direct mode (accepted tradeoff).
        if self._max_output_tokens is not None:
            models_entry["maxOutputTokens"] = self._max_output_tokens
        if self._temperature is not None:
            models_entry["temperature"] = self._temperature
        # Drop any ${VAR} placeholders the preset declared but we did not fill at
        # run time (e.g. ${MAX_OUTPUT_TOKENS} when no max_output_tokens is set), so
        # cbc never receives an unresolved literal.
        models_entry = {
            k: v for k, v in models_entry.items()
            if not (isinstance(v, str) and v.startswith("${") and v.endswith("}"))
        }
        models_json = {"models": [models_entry], "availableModels": [model]}

        # settings.json: start from the deterministic preset file (config layer),
        # overlay dynamic items. An explicit thinking_enabled (from model params)
        # wins over the preset's alwaysThinkingEnabled so the model config
        # controls thinking; when unset the preset default is left intact.
        settings_json: dict = copy.deepcopy(self._settings_preset)
        if self._thinking_enabled_set:
            settings_json["alwaysThinkingEnabled"] = self._thinking_enabled
        # Disabled tools (permissions.deny) come straight from the preset — no
        # runtime injection, so the preset file is the single source of truth.

        models_b64 = base64.b64encode(json.dumps(models_json).encode()).decode()
        settings_b64 = base64.b64encode(json.dumps(settings_json).encode()).decode()
        # Some task images bake an ENV HOME pointing at a package-specific user's
        # home (e.g. radare2:6.1.6 sets HOME=/home/r2). A non-root agent_user (dev)
        # inherits that HOME via docker exec but cannot write there, so `~/.codebuddy`
        # fails. Force HOME to the running user's real home (passwd field 6) so cbc's
        # config lands in a writable dir regardless of the image's baked HOME.
        home_fix = 'export HOME="$(getent passwd "$(id -u)" | cut -d: -f6)"; '
        setup_cmd = (
            home_fix
            + "mkdir -p ~/.codebuddy && "
            f"echo {models_b64} | base64 -d > ~/.codebuddy/models.json && "
            f"echo {settings_b64} | base64 -d > ~/.codebuddy/settings.json"
        )
        await self.exec_as_agent(environment, command=setup_cmd)

        # Non-interactive, parseable stream (mirrors claude-code's --print path).
        # -y bypasses permission prompts (sandbox). stream-json's final `result`
        # event carries usage{input_tokens,output_tokens,...}.
        output_path = f"/logs/agent/{_OUTPUT_FILENAME}"
        # Disabled tools are enforced via settings.json permissions.deny (above),
        # not a CLI flag. -y bypasses permission prompts but deny rules still hold.
        flags = ["-p", "--output-format", "stream-json", "-y",
                 "--model", shlex.quote(model)]
        if self._max_turns is not None:
            flags += ["--max-turns", str(self._max_turns)]
        # Context WINDOW translation depends on the cbc compaction mechanism:
        #   Route B (>= 2.103.4): absolute-token window via CODEBUDDY_AUTO_COMPACT_WINDOW.
        #     maxInputTokens was deliberately omitted (above), so cbc falls through
        #     to getAutoCompactWindow() = clamp(env, 100k, 1M). No pct discount —
        #     CODEBUDDY_AUTOCOMPACT_PCT_OVERRIDE is inert on this path, so we skip it.
        #   Route A (< 2.103.4): window already carried via maxInputTokens (×pct);
        #     pct is the only knob without a models/settings field → env override.
        # HOME fix (see setup_cmd): cbc reads ~/.codebuddy at run time too, so the
        # same baked-HOME correction must lead the run command's env prefix.
        env_prefix = home_fix
        if self._use_autocompact_window_env:
            if self._context_window is not None:
                win = int(self._context_window)
                if win < _AUTOCOMPACT_WINDOW_MIN or win > _AUTOCOMPACT_WINDOW_MAX:
                    self.logger.warning(
                        "context_window %d is outside cbc's auto-compact clamp "
                        "[%d, %d]; cbc will drag it into range.",
                        win, _AUTOCOMPACT_WINDOW_MIN, _AUTOCOMPACT_WINDOW_MAX,
                    )
                env_prefix += f"export CODEBUDDY_AUTO_COMPACT_WINDOW={win}; "
        elif self._compact_pct is not None:
            env_prefix += f"export CODEBUDDY_AUTOCOMPACT_PCT_OVERRIDE={int(self._compact_pct)}; "
        # A hard non-zero cbc exit propagates through the pipefail-wrapped exec
        # and raises. But cbc more often exhausts its retries and terminates
        # cleanly, recording the failure only in the result event's
        # is_error/errors — which _build_trajectory parses so Harbor can
        # attribute it.
        run_cmd = (
            f"{env_prefix}cbc {' '.join(flags)} -- {escaped_instruction} "
            f"2>&1 </dev/null | tee {output_path}"
        )
        await self.exec_as_agent(environment, command=run_cmd)

    # ── post-run: parse stream-json → ATIF trajectory + token context ────
    def populate_context_post_run(self, context: AgentContext) -> None:
        events = self._read_events()
        if events is None:
            return
        trajectory = self._build_trajectory(events)
        if trajectory:
            traj_path = self.logs_dir / "trajectory.json"
            try:
                with open(traj_path, "w", encoding="utf-8") as fh:
                    json.dump(trajectory.to_json_dict(), fh, indent=2, ensure_ascii=False)
            except (OSError, AttributeError) as exc:
                self.logger.debug("Failed writing trajectory.json: %s", exc)
            # Fill context token/cost from the trajectory's final metrics.
            fm = trajectory.final_metrics
            if fm:
                context.n_input_tokens = fm.total_prompt_tokens or 0
                context.n_output_tokens = fm.total_completion_tokens or 0
                context.n_cache_tokens = fm.total_cached_tokens or 0
                if fm.total_cost_usd is not None:
                    context.cost_usd = fm.total_cost_usd

    def _read_events(self) -> list[dict] | None:
        out = self.logs_dir / _OUTPUT_FILENAME
        if not out.is_file():
            self.logger.warning(
                "cbc output not found: %s — token/cost context will be empty "
                "(check the /logs/agent mount)",
                out,
            )
            return None
        events: list[dict] = []
        try:
            for line in out.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError as exc:
            self.logger.debug("Failed reading cbc output: %s", exc)
            return None
        return events

    def _build_trajectory(self, events: list[dict]) -> Trajectory | None:
        """Map cbc stream-json events to an ATIF Trajectory.

        cbc events mirror claude-code's: a `system/init`, one or more
        `assistant` messages (content blocks: thinking/text/tool_use, each with
        usage), and a final `result` (authoritative usage + cost).
        """
        steps: list[Step] = []
        session_id: str | None = None
        result_usage: dict | None = None
        total_cost: float | None = None
        # Diagnostic fields off the final `result` event. When cbc exhausts its
        # own retries (e.g. upstream model 500), it terminates with is_error=true
        # / subtype=error_during_execution / errors=[...] — the only signal that
        # distinguishes a service failure from the model just answering wrong.
        # Surfaced into the trajectory's extra/notes for Harbor attribution.
        result_is_error: bool | None = None
        result_subtype: str | None = None
        result_errors: list | None = None
        result_num_turns: int | None = None
        # Count LLM inferences, not raw events: cbc splits one message.id into
        # several `assistant` events, and some carry all-zero usage. A step's
        # llm_call_count is 1 when it has real token usage, else 0 (a
        # deterministic dispatch under ATIF v1.7 — metrics must be absent there).
        llm_calls = 0
        step_id = 1  # ATIF Step.step_id is 1-indexed

        for ev in events:
            etype = ev.get("type")
            session_id = session_id or ev.get("session_id")
            if etype == "result":
                if isinstance(ev.get("usage"), dict):
                    result_usage = ev["usage"]
                if isinstance(ev.get("total_cost_usd"), (int, float)):
                    total_cost = float(ev["total_cost_usd"])
                if isinstance(ev.get("is_error"), bool):
                    result_is_error = ev["is_error"]
                if isinstance(ev.get("subtype"), str):
                    result_subtype = ev["subtype"]
                if isinstance(ev.get("errors"), list):
                    result_errors = ev["errors"]
                if isinstance(ev.get("num_turns"), int):
                    result_num_turns = ev["num_turns"]
                continue
            if etype != "assistant":
                continue
            msg = ev.get("message") or {}
            content = msg.get("content") or []
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            tool_calls: list[ToolCall] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt == "text":
                    text_parts.append(str(block.get("text", "")))
                elif bt == "thinking":
                    reasoning_parts.append(str(block.get("thinking", "")))
                elif bt == "tool_use":
                    tool_calls.append(ToolCall(
                        tool_call_id=str(block.get("id", "")),
                        function_name=str(block.get("name", "")),
                        arguments=block.get("input") if isinstance(block.get("input"), dict) else {},
                    ))
            usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
            has_tokens = bool(
                usage and (usage.get("input_tokens") or usage.get("output_tokens"))
            )
            # llm_call_count=0 forbids metrics/reasoning_content (ATIF v1.7), so
            # only attach them on token-bearing (real-inference) events.
            if has_tokens:
                llm_calls += 1
                metrics = Metrics(
                    prompt_tokens=usage.get("input_tokens"),
                    completion_tokens=usage.get("output_tokens"),
                )
                reasoning = "\n".join(reasoning_parts) or None
                llm_call_count = 1
            else:
                metrics = None
                reasoning = None
                llm_call_count = 0
            steps.append(Step(
                step_id=step_id,
                source="agent",
                model_name=msg.get("model") or self.model_name,
                message="\n".join(text_parts),
                reasoning_content=reasoning,
                tool_calls=tool_calls or None,
                metrics=metrics,
                llm_call_count=llm_call_count,
            ))
            step_id += 1

        # Diagnostic extras: raw event count vs. real LLM calls vs. cbc's own
        # turn count all differ, so record each explicitly rather than conflating
        # them into total_steps.
        extra: dict = {
            "raw_assistant_events": len(steps),
            "llm_calls": llm_calls,
        }
        if result_num_turns is not None:
            extra["cbc_num_turns"] = result_num_turns
        if result_is_error is not None:
            extra["cbc_is_error"] = result_is_error
        if result_subtype is not None:
            extra["cbc_subtype"] = result_subtype
        if result_errors:
            extra["cbc_errors"] = result_errors

        final_metrics = None
        if result_usage:
            final_metrics = FinalMetrics(
                total_prompt_tokens=int(result_usage.get("input_tokens") or 0),
                total_completion_tokens=int(result_usage.get("output_tokens") or 0),
                total_cached_tokens=cached_input_tokens(result_usage),
                total_cost_usd=total_cost,
                # total_steps counts raw ATIF Steps (one per assistant event).
                # This differs from LLM calls and from cbc's num_turns, so the
                # breakdown lives in extra and is called out in notes below.
                total_steps=len(steps),
                extra=extra,
            )

        notes = (
            "total_steps counts raw cbc `assistant` events (extra.raw_assistant_events); "
            "extra.llm_calls counts token-bearing inferences; extra.cbc_num_turns is cbc's "
            "own turn count. cbc splits one message into multiple assistant events, so the "
            "three differ by design."
        )
        if result_is_error:
            notes += (
                " RUN FAILED: cbc reported is_error=true "
                f"(subtype={result_subtype}); see extra.cbc_errors."
            )

        return Trajectory(
            session_id=session_id,
            agent=Agent(name="cbc", version=self._version or "unknown",
                        model_name=self.model_name),
            steps=steps,
            final_metrics=final_metrics,
            notes=notes,
        )

    # ── helpers ──────────────────────────────────────────────────
    @staticmethod
    def _ensure_chat_completions_url(base_url: str) -> str:
        """Normalize a base URL to the OpenAI chat-completions endpoint cbc expects.

        cbc posts OpenAI Chat Completions to this exact URL. The bench proxy only
        treats ``/v1/chat/completions`` as an OpenAI client (any other path falls
        through to the Anthropic catch-all). So the URL must carry the ``/v1``
        segment, or the proxy misclassifies the request as Anthropic and 400s.
        """
        u = (base_url or "").rstrip("/")
        if not u:
            return u
        if u.endswith("/chat/completions"):
            return u
        # Ensure a /v1 API-version segment before appending /chat/completions.
        if not u.endswith("/v1") and "/v1/" not in u + "/":
            u = u + "/v1"
        return u + "/chat/completions"
