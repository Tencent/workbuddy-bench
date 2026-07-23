"""WorkBuddy Bench Docker environment extensions."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable, MutableMapping
from itertools import zip_longest
from typing import Any

from harbor.environments.docker.docker import DockerEnvironment

_MIN_IMAGE_MOUNT_API = (1, 48)
_MIN_IMAGE_MOUNT_API_TEXT = "1.48"
_DOCKER_API_ENV = "DOCKER_API_VERSION"


def _parse_api_version(value: str | None) -> tuple[int, ...] | None:
    """Parse a Docker API version such as ``1.51`` into comparable parts."""

    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    parts = text.split(".")
    if not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _api_version_lt(left: tuple[int, ...], right: tuple[int, ...]) -> bool:
    """Return whether one parsed API version is lower than another."""

    for l_part, r_part in zip_longest(left, right, fillvalue=0):
        if l_part != r_part:
            return l_part < r_part
    return False


def _mount_type(mount: Any) -> str | None:
    if isinstance(mount, dict):
        value = mount.get("type")
    else:
        value = getattr(mount, "type", None)
    return str(value) if value is not None else None


def has_image_mount(mounts: Iterable[Any]) -> bool:
    """Return true when Harbor will ask Docker Compose for an OCI image mount."""

    return any(_mount_type(mount) == "image" for mount in mounts)


def _detect_docker_server_api_version() -> str:
    """Query the local Docker daemon's API version."""

    result = subprocess.run(
        ["docker", "version", "--format", "{{.Server.APIVersion}}"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return result.stdout.strip()


def ensure_image_mount_docker_api(
    env_vars: MutableMapping[str, str],
    mounts: Iterable[Any],
    *,
    detect_server_api_version: Callable[[], str] = _detect_docker_server_api_version,
) -> MutableMapping[str, str]:
    """Ensure Docker Compose uses an API version that supports image mounts.

    Docker Compose ``type: image`` volumes require Docker Engine API >= 1.48.
    Some Compose versions under-negotiate the API unless ``DOCKER_API_VERSION`` is
    explicit, so set it only for trials that actually contain image mounts.
    """

    if not has_image_mount(mounts):
        return env_vars

    explicit_api = env_vars.get(_DOCKER_API_ENV, "").strip()
    if explicit_api:
        parsed_explicit = _parse_api_version(explicit_api)
        if parsed_explicit is None:
            raise RuntimeError(
                f"{_DOCKER_API_ENV}={explicit_api!r} is not a valid Docker API "
                f"version; image mounts require API >= {_MIN_IMAGE_MOUNT_API_TEXT}."
            )
        if _api_version_lt(parsed_explicit, _MIN_IMAGE_MOUNT_API):
            raise RuntimeError(
                f"{_DOCKER_API_ENV}={explicit_api!r} is too old for Docker image "
                f"mounts; set {_DOCKER_API_ENV} to {_MIN_IMAGE_MOUNT_API_TEXT} "
                "or newer, or upgrade Docker."
            )
        return env_vars

    try:
        server_api = detect_server_api_version().strip()
    except Exception as exc:  # pragma: no cover - exercised via caller behavior
        raise RuntimeError(
            "Could not determine the Docker server API version. Docker image "
            f"mounts require API >= {_MIN_IMAGE_MOUNT_API_TEXT}."
        ) from exc

    parsed_server = _parse_api_version(server_api)
    if parsed_server is None:
        raise RuntimeError(
            f"Docker reported an invalid server API version {server_api!r}; image "
            f"mounts require API >= {_MIN_IMAGE_MOUNT_API_TEXT}."
        )
    if _api_version_lt(parsed_server, _MIN_IMAGE_MOUNT_API):
        raise RuntimeError(
            f"Docker server API {server_api!r} is too old for image mounts; "
            f"Docker image mounts require API >= {_MIN_IMAGE_MOUNT_API_TEXT}."
        )

    env_vars[_DOCKER_API_ENV] = server_api
    return env_vars


class WorkBuddyDockerEnvironment(DockerEnvironment):
    """Docker backend with WorkBuddy Bench image-mount API negotiation."""

    def _detect_server_api_version(self) -> str:
        cached = getattr(self, "_codebuddy_docker_server_api_version", None)
        if cached is None:
            cached = _detect_docker_server_api_version()
            self._codebuddy_docker_server_api_version = cached
        return str(cached)

    def _compose_env_vars(self, include_os_env: bool = True) -> dict[str, str]:
        env_vars = super()._compose_env_vars(include_os_env=include_os_env)
        ensure_image_mount_docker_api(
            env_vars,
            self._mounts,
            detect_server_api_version=self._detect_server_api_version,
        )
        return env_vars
