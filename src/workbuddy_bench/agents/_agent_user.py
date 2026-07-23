"""Shared runtime creation of the non-root agent user.

Both ``CcAgent`` and ``CbcAgent`` run their rollout (and the verifier) as the
configured ``agent_user`` (default ``dev`` — see ``configs/bench/_default.yaml``).
That user need not exist in the task image: this helper materializes it at
``install()`` time, in the root context, so task images stay harness-free and any
task works unchanged.
"""

from __future__ import annotations

import shlex

from harbor.environments.base import BaseEnvironment


async def ensure_agent_user(agent, environment: BaseEnvironment) -> None:
    """Create the configured non-root agent user (idempotent, cross-distro).

    Reads the target user from ``environment.default_user`` (= the job's
    ``agent_user``, set by harbor for install()). Root/None ⇒ no-op. Gives the
    user ownership of /workspace (so it can edit + commit) and marks the repo a
    git safe.directory (the repo was created as root, so a non-root git would
    otherwise abort with "dubious ownership").
    """
    run_user = getattr(environment, "default_user", None)
    if run_user in (None, "", "root", 0, "0"):
        return  # running as root: nothing to create
    u = shlex.quote(str(run_user))
    # useradd (debian/rhel) vs adduser (alpine/busybox); both guarded by `id`
    # so re-running is a no-op. -m create the home dir. Workspace ownership
    # + safe.directory let the user edit files and run git.
    await agent.exec_as_root(
        environment,
        command=(
            f"if ! id {u} >/dev/null 2>&1; then "
            f"  if command -v useradd >/dev/null 2>&1; then useradd -m -s /bin/bash {u}; "
            f"  elif command -v adduser >/dev/null 2>&1; then adduser -D -s /bin/sh {u}; "
            f"  else echo 'no useradd/adduser; cannot create {u}' >&2; exit 1; fi; "
            "fi && "
            f"mkdir -p /workspace && chown -R {u} /workspace && "
            f"(git config --system --add safe.directory /workspace || true) && "
            f"(git config --system --add safe.directory '*' || true)"
        ),
    )
