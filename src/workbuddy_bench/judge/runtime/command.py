"""Command execution adapters used by verifier runners."""

from __future__ import annotations

import math
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


Command = str | Sequence[str]


@dataclass
class CommandResult:
    command: Command
    return_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str | None = None
    duration_sec: float = 0.0

    @property
    def output_text(self) -> str:
        return "\n".join(part for part in (self.stdout, self.stderr) if part)


class CommandExecutor(Protocol):
    def run(
        self,
        command: Command,
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float | None = None,
        shell: bool | None = None,
    ) -> CommandResult:
        """Run ``command`` and return a normalized result."""


def _decode_timeout_stream(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _harbor_timeout(timeout_sec: float | None) -> int | None:
    if timeout_sec is None:
        return None
    if isinstance(timeout_sec, bool):
        raise TypeError("timeout_sec must be numeric")
    number = float(timeout_sec)
    if number <= 0:
        return 0
    return max(1, math.ceil(number))


def _completed_return_code(completed: Any) -> int:
    for attr in ("return_code", "returncode", "exit_code"):
        value = getattr(completed, attr, None)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


@dataclass
class LocalCommandExecutor:
    """Subprocess-backed executor used by local tests and host-side runners."""

    default_timeout_sec: float | None = None

    def run(
        self,
        command: Command,
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float | None = None,
        shell: bool | None = None,
    ) -> CommandResult:
        use_shell = isinstance(command, str) if shell is None else shell
        merged_env = os.environ.copy()
        if env:
            merged_env.update({str(key): str(value) for key, value in env.items()})
        timeout = self.default_timeout_sec if timeout_sec is None else timeout_sec
        start = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd) if cwd is not None else None,
                env=merged_env,
                timeout=timeout,
                shell=use_shell,
                text=True,
                capture_output=True,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - start
            return CommandResult(
                command=command,
                return_code=124,
                stdout=_decode_timeout_stream(exc.stdout),
                stderr=_decode_timeout_stream(exc.stderr),
                timed_out=True,
                error=f"command timed out after {timeout} seconds",
                duration_sec=round(duration, 4),
            )
        except OSError as exc:
            duration = time.monotonic() - start
            return CommandResult(
                command=command,
                return_code=127,
                error=str(exc),
                duration_sec=round(duration, 4),
            )

        duration = time.monotonic() - start
        return CommandResult(
            command=command,
            return_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_sec=round(duration, 4),
        )


@dataclass
class HarborCommandExecutor:
    """Async command executor backed by a Harbor environment."""

    environment: Any

    async def run(
        self,
        command: str,
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float | None = None,
        shell: bool | None = None,
    ) -> CommandResult:
        if shell is False:
            raise TypeError("HarborCommandExecutor only supports shell command strings")
        start = time.monotonic()
        try:
            completed = await self.environment.exec(
                command=command,
                cwd=str(cwd) if cwd is not None else None,
                env={str(key): str(value) for key, value in env.items()} if env else None,
                timeout_sec=_harbor_timeout(timeout_sec),
            )
        except TimeoutError as exc:
            return CommandResult(
                command=command,
                return_code=124,
                timed_out=True,
                error=str(exc) or "command timed out",
                duration_sec=round(time.monotonic() - start, 4),
            )
        except Exception as exc:
            return CommandResult(
                command=command,
                return_code=127,
                error=str(exc),
                duration_sec=round(time.monotonic() - start, 4),
            )
        return CommandResult(
            command=command,
            return_code=_completed_return_code(completed),
            stdout=str(getattr(completed, "stdout", "") or ""),
            stderr=str(getattr(completed, "stderr", "") or ""),
            duration_sec=round(time.monotonic() - start, 4),
        )
