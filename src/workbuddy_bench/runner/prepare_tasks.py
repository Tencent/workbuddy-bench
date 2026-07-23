"""Prepare Harbor task directories for WorkBuddy Bench runs."""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

HOST_GATEWAY_ENTRY = "host.docker.internal:host-gateway"
# Harbor's egress-control sidecar service name (docker-compose-egress-control.yaml).
# When a task runs under egress control, Harbor forces ``main`` onto
# ``network_mode: service:<sidecar>``, which Docker forbids from also carrying
# ``extra_hosts``. We instead attach the host-gateway entry to the sidecar (the
# netns owner); ``main`` inherits the resolved host.docker.internal via the
# shared network namespace.
EGRESS_SIDECAR_SERVICE = "harbor-docker-egress-control-sidecar"
COMPOSITE_VERIFIER_IMPORT_PATH = "workbuddy_bench.judge:CompositeVerifier"
COMPOSITE_VERIFIER_SCHEMA = "workbuddy.verifier.v1"
COMPOSITE_VERIFIER_ENGINE = "composite"


def iter_harbor_tasks(path: Path) -> list[Path]:
    """Return task directories under ``path``.

    ``path`` can point either at one Harbor task directory or at a directory
    containing multiple task subdirectories.
    """
    if (path / "task.toml").exists():
        return [path]
    if not path.exists():
        raise FileNotFoundError(f"tasks path not found: {path}")
    return sorted(
        child
        for child in path.iterdir()
        if child.is_dir() and (child / "task.toml").exists()
    )


def _dataset_config_for_tasks(task_dirs: list[Path]) -> dict:
    if not task_dirs:
        return {}
    dataset_toml = task_dirs[0].parent.parent / "dataset.toml"
    if not dataset_toml.is_file():
        return {}
    try:
        return tomllib.loads(dataset_toml.read_text())
    except tomllib.TOMLDecodeError:
        return {}


def _has_composite_verifier_contract(task_dirs: list[Path]) -> bool:
    data = _dataset_config_for_tasks(task_dirs)
    verifier = data.get("verifier") or {}
    if not isinstance(verifier, dict):
        return False
    return (
        str(verifier.get("schema") or "") == COMPOSITE_VERIFIER_SCHEMA
        and str(verifier.get("engine") or "") == COMPOSITE_VERIFIER_ENGINE
    )


def _remove_verifier_profile_kwarg(text: str) -> str | None:
    lines = text.splitlines(keepends=True)
    header_idx = -1
    table_end = len(lines)
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped == "[verifier.kwargs]":
            header_idx = i
            continue
        if header_idx != -1 and stripped.startswith("[") and stripped.endswith("]"):
            table_end = i
            break
    if header_idx == -1:
        return None

    remove: set[int] = set()
    for i in range(header_idx + 1, table_end):
        stripped = lines[i].strip()
        if "=" in stripped and stripped.split("=", 1)[0].strip() == "profile":
            remove.add(i)

    if not remove:
        return None

    has_remaining_entries = any(
        i not in remove and lines[i].strip() and not lines[i].lstrip().startswith("#")
        for i in range(header_idx + 1, table_end)
    )
    if not has_remaining_entries:
        remove.add(header_idx)
        while table_end - 1 in remove or (
            table_end - 1 >= 0 and not lines[table_end - 1].strip()
        ):
            table_end -= 1
            remove.add(table_end)
            if table_end <= header_idx + 1:
                break

    new = "".join(line for i, line in enumerate(lines) if i not in remove)
    return new if new != text else None


def _set_verifier_import_path(text: str) -> str | None:
    lines = text.splitlines(keepends=True)
    header_idx = -1
    table_end = len(lines)
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped == "[verifier]":
            header_idx = i
            continue
        if header_idx != -1 and stripped.startswith("[") and stripped.endswith("]"):
            table_end = i
            break
    if header_idx == -1:
        return None

    desired = f'import_path = "{COMPOSITE_VERIFIER_IMPORT_PATH}"\n'
    for i in range(header_idx + 1, table_end):
        stripped = lines[i].strip()
        if "=" in stripped and stripped.split("=", 1)[0].strip() == "import_path":
            if lines[i] == desired or stripped == desired.strip():
                return None
            lines[i] = desired
            return "".join(lines)

    insert_at = header_idx + 1
    while insert_at < table_end and not lines[insert_at].strip():
        insert_at += 1
    lines.insert(insert_at, desired)
    return "".join(lines)


def ensure_composite_verifier_contract(tasks_path: Path) -> int:
    """Mark tasks for the generic CompositeVerifier contract.

    Unlike the legacy profile helpers, this only ensures the verifier import path
    and removes stale ``verifier.kwargs.profile`` entries. Dataset behavior is
    resolved from ``dataset.toml`` + ``shared/verifier/plugin.py`` at runtime.
    """

    task_dirs = iter_harbor_tasks(tasks_path)
    if not _has_composite_verifier_contract(task_dirs):
        return 0

    changed = 0
    for task_dir in task_dirs:
        toml_path = task_dir / "task.toml"
        if not toml_path.exists():
            continue
        text = toml_path.read_text()
        new = text
        updated = _set_verifier_import_path(new)
        if updated is not None:
            new = updated
        updated = _remove_verifier_profile_kwarg(new)
        if updated is not None:
            new = updated
        if new != text:
            toml_path.write_text(new)
            changed += 1
    return changed


def _task_uses_egress_control(task_dir: Path) -> bool:
    """Whether Harbor will run this Linux task under egress control.

    Mirrors Harbor's trigger: a sidecar is spawned when the container is *not*
    Windows and any phase (environment/agent/verifier, incl. steps) declares a
    ``network_mode`` other than the default ``public``. Absent keys default to
    ``public``, so a task with no ``network_mode`` anywhere gets no sidecar.
    """
    toml_path = task_dir / "task.toml"
    if not toml_path.exists():
        return False
    try:
        cfg = tomllib.loads(toml_path.read_text())
    except tomllib.TOMLDecodeError:
        return False

    environment = cfg.get("environment")
    if isinstance(environment, dict):
        if str(environment.get("os") or "linux").lower() == "windows":
            return False

    def _phase_modes(table: dict) -> list[str]:
        modes: list[str] = []
        for key in ("environment", "agent", "verifier"):
            section = table.get(key)
            if isinstance(section, dict) and section.get("network_mode"):
                modes.append(str(section["network_mode"]))
        return modes

    modes = _phase_modes(cfg)
    steps = cfg.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict):
                modes.extend(_phase_modes(step))

    return any(mode != "public" for mode in modes)


def _set_service_host_gateway(services: dict, service_name: str) -> bool:
    """Ensure ``service_name`` carries the host-gateway extra_hosts entry.

    Returns True if the compose document was mutated.
    """
    service = services.setdefault(service_name, {})
    if not isinstance(service, dict):
        raise ValueError(f"compose services.{service_name} must be a mapping")
    extra_hosts = service.get("extra_hosts")
    if extra_hosts is None:
        extra_hosts = []
        service["extra_hosts"] = extra_hosts
    if not isinstance(extra_hosts, list):
        raise ValueError(f"compose services.{service_name}.extra_hosts must be a list")
    if HOST_GATEWAY_ENTRY in extra_hosts:
        return False
    extra_hosts.append(HOST_GATEWAY_ENTRY)
    return True


def _drop_service_host_gateway(services: dict, service_name: str) -> bool:
    """Remove a stale host-gateway entry (and prune the service if it becomes
    an empty mapping we effectively own). Returns True if anything changed."""
    service = services.get(service_name)
    if not isinstance(service, dict):
        return False
    extra_hosts = service.get("extra_hosts")
    if not isinstance(extra_hosts, list) or HOST_GATEWAY_ENTRY not in extra_hosts:
        return False
    extra_hosts[:] = [h for h in extra_hosts if h != HOST_GATEWAY_ENTRY]
    changed = True
    if not extra_hosts:
        service.pop("extra_hosts", None)
    if not service:
        services.pop(service_name, None)
    return changed


def ensure_host_gateway_compose(tasks_path: Path) -> int:
    """Ensure every task can reach the host proxy via host.docker.internal.

    Harbor already supports task-local ``environment/docker-compose.yaml``
    overlays.  Using that extension point keeps the host gateway requirement in
    this benchmark repo instead of patching Harbor's global compose file.

    Placement depends on whether the task runs under egress control:

    * **No egress** (all phases ``public``): ``main`` owns its network namespace,
      so the host-gateway entry goes on ``services.main`` as before.
    * **Egress** (any phase not ``public``, Linux): Harbor forces ``main`` onto
      ``network_mode: service:<sidecar>``, which cannot coexist with
      ``extra_hosts``.  The entry instead goes on the sidecar service; ``main``
      inherits the resolved name through the shared netns.  This preserves the
      task's allowlist/no-network isolation.

    Returns the number of files created or updated.
    """
    task_dirs = iter_harbor_tasks(tasks_path)
    if not task_dirs:
        raise ValueError(f"no Harbor task directories found under: {tasks_path}")

    changed = 0
    for task_dir in task_dirs:
        compose_path = task_dir / "environment" / "docker-compose.yaml"
        compose_path.parent.mkdir(parents=True, exist_ok=True)

        existed = compose_path.exists()
        if existed:
            data = yaml.safe_load(compose_path.read_text()) or {}
            if not isinstance(data, dict):
                raise ValueError(f"compose file must contain a mapping: {compose_path}")
        else:
            data = {}

        services = data.setdefault("services", {})
        if not isinstance(services, dict):
            raise ValueError(f"compose services must be a mapping: {compose_path}")

        if _task_uses_egress_control(task_dir):
            target, stale = EGRESS_SIDECAR_SERVICE, "main"
        else:
            target, stale = "main", EGRESS_SIDECAR_SERVICE

        mutated = _drop_service_host_gateway(services, stale)
        mutated = _set_service_host_gateway(services, target) or mutated

        if not existed or mutated:
            compose_path.write_text(
                yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
            )
            changed += 1

    return changed


def _set_toml_table_user(text: str, table: str, user: str) -> str | None:
    """Set ``user = "<user>"`` inside a top-level TOML ``[table]``.

    Line-level edit (no TOML writer dep): if the table already has a ``user``
    key, rewrite that line; otherwise insert the key right after the table
    header. Returns the new text, or None if unchanged / the table is absent.
    Idempotent — re-running with the same user is a no-op.
    """
    lines = text.splitlines(keepends=True)
    header = f"[{table}]"
    desired = f'user = "{user}"\n'
    in_table = False
    header_idx = -1
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped == header:
            in_table = True
            header_idx = i
            continue
        if in_table:
            if stripped.startswith("[") and stripped.endswith("]"):
                break  # next table; user key absent → insert after header
            if "=" in stripped and stripped.split("=", 1)[0].strip() == "user":
                if raw == desired or stripped == desired.strip():
                    return None  # already set
                lines[i] = desired
                return "".join(lines)
    if header_idx == -1:
        return None  # table not present
    # Insert the user line immediately after the header (preserve trailing nl).
    if not lines[header_idx].endswith("\n"):
        lines[header_idx] += "\n"
    lines.insert(header_idx + 1, desired)
    return "".join(lines)


def ensure_task_user(
    tasks_path: Path, *, agent_user: str | None, verifier_user: str | None
) -> int:
    """Inject ``[agent].user`` / ``[verifier].user`` into each task.toml.

    Harbor reads the container exec user from ``task.config.agent.user`` /
    ``verifier.user`` (task.toml), NOT from the job's agents[] block. This lets a
    job/bench-level user (e.g. for harnesses that refuse root) apply to all tasks
    without hand-editing each task.toml. ``None`` leaves the file untouched
    (default root). Idempotent.
    """
    if not agent_user and not verifier_user:
        return 0
    changed = 0
    for task_dir in iter_harbor_tasks(tasks_path):
        toml_path = task_dir / "task.toml"
        if not toml_path.exists():
            continue
        text = toml_path.read_text()
        new = text
        if agent_user:
            updated = _set_toml_table_user(new, "agent", agent_user)
            if updated is not None:
                new = updated
        if verifier_user:
            updated = _set_toml_table_user(new, "verifier", verifier_user)
            if updated is not None:
                new = updated
        if new != text:
            toml_path.write_text(new)
            changed += 1
    return changed


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Prepare Harbor task directories for WorkBuddy Bench."
    )
    parser.add_argument("tasks_path", type=Path)
    parser.add_argument("--agent-user", default=None,
                        help="Inject [agent].user into each task.toml (default: leave as-is/root)")
    parser.add_argument("--verifier-user", default=None,
                        help="Inject [verifier].user into each task.toml (default: leave as-is/root)")
    args = parser.parse_args()

    compose_changed = ensure_host_gateway_compose(args.tasks_path)
    composite_contract_changed = ensure_composite_verifier_contract(args.tasks_path)
    user_changed = ensure_task_user(
        args.tasks_path,
        agent_user=args.agent_user or None,
        verifier_user=args.verifier_user or None,
    )
    print(
        "Prepared Harbor tasks: "
        f"compose_changed={compose_changed} "
        f"composite_contract_changed={composite_contract_changed} "
        f"user_changed={user_changed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
