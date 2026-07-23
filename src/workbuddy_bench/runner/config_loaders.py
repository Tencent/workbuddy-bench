"""Shared config loaders for runner/runtime composition.

This module keeps file-layout compatibility logic in one place so shell entry
points, manifest resolution, Harbor runtime composition, and helper scripts do
not each need to know every historical config shape.
"""

from __future__ import annotations

import copy
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ResolvedHarnessConfig:
    """A harness config after defaults merging."""

    requested_slug: str
    slug: str
    path: Path
    harness: dict[str, Any]
    defaults_path: Path | None = None


@dataclass(frozen=True)
class DatasetRuntimeContract:
    """Dataset-level runtime contract used by WorkBuddy runner.

    This is project metadata, not a Harbor validity requirement. Missing
    metadata is intentionally non-fatal so older datasets keep working.
    """

    harness_delivery: str = ""
    requires_harness_mount: bool = False
    harness_delivery_by_harness: dict[str, str] = field(default_factory=dict)
    source: Path | None = None
    dataset_root: Path | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def requires_split_mount(self) -> bool:
        return self.requires_harness_mount or self.harness_delivery == "split-mount"

    def delivery_for_harness(self, harness_name: str) -> str:
        return self.harness_delivery_by_harness.get(harness_name, self.harness_delivery)

    def requires_split_mount_for(self, harness_name: str) -> bool:
        return self.requires_harness_mount or self.delivery_for_harness(harness_name) == "split-mount"

    def to_manifest(self, repo_root: Path | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "harness_delivery": self.harness_delivery or None,
            "requires_harness_mount": self.requires_harness_mount,
            "harness_delivery_by_harness": copy.deepcopy(self.harness_delivery_by_harness),
        }
        if self.source:
            data["source"] = _display_path(self.source, repo_root)
        if self.dataset_root:
            data["dataset_root"] = _display_path(self.dataset_root, repo_root)
        return data


def _display_path(path: Path, repo_root: Path | None = None) -> str:
    path = path.resolve()
    if repo_root:
        try:
            return str(path.relative_to(repo_root.resolve()))
        except ValueError:
            pass
    return str(path)


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` into ``base`` (override wins)."""

    out = copy.deepcopy(base) if isinstance(base, dict) else {}
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML mapping, raising clear errors for missing/bad files."""

    if not path.is_file():
        raise FileNotFoundError(f"config not found: {path}")
    data = yaml.safe_load(path.read_text())
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"config must be a mapping: {path}")
    return data


def normalize_model_protocols(model: dict[str, Any]) -> list[str]:
    """Return the model config's wire protocols, primary first, order-deduped.

    ``model.protocols`` is the single declaration of a model service's wire
    protocols (the first element is the primary, used for direct-mode forwarding
    and validation). A scalar is wrapped to a one-element list; a missing or
    empty value defaults to ``["openai"]``. The result is always a non-empty
    list; callers that need the primary take ``[0]``.
    """
    raw = model.get("protocols")
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = [str(v) for v in raw if v]
    else:
        values = ["openai"]
    if not values:
        values = ["openai"]

    out: list[str] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


def _harnesses_dir(configs_dir: Path) -> Path:
    return configs_dir / "harnesses"


def _load_nested_harness(
    slug: str,
    configs_dir: Path,
    *,
    requested_slug: str,
) -> ResolvedHarnessConfig:
    hdir = _harnesses_dir(configs_dir)
    parts = slug.split("/")
    family = parts[0]
    version_stem = parts[-1]
    # Canonical layout: <family>/versions/<version>.yaml.
    version_path = hdir / family / "versions" / f"{version_stem}.yaml"
    if not version_path.is_file():
        raise FileNotFoundError(f"harness config not found: {version_path}")

    defaults_path = hdir / family / "_defaults.yaml"
    data: dict[str, Any] = {}
    effective_defaults_path: Path | None = None
    if defaults_path.is_file():
        data = load_yaml(defaults_path)
        effective_defaults_path = defaults_path
    data = deep_merge(data, load_yaml(version_path))

    harness = data.get("harness")
    if not isinstance(harness, dict):
        raise ValueError(f"{version_path}: missing or invalid 'harness:' block")
    _derive_mount_from_version(harness, version_slug=parts[-1], context=str(version_path))
    return ResolvedHarnessConfig(
        requested_slug=requested_slug,
        slug=slug,
        path=version_path,
        harness=harness,
        defaults_path=effective_defaults_path,
    )


def harness_version(harness: dict[str, Any]) -> str | None:
    """Return the harness CLI version from a ``params.*_VERSION`` key, if any.

    A version file only needs to set one version param (e.g. ``CBC_VERSION`` /
    ``CLAUDE_CODE_VERSION``); everything else (mount image tag, build args) is
    derived from it. The first ``params`` key ending in ``_VERSION`` wins.
    """
    params = harness.get("params")
    if not isinstance(params, dict):
        return None
    for key, value in params.items():
        if str(key).upper().endswith("_VERSION") and value not in (None, ""):
            return str(value)
    return None


# cbc auto-compaction route (shared by cbc_agent runtime + manifest audit)
# cbc's compaction mechanism changed at this version. Below it (route A, e.g.
# 2.93.5) the only trigger is inputTokens/maxInputTokens >= pct, with no
# CODEBUDDY_AUTO_COMPACT_WINDOW env — so the window must go to maxInputTokens.
# At/above it (route B, e.g. 2.109.x) we carry an absolute-token window via
# CODEBUDDY_AUTO_COMPACT_WINDOW and deliberately omit maxInputTokens. Keep this
# constant + helper as the single source of truth so cbc_agent.run() and the
# manifest's harness_runtime_config audit can never diverge. cbc clamps the env
# to [100_000, 1_000_000].
CBC_AUTOCOMPACT_WINDOW_ENV_MIN_VERSION = (2, 103, 4)
CBC_AUTOCOMPACT_WINDOW_MIN = 100_000
CBC_AUTOCOMPACT_WINDOW_MAX = 1_000_000


def cbc_uses_autocompact_window_env(version: str | None) -> bool:
    """Whether this cbc version carries the window via CODEBUDDY_AUTO_COMPACT_WINDOW.

    True for >= 2.103.4 (route B, current) and for an unknown/None/unparseable
    version (image-baked or odd tag) — default to the current mechanism, since the
    legacy path is a shrinking set of one pinned old release. Only a parseable
    version strictly below the boundary takes route A.
    """
    if not version:
        return True
    try:
        from packaging.version import InvalidVersion, Version

        try:
            return Version(version).release >= CBC_AUTOCOMPACT_WINDOW_ENV_MIN_VERSION
        except InvalidVersion:
            return True
    except ImportError:  # pragma: no cover - packaging is a hard dep
        parts = tuple(int(p) for p in version.split(".") if p.isdigit())
        return parts >= CBC_AUTOCOMPACT_WINDOW_ENV_MIN_VERSION


def _derive_mount_from_version(
    harness: dict[str, Any], *, version_slug: str, context: str
) -> None:
    """Fill split-mount image/build args from the harness version param.

    Lets a ``<version>.yaml`` declare only the version (e.g.
    ``params: {CBC_VERSION: "2.109.1"}``). The shared ``_defaults.yaml`` carries
    the version-independent ``mount`` skeleton (name / path / dockerfile /
    context / build.args.MOUNT_PATH). This derives, only when not explicitly set:

      * ``mount.images.local``        -> ``<mount.name>:<version>``
      * ``mount.build.args.<VER_KEY>`` -> the version (key copied from params)

    Explicit values in the version/defaults YAML always win (no override).
    """
    mount = harness.get("mount")
    if not isinstance(mount, dict):
        return  # harness has no split-mount (e.g. baked-in); nothing to derive.

    version = harness_version(harness) or (version_slug if version_slug else None)
    name = mount.get("name")

    # mount.images.local — derive "<image_prefix><name>:<version>" when absent.
    # ``mount.image_prefix`` (in _defaults) namespaces the local tag so it can't
    # collide with a real published image of the same name; default
    # "workbuddy-bench/harness/".
    images = mount.setdefault("images", {})
    if isinstance(images, dict) and not images.get("local"):
        if not name:
            raise ValueError(
                f"{context}: cannot derive mount.images.local without mount.name "
                "(set it in the family _defaults.yaml)"
            )
        if not version:
            raise ValueError(
                f"{context}: cannot derive mount image tag without a version "
                "(set a params.<NAME>_VERSION key)"
            )
        prefix = mount.get("image_prefix", "workbuddy-bench/harness/")
        images["local"] = f"{prefix}{name}:{version}"

    # mount.build.args.<VERSION_KEY> — derive from the params version key.
    if version:
        build = mount.setdefault("build", {})
        if isinstance(build, dict):
            args = build.setdefault("args", {})
            params = harness.get("params") or {}
            ver_key = next(
                (k for k in params if str(k).upper().endswith("_VERSION")), None
            )
            if isinstance(args, dict) and ver_key and ver_key not in args:
                args[ver_key] = version


def load_harness_config(
    slug: str,
    configs_dir: Path,
) -> ResolvedHarnessConfig:
    """Load a harness config by canonical slug.

    A slug is ``<family>/<version>`` and resolves to
    ``configs/harnesses/<family>/_defaults.yaml`` + ``<family>/versions/<version>.yaml``,
    e.g. ``claude-code/2.1.104``.
    """

    configs_dir = Path(configs_dir)
    hdir = _harnesses_dir(configs_dir)

    if "/" in slug:
        return _load_nested_harness(slug, configs_dir, requested_slug=slug)

    raise FileNotFoundError(
        f"harness config not found for slug {slug!r} under {hdir}: "
        f"expected a canonical <family>/<version> slug"
    )


def iter_harness_configs(configs_dir: Path) -> list[ResolvedHarnessConfig]:
    """Enumerate canonical nested harness configs (<family>/versions/<version>.yaml)."""

    hdir = _harnesses_dir(Path(configs_dir))
    resolved: list[ResolvedHarnessConfig] = []

    if not hdir.is_dir():
        return []

    for family_dir in sorted(p for p in hdir.iterdir() if p.is_dir()):
        if family_dir.name.startswith("_"):
            continue
        vdir = family_dir / "versions"
        if not vdir.is_dir():
            continue
        for path in sorted(vdir.glob("*.yaml")):
            if path.name.startswith("_"):
                continue
            slug = f"{family_dir.name}/{path.stem}"
            resolved.append(load_harness_config(slug, Path(configs_dir)))

    return resolved


def select_harness_mount_image(mount: dict[str, Any], backend: str) -> str:
    """Return the backend-specific harness mount image ref (``mount.images.local``).

    Only the ``local`` backend is shipped; ``backend`` is kept in
    the signature so the manifest schema and callers stay stable.
    """

    images = mount.get("images") if isinstance(mount.get("images"), dict) else {}
    value = images.get("local") or ""
    return str(value) if value else ""


def harness_mount_summary(
    harness: dict[str, Any],
    *,
    backend: str,
    required: bool,
) -> dict[str, Any]:
    """Build a manifest-friendly summary of the selected harness mount."""

    mount = harness.get("mount") if isinstance(harness.get("mount"), dict) else {}
    images = mount.get("images") if isinstance(mount.get("images"), dict) else {}
    selected_image = select_harness_mount_image(mount, backend) if mount else ""
    return {
        "required": required,
        "name": mount.get("name") if mount else None,
        "path": mount.get("path") if mount else None,
        "images": copy.deepcopy(images) if images else {},
        "selected_backend": backend,
        "selected_harness_backend": backend,
        "selected_image": selected_image or None,
    }


def validate_harness_mount_available(
    harness: dict[str, Any],
    *,
    backend: str,
    context: str,
) -> None:
    """Fail fast when a required split-mount harness cannot be mounted."""

    mount = harness.get("mount") if isinstance(harness.get("mount"), dict) else None
    if not mount:
        raise ValueError(f"{context}: dataset requires a split-mount harness, but selected harness has no mount block")
    if not mount.get("path"):
        raise ValueError(f"{context}: harness mount requires a non-empty path")
    if not select_harness_mount_image(mount, backend):
        raise ValueError(
            f"{context}: harness mount requires an image for backend={backend!r} "
            "(expected mount.images.local)"
        )


def _candidate_dataset_tomls(dataset_path: Path, repo_root: Path | None = None) -> list[Path]:
    """Return likely dataset.toml locations for a dataset root/tasks/task path."""

    candidates: list[Path] = []
    for path in [dataset_path, *dataset_path.parents]:
        candidates.append(path / "dataset.toml")
        if repo_root and path.resolve() == repo_root.resolve():
            break
    return candidates


def load_dataset_runtime_contract(
    dataset: str | Path,
    *,
    repo_root: Path | None = None,
) -> DatasetRuntimeContract:
    """Load ``[runtime]`` from the dataset-level ``dataset.toml`` if present."""

    dataset_path = Path(dataset)
    if not dataset_path.is_absolute() and repo_root is not None:
        dataset_path = repo_root / dataset_path

    for candidate in _candidate_dataset_tomls(dataset_path, repo_root=repo_root):
        if not candidate.is_file():
            continue
        data = tomllib.loads(candidate.read_text())
        runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
        harness_delivery = runtime.get("harness_delivery") or ""
        requires = runtime.get("requires_harness_mount", False)
        delivery_by_harness = runtime.get("harness_delivery_by_harness")
        if not isinstance(delivery_by_harness, dict):
            delivery_by_harness = {}
        return DatasetRuntimeContract(
            harness_delivery=str(harness_delivery) if harness_delivery else "",
            requires_harness_mount=bool(requires),
            harness_delivery_by_harness={str(k): str(v) for k, v in delivery_by_harness.items()},
            source=candidate,
            dataset_root=candidate.parent,
            raw=copy.deepcopy(runtime),
        )

    return DatasetRuntimeContract()
