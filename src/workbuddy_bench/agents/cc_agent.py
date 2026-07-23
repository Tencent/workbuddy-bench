"""CcAgent — Claude Code (claude CLI) harness for the external bench.

A ``BaseInstalledAgent`` driving the Claude Code CLI, structured like
``CbcAgent`` (install → run → parse stream-json → fill token context) so the
bench's split-mount / proxy conventions apply uniformly.

Differences from CbcAgent (cbc speaks OpenAI, claude speaks Anthropic):

- Addressing is via env, not models.json. The claude CLI reads its endpoint and
  model from ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_MODEL``,
  so ``run()`` builds these into the exec env instead of writing a
  ``~/.codebuddy/models.json``. There is no models preset.
  - ``local_proxy``: ``ANTHROPIC_BASE_URL`` → the host proxy (``proxy_url``),
    ``ANTHROPIC_MODEL`` → the route slug; the proxy converts Anthropic→OpenAI
    (a2o), rewrites ``model`` to the real backend name and injects extra_body.
    The api key is a dummy for the hop (the proxy holds the real backend key).
  - ``direct``: ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_API_KEY`` come from the
    harness env, ``ANTHROPIC_MODEL`` is the real model id.
- Tool deny list is via settings.json (``permissions.deny``) written into
  ``$CLAUDE_CONFIG_DIR/settings.json`` from the version-mapped settings preset.
- Split-mount install: symlink the mounted ``claude`` launcher onto PATH, npm
  fallback only on a bare image.

All behavioural knobs (max_turns / model_params / settings preset) come from
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

# claude CLI's stream-json output, tee'd here inside the container (under /logs/agent).
_OUTPUT_FILENAME = "cc-output.txt"


class CcAgent(BaseInstalledAgent):
    """Claude Code CLI agent (installed-CLI harness) for harbor."""

    SUPPORTS_ATIF: bool = True  # emits an ATIF trajectory.json + fills token context.

    def __init__(self, logs_dir: Path, *args, **kwargs):
        # Harness knobs (bench-defined kwargs, not claude env vars).
        # CLAUDE_CODE_VERSION: npm version to install (None → use image's / latest).
        self._cc_version: str | None = kwargs.pop("CLAUDE_CODE_VERSION", None)
        # CLAUDE_CODE_MAX_TURNS → claude `--max-turns` flag (limits agentic turns
        # in --print mode; errors when the limit is hit). Bench config sets it
        # (default 256). None → unbounded. (`--max-turns` is hidden from --help on
        # 2.1.x but functional.)
        mt = kwargs.pop("CLAUDE_CODE_MAX_TURNS", 256)
        self._max_turns: int | None = int(mt) if mt is not None else None
        # model.params: claude takes max_output_tokens via CLAUDE_CODE_MAX_OUTPUT_TOKENS
        # env. Other sampling params (top_p/top_k/min_p/...) and chat_template_kwargs
        # are not expressible to the claude CLI — they require the proxy.
        # temperature has no claude CLI/env knob; kept only for parity/record.
        model_params = kwargs.pop("model_params", None) or {}
        self._max_output_tokens = model_params.get("max_output_tokens")
        self._temperature = model_params.get("temperature")  # unused by claude CLI
        # thinking_enabled: kept for parity with cbc, but not wired to a CLI flag.
        # claude's thinking depth is the `--effort` level, which the bench controls
        # uniformly via CLAUDE_CODE_EFFORT_LEVEL env in the harness preset (default
        # "max"), not per model param. Stored only for record.
        self._thinking_enabled = bool(model_params.get("thinking_enabled", False))
        # connection: {mode, proxy_url, model_route} injected by prepare_job from
        # the resolved manifest. Decides proxy vs direct addressing in run().
        connection = kwargs.pop("connection", None) or {}
        self._conn_mode = str(connection.get("mode") or "direct")
        self._proxy_url = str(connection.get("proxy_url") or "")
        # Split-mount path where the harness CLI image is mounted (harness
        # config ``mount.path``). install() links the claude launcher onto PATH
        # from here at run time. Defaults to the claude-code mount target.
        self._mount_path = str(kwargs.pop("mount_path", None) or "/opt/claude-code")
        # Deterministic settings.json preset (parsed dict), resolved from the
        # harness ``settings_file`` by prepare_job. Written into
        # $CLAUDE_CONFIG_DIR/settings.json; carries the tool deny list.
        # Fail-fast: the settings preset is version-mapped per versions/<v>.yaml
        # (its settings_file field). A missing/empty preset means the version YAML
        # forgot its `settings_file` mapping — refuse rather than silently run with
        # no deny list (e.g. WebSearch re-enabled).
        sp = kwargs.pop("settings_preset", None)
        if not (isinstance(sp, dict) and sp):
            raise ValueError(
                "claude-code settings_preset is missing/empty — the harness version "
                "config must set `harness.settings_file` to its mapped preset "
                "(each versions/<v>.yaml). Running without a deny list would silently "
                "re-enable disabled tools."
            )
        self._settings_preset: dict = dict(sp)
        # models_preset: not used by claude (addressing is via env). Accept and drop
        # so a stray harness models_file never reaches the agent.
        kwargs.pop("models_preset", None)
        # Context window + compaction percent (harness-agnostic, from manifest).
        # run() translates these to claude's native auto-compaction env:
        #   window  → CLAUDE_CODE_AUTO_COMPACT_WINDOW (absolute tokens)
        #   percent → CLAUDE_AUTOCOMPACT_PCT_OVERRIDE
        # (the claude analogues of cbc's CODEBUDDY_* compaction env). None → default.
        cw = kwargs.pop("context_window", None)
        self._context_window: int | None = int(cw) if cw is not None else None
        pct = kwargs.pop("context_compact_pct", None)
        self._compact_pct: int | None = int(pct) if pct is not None else None
        # instance_id is carried only so Harbor records it in config.json (offline
        # key for tying this trial to proxy logs); the agent does not use it.
        kwargs.pop("instance_id", None)
        # Per-trial id (``trial_name`` = ``{task}__{uuid}``, unique per trial),
        # prefixed onto ANTHROPIC_API_KEY under local_proxy so the proxy can
        # attribute each request log line to one trial (instance_id is run-level).
        # The trial_name is not passed to the agent as a kwarg; it is only
        # recoverable from logs_dir, whose parent dir is named after it.
        self._session_id = self._trial_id_from_logs_dir(logs_dir)
        super().__init__(logs_dir, *args, version=self._cc_version, **kwargs)

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
        return "cc"

    def get_version_command(self) -> str | None:
        return 'export PATH="/usr/local/bin:$PATH"; claude --version'

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
        # Wire the split-mount launcher onto PATH at run time, then install only if
        # claude is still absent. The task image is harness-free (no baked
        # symlinks) and harbor overrides the container CMD with `sleep infinity`,
        # so a Dockerfile ENTRYPOINT never runs — this install() exec is the only
        # post-start hook. We symlink the mounted launcher (skip if missing), then
        # fall back to npm only on a bare image. When a version is pinned the
        # package spec includes it; --force avoids EEXIST.
        ver = self._cc_version or ""
        pkg = "@anthropic-ai/claude-code" + (f"@{ver}" if ver else "")
        mount_bin = f"{self._mount_path.rstrip('/')}/bin"
        await self.exec_as_root(
            environment,
            command=(
                'export PATH="/usr/local/bin:$PATH"; '
                # Link the mounted launcher onto PATH (idempotent; skip if missing).
                f'[ -x "{mount_bin}/claude" ] && ln -sf "{mount_bin}/claude" "/usr/local/bin/claude"; '
                "true; "
                "if command -v claude >/dev/null 2>&1; then "
                "echo 'claude present (split-mount/image), skipping npm install'; "
                f"else npm install -g --force {pkg}; fi && "
                "claude --version"
            ),
        )
        # Plan B (non-root): claude refuses bypassPermissions as root unless
        # IS_SANDBOX=1 (an undocumented signal). The official, dev-container-aligned
        # alternative is to run claude as a non-root user. We create that user HERE,
        # at install time (root), so no task image / dataset edit is needed — any
        # task works unchanged. install() runs under
        # `with_default_user(agent.user)` (harbor trial._prepare), so the configured
        # agent_user is already on environment.default_user; we just materialize it.
        await ensure_agent_user(self, environment)

    # ── run ──────────────────────────────────────────────────────
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        instruction = self.render_instruction(instruction)
        escaped_instruction = shlex.quote(instruction)

        model = self.model_name  # route slug under local_proxy; real id under direct

        # claude reads its endpoint/model/key from env (not a models.json).
        if self._conn_mode == "local_proxy":
            # The bench proxy speaks Anthropic on /v1/messages, keys routes by slug,
            # converts A→O (a2o) and rewrites body["model"] + injects extra_body. cc
            # only needs to address the route; the api key is a dummy for the hop.
            base_url = self._proxy_url
            api_key = self._get_env("ANTHROPIC_API_KEY") or "dummy-for-proxy"
            # Prefix the per-trial session id onto the token as ``{trial}::{route}``.
            # The proxy splits on ``::``: the trial half is logged for per-trial
            # request attribution, the route half selects the route (its strict
            # slug match is unaffected). ANTHROPIC_MODEL stays the bare route slug
            # (body.model fallback). Skip when there is no session id.
            if self._session_id:
                api_key = f"{self._session_id}::{model}"
        else:
            # Direct: claude talks to the model backend named by ANTHROPIC_BASE_URL.
            base_url = self._get_env("ANTHROPIC_BASE_URL") or ""
            api_key = self._get_env("ANTHROPIC_API_KEY") or ""

        # Who will this agent run as? harbor sets environment.default_user from the
        # task's [agent].user (which the bench fills from agent_user). None ⇒ the
        # image default (root for our task images).
        run_user = getattr(environment, "default_user", None)
        is_root = run_user in (None, "", "root", 0, "0")

        env: dict[str, str] = {
            "ANTHROPIC_API_KEY": api_key,
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_MODEL": model,
            # Disable non-essential traffic (telemetry, etc.).
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
        # CLAUDE_CONFIG_DIR is deliberately not set here. Docker env values are not
        # shell-expanded, so passing "$HOME/.claude" would reach claude as the
        # literal text "$HOME/.claude" — claude would then create a junk dir
        # literally named "$HOME" in the cwd (polluting the agent diff). Instead we
        # `export CLAUDE_CONFIG_DIR="$HOME/.claude"` inside the command strings
        # below, where the agent user's real $HOME expands.
        # bypassPermissions / --dangerously-skip-permissions is refused by claude
        # when it detects it is running as root, unless IS_SANDBOX=1 tells it the
        # outer environment is already sandboxed. When the bench runs the agent as a
        # non-root user (Plan B via agent_user), claude accepts bypassPermissions
        # natively, so we don't set IS_SANDBOX — matching how real users run it in a
        # dev container. We only fall back to IS_SANDBOX=1 when actually root.
        if is_root:
            env["IS_SANDBOX"] = "1"
        # When a custom base URL is set (proxy or self-hosted), pin every model
        # alias to our model so claude never substitutes a built-in Anthropic id.
        if base_url:
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = model
            env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = model
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = model
            env["CLAUDE_CODE_SUBAGENT_MODEL"] = model
        # max_output_tokens cbc-equivalent: claude reads CLAUDE_CODE_MAX_OUTPUT_TOKENS.
        # Under local_proxy the proxy is the complete source of truth for sampling
        # params it can express; this env is a harmless native fallback.
        if self._max_output_tokens is not None:
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(self._max_output_tokens)
        # Context-window auto-compaction: claude's analogues of cbc's CODEBUDDY_*
        # compaction env. Absolute-token window + optional percent override.
        if self._context_window is not None:
            env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(self._context_window)
        if self._compact_pct is not None:
            env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(self._compact_pct)
        # Drop empty-valued keys so claude prioritizes the available auth/endpoint.
        env = {k: v for k, v in env.items() if v}

        # settings.json: start from the deterministic preset (config layer). It
        # carries permissions.deny (single source of truth for disabled tools).
        settings_json: dict = copy.deepcopy(self._settings_preset)
        settings_b64 = base64.b64encode(json.dumps(settings_json).encode()).decode()
        # Export CLAUDE_CONFIG_DIR inside the shell so $HOME expands to the agent
        # user's real home (docker env values are not shell-expanded — see above).
        # Some task images bake ENV HOME=/home/<foo> (e.g. radare2 → /home/r2)
        # the non-root agent user cannot write. docker exec -u <user> inherits that
        # baked HOME rather than the exec user's real home, so mkdir "$HOME/.claude"
        # fails with Permission denied. Re-derive HOME from the running user's passwd
        # entry so it always points at a writable home. Task image stays untouched.
        home_fix = 'export HOME="$(getent passwd "$(id -u)" | cut -d: -f6)"; '
        config_dir_export = home_fix + 'export CLAUDE_CONFIG_DIR="$HOME/.claude"; '
        setup_cmd = (
            f'{config_dir_export}'
            'mkdir -p "$CLAUDE_CONFIG_DIR" && '
            f'echo {settings_b64} | base64 -d > "$CLAUDE_CONFIG_DIR/settings.json"'
        )
        await self.exec_as_agent(environment, command=setup_cmd, env=env)

        # Non-interactive, parseable stream. --print runs headless; stream-json's
        # final `result` event carries usage{input_tokens,output_tokens,...}.
        # Disabled tools are enforced via settings.json permissions.deny (above);
        # --settings points claude at the file we just wrote (explicit, not relying
        # on auto-discovery). Flags accepted by the pinned CLI (2.1.x):
        # --max-turns, --permission-mode, --effort (--max-turns is hidden from
        # --help but functional). claude has no boolean --thinking flag; thinking
        # depth is --effort. --max-turns errors out when the limit is hit (print mode).
        output_path = f"/logs/agent/{_OUTPUT_FILENAME}"
        flags = [
            "--verbose", "--output-format", "stream-json", "--print",
            "--permission-mode", "bypassPermissions",
            "--settings", '"$CLAUDE_CONFIG_DIR/settings.json"',
            "--model", shlex.quote(model),
        ]
        if self._max_turns is not None:
            flags += ["--max-turns", str(self._max_turns)]
        # thinking depth (claude's --effort: low|medium|high|xhigh|max) is not set
        # as a CLI flag here. It is controlled by CLAUDE_CODE_EFFORT_LEVEL in the
        # harness settings preset / version env (default "max"), so the bench owns
        # the depth uniformly without per-call flags. A --effort flag would take
        # precedence over that env and defeat the preset default, so we omit it.
        # (claude has no boolean --thinking flag; effort is the only depth knob.)
        run_cmd = (
            'export PATH="/usr/local/bin:$PATH"; '
            f'{config_dir_export}'
            f"claude {' '.join(flags)} -- {escaped_instruction} "
            f"2>&1 </dev/null | tee {output_path}"
        )
        await self.exec_as_agent(environment, command=run_cmd, env=env)

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
                "claude output not found: %s — token/cost context will be empty "
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
            self.logger.debug("Failed reading claude output: %s", exc)
            return None
        return events

    def _build_trajectory(self, events: list[dict]) -> Trajectory | None:
        """Map claude stream-json events to an ATIF Trajectory.

        claude events: a `system/init`, one or more `assistant` messages (content
        blocks: thinking/text/tool_use, each with usage), and a final `result`
        (authoritative usage + cost). Same shape as cbc.
        """
        steps: list[Step] = []
        session_id: str | None = None
        result_usage: dict | None = None
        total_cost: float | None = None
        step_id = 1  # ATIF Step.step_id is 1-indexed

        for ev in events:
            etype = ev.get("type")
            session_id = session_id or ev.get("session_id")
            if etype == "result":
                if isinstance(ev.get("usage"), dict):
                    result_usage = ev["usage"]
                if isinstance(ev.get("total_cost_usd"), (int, float)):
                    total_cost = float(ev["total_cost_usd"])
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
            metrics = None
            if usage:
                metrics = Metrics(
                    prompt_tokens=usage.get("input_tokens"),
                    completion_tokens=usage.get("output_tokens"),
                )
            steps.append(Step(
                step_id=step_id,
                source="agent",
                model_name=msg.get("model") or self.model_name,
                message="\n".join(text_parts),
                reasoning_content="\n".join(reasoning_parts) or None,
                tool_calls=tool_calls or None,
                metrics=metrics,
            ))
            step_id += 1

        final_metrics = None
        if result_usage:
            final_metrics = FinalMetrics(
                total_prompt_tokens=int(result_usage.get("input_tokens") or 0),
                total_completion_tokens=int(result_usage.get("output_tokens") or 0),
                total_cached_tokens=cached_input_tokens(result_usage),
                total_cost_usd=total_cost,
                total_steps=len(steps),
            )
        return Trajectory(
            session_id=session_id,
            agent=Agent(name="cc", version=self._version or "unknown",
                        model_name=self.model_name),
            steps=steps,
            final_metrics=final_metrics,
        )
