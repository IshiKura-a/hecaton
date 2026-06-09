# Copyright 2026 The hecaton Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Scaffold adapters — translate scaffold-native actions to /execute calls.

hecaton's sandbox HTTP contract is the lowest common denominator: a pod
exposes `POST /execute {"command"}` and returns stdout/stderr/exit_code.
Real agent scaffolds (R2E-Gym, MCP-mounted toolsets, SWE-agent, ...)
each have their own action shape — XML function calls, JSON-RPC,
custom dataclasses. A ScaffoldAdapter is the small piece of code that
mediates between those two worlds:

  action --render--> bash --/execute--> ExecResult --parse--> observation

The adapter abstraction lives in the trainer SDK, not in the broker,
because rendering and parsing are pure trainer-side concerns — the
fleet only ever sees the bash command. Adapters register themselves
under a scaffold name that matches the directory under scaffolds/
(and the hostPath the broker mounts).
"""

from __future__ import annotations

from typing import Any, Protocol

from .provider import ExecResult

# Where the broker mounts scaffold tools inside every sandbox pod.
# This must match the value the broker uses when injecting the
# hostPath volume — see platform/broker/broker.py:SCAFFOLD_MOUNT.
SCAFFOLD_MOUNT = "/opt/agent-tools"
# Where the SDK installs scaffold pip requirements at acquire time
# (see provider.SandboxProvider._install_scaffold_requirements).
# Adapters that invoke Python tools must prepend PYTHONPATH=<this>.
SCAFFOLD_DEPS = "/tmp/scaffold-deps"


class ScaffoldAdapter(Protocol):
    """Render scaffold actions to bash, parse /execute results back."""

    def render(self, action: Any) -> str:
        """Translate a scaffold-native action into a bash command."""

    def parse(self, result: ExecResult) -> Any:
        """Translate a /execute response into a scaffold-native observation."""


_REGISTRY: dict[str, ScaffoldAdapter] = {}


def register_scaffold(name: str, adapter: ScaffoldAdapter) -> None:
    """Register an adapter under a scaffold name.

    The name must match the directory under scaffolds/ that phase 27
    staged on every host. Re-registering overwrites.
    """
    _REGISTRY[name] = adapter


def get_scaffold(name: str) -> ScaffoldAdapter:
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "<none>"
        raise KeyError(
            f"no ScaffoldAdapter registered for {name!r}; known: {known}"
        ) from None


# --- built-in adapters ------------------------------------------------------


class R2EGymAdapter:
    """Adapter for R2E-Gym scaffolds.

    Accepts any object exposing `.to_bashcmd() -> str` — in practice an
    r2egym `Action`, but we don't import r2egym here so hecaton_envs
    has no dependency on it. Trainers that use this adapter install
    r2egym themselves.

    Rewrites the command to:
      env PYTHONPATH=<deps> /opt/agent-tools/<tool>.py <args>

    - `.py` suffix because upstream R2E-Gym strips it when staging
      tools onto PATH; we keep the suffix on disk and re-attach here.
    - Absolute path so the sandbox image's PATH is irrelevant.
    - `env PYTHONPATH=...` (the literal `env` program) because
      `/execute` is argv-not-shell, so `KEY=val cmd` shell syntax
      doesn't work — `env` is a real executable that does.
    """

    def render(self, action: Any) -> str:
        cmd = action.to_bashcmd()
        name, sep, rest = cmd.partition(" ")
        return (
            f"env PYTHONPATH={SCAFFOLD_DEPS} "
            f"{SCAFFOLD_MOUNT}/{name}.py{sep}{rest}"
        )

    def parse(self, result: ExecResult) -> ExecResult:
        return result


register_scaffold("r2egym", R2EGymAdapter())
