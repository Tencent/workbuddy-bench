# Adding / adjusting a harness

A harness is the agent CLI under evaluation (`codebuddy-code`, `claude-code`). It is
layer 2 of the four-layer configuration, loaded at runtime through Harbor's installed-agent
mechanism: a job's `agents[].import_path` points to a Python class, which Harbor
instantiates inside the task container and then calls `install()` followed by `run()`.

## Configuration layout

```
configs/harnesses/<family>/
  _defaults.yaml            shared identity: name / import_path / protocol / params / mount skeleton
  versions/<version>.yaml   per version: CLI version number + settings_file + env (the loader derives the mount tag from this)
  presets/                  deterministic configuration presets (static fields of settings.json / models.json)
  docker/Dockerfile         split-mount image build template
  CONFIG.md                 mechanism notes for the family
```

## The agent class: writing a `BaseInstalledAgent` subclass from scratch

Both harnesses in this bench (`agents/cbc_agent.py:CbcAgent`, `agents/cc_agent.py:CcAgent`)
are **`BaseInstalledAgent` subclasses written from scratch**, rather than extensions of
Harbor's built-in agent classes. claude-code does have a built-in implementation in
Harbor (`harbor.agents.installed.claude_code`), but we don't use it: the built-in class
knows nothing about this bench's split-mount, proxy, or context-window translation, and
keeping the two harnesses isomorphic — the same install/run/populate structure — is simply
easier to maintain.

A subclass must implement:

| Method | Responsibility |
|---|---|
| `name()` | harness identifier |
| `get_version_command()` | command to read the CLI version inside the container |
| `install(environment)` | symlink the split-mount launcher onto PATH (fall back to installing it if missing); see below |
| `run(...)` | write the CLI configuration, invoke it non-interactively, tee output to `/logs/agent` |
| `populate_context_post_run(context)` | optional: parse output → ATIF trace + backfill token counts |

`import_path` points to this class and is set in `_defaults.yaml`:

```yaml
harness:
  import_path: "workbuddy_bench.agents.cbc_agent:CbcAgent"
```

Harbor stays pinned to a fixed version (`[tool.uv.sources]` in `pyproject`); the subclass
depends only on the stable `BaseInstalledAgent` interface and does not fork Harbor.

## Version files

`versions/<version>.yaml` declares the CLI version number plus which `settings_file` and env
that version uses. The loader (`config_loaders._derive_mount_from_version`) derives
`mount.images.local` (= `workbuddy-bench/harness/<name>:<version>`) and
`build.args.<NAME>_VERSION` from it. `_defaults.yaml` carries the version-independent mount
skeleton and does **not** provide a default for `settings_file`—a missing mapping fails fast
rather than silently applying the wrong group.

Adding a version = write a `versions/<v>.yaml` and run:

```bash
scripts/harness/build-harness-mounts.sh --harness <family>/<version>
```

## How split-mount is implemented (a native Harbor capability)

The CLI is **not baked into the task image**. Each version is built into a read-only image
that gets mounted at the task container's `mount.path` at runtime, and the agent's `install()`
symlinks it onto PATH. The payoff: one task image serves any harness / version, switching
harnesses never rebuilds the task image, and nothing has to be installed over the network at
runtime.

Underneath, this is Harbor's native image-backed volume (not bench-specific):

- Harbor's `ServiceVolumeConfig` (`models/trial/config.py`) supports `type: "image"`
  (alongside `bind` / `volume`), and has since v0.13.0.
- `prepare_job._harness_mount_volume` translates a harness's `mount` block into
  `{type: image, source: <image>, target: <path>, read_only: true}` and places it in
  `environment.mounts`.
- Harbor's docker environment writes these mounts into the compose override's
  `services.main.volumes` (`environments/docker/__init__.py`), where Docker Compose mounts
  them read-only as native image-backed volumes.

> Harbor provides the **mechanism** (image volumes, a general-purpose capability); the bench
> combines it with the convention of a "harness-free image + runtime symlink from `install()`"
> to meet the split-mount evaluation requirement.
