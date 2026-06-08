"""SandboxProvider — trainer-facing API.

Usage:

    p = SandboxProvider.from_env(run_id="run-2026-06-08")
    p.revoke()                              # clean orphans from a prior crash
    sb = p.acquire(template="swe-django-restapi")
    res = sb.exec("pytest -q")
    p.release(sb)

The set of available templates is fleet-side configuration; each task
repo registers a SandboxTemplate CR that fixes image, resources and
anything else about the pod. Trainers reference templates by name only.

Why we do not import `k8s_agent_sandbox` (the upstream Python SDK):
  * its `Sandbox` class needs a `K8sHelper`, which talks to the
    Kubernetes API. Trainers in our model have no kubeconfig.
  * its discovery flow is built around SandboxClaim+WarmPool; we cold-
    create one Sandbox per task and don't want to manage warmpools.
  * the sandbox-pod HTTP surface is small (execute/upload/download/list/
    exists) and worth matching by hand to keep this SDK light.

This module matches the upstream wire format byte-for-byte so that any
sandbox image conforming to the agent-sandbox spec works unchanged.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.parse
from dataclasses import dataclass, field

import httpx

from .errors import BrokerError, SandboxExecError


def _get_tailscale_device_id() -> str:
    """Get this machine's Tailscale device ID from the local daemon."""
    try:
        out = subprocess.check_output(
            ["tailscale", "status", "--json"],
            timeout=5,
        )
        return json.loads(out)["Self"]["ID"]
    except Exception:
        return ""


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_s: float


@dataclass
class SandboxHandle:
    id: str
    run_id: str
    template: str
    host: str
    port: int
    # Bound to a SandboxProvider in `acquire`; not part of the public surface.
    _provider: SandboxProvider = field(repr=False)
    node: str = ""

    def exec(self, cmd: str, *, timeout_s: float | None = None) -> ExecResult:
        url = f"http://{self.host}:{self.port}/execute"
        started = time.monotonic()
        try:
            resp = self._provider._sandbox.post(
                url,
                json={"command": cmd},
                timeout=httpx.Timeout(timeout_s) if timeout_s else None,
            )
        except httpx.HTTPError as exc:
            raise SandboxExecError(f"sandbox {self.id} unreachable: {exc}") from exc

        if resp.status_code != 200:
            raise SandboxExecError(
                f"sandbox {self.id} HTTP {resp.status_code}: {resp.text[:500]}"
            )

        data = resp.json()
        self._provider.heartbeat(self.id)
        return ExecResult(
            stdout=data["stdout"],
            stderr=data["stderr"],
            exit_code=data["exit_code"],
            duration_s=time.monotonic() - started,
        )

    def upload(self, path: str, data: bytes) -> None:
        url = f"http://{self.host}:{self.port}/upload"
        resp = self._provider._sandbox.post(url, files={"file": (path, data)})
        if resp.status_code != 200:
            raise SandboxExecError(
                f"sandbox {self.id} upload HTTP {resp.status_code}: {resp.text[:500]}"
            )

    def read(self, path: str) -> bytes:
        q = urllib.parse.quote(path, safe="")
        url = f"http://{self.host}:{self.port}/download/{q}"
        resp = self._provider._sandbox.get(url)
        if resp.status_code != 200:
            raise SandboxExecError(
                f"sandbox {self.id} download HTTP {resp.status_code}: {resp.text[:500]}"
            )
        return resp.content

    def list(self, path: str) -> list[dict]:
        q = urllib.parse.quote(path, safe="")
        url = f"http://{self.host}:{self.port}/list/{q}"
        resp = self._provider._sandbox.get(url)
        if resp.status_code != 200:
            raise SandboxExecError(
                f"sandbox {self.id} list HTTP {resp.status_code}: {resp.text[:500]}"
            )
        return resp.json() or []

    def exists(self, path: str) -> bool:
        q = urllib.parse.quote(path, safe="")
        url = f"http://{self.host}:{self.port}/exists/{q}"
        resp = self._provider._sandbox.get(url)
        if resp.status_code != 200:
            raise SandboxExecError(
                f"sandbox {self.id} exists HTTP {resp.status_code}: {resp.text[:500]}"
            )
        return bool(resp.json().get("exists", False))


class SandboxProvider:
    def __init__(self, *, broker_url: str, token: str, run_id: str) -> None:
        self.run_id = run_id
        self._base = broker_url.rstrip("/")
        self._broker = httpx.Client(
            timeout=httpx.Timeout(60.0),
            headers={"Authorization": f"Bearer {token}"},
        )
        self._sandbox = httpx.Client(timeout=httpx.Timeout(600.0))
        self._register()

    def _register(self) -> None:
        """Register this trainer with the broker for lifecycle tracking."""
        try:
            self._broker.post(
                f"{self._base}/register",
                json={"run_id": self.run_id, "device_id": _get_tailscale_device_id()},
            )
        except httpx.HTTPError:
            pass  # best-effort; broker may be older version

    @classmethod
    def from_env(cls, *, run_id: str) -> SandboxProvider:
        return cls(
            broker_url=os.environ["HECATON_BROKER_URL"],
            token=os.environ["HECATON_TOKEN"],
            run_id=run_id,
        )

    def acquire(self, template: str) -> SandboxHandle:
        resp = self._broker.post(
            f"{self._base}/sandboxes",
            json={"run_id": self.run_id, "template": template},
        )
        _raise(resp)
        d = resp.json()
        return SandboxHandle(
            id=d["id"],
            run_id=d["run_id"],
            template=d["template"],
            host=d["host"],
            port=d["port"],
            node=d.get("node", ""),
            _provider=self,
        )

    def heartbeat(self, sandbox_id: str) -> None:
        """Send heartbeat to keep sandbox alive. Called automatically by exec(),
        but can also be called manually during long local computations."""
        try:
            self._broker.post(f"{self._base}/heartbeat/{sandbox_id}")
        except httpx.HTTPError:
            pass  # best-effort; don't fail the caller

    def release(self, sb: SandboxHandle) -> None:
        _raise(self._broker.delete(f"{self._base}/sandboxes/{sb.id}"))

    def revoke(self, *, run_id: str | None = None) -> int:
        resp = self._broker.post(
            f"{self._base}/revoke",
            json={"run_id": run_id or self.run_id},
        )
        _raise(resp)
        return resp.json()["released"]


def _raise(resp: httpx.Response) -> None:
    if 200 <= resp.status_code < 300:
        return
    try:
        detail = resp.json().get("detail", resp.text)
    except ValueError:
        detail = resp.text
    raise BrokerError(resp.status_code, detail)
