"""hecaton broker — trainer-facing control plane.

Acquires sandbox pods from the cluster on behalf of trainers, hands back
`(host, port)` so the trainer talks to the sandbox pod directly (no data
plane through this service). Tracks idle time per sandbox and reaps
anything quiet for more than 2h.

Endpoints (see platform/broker/API.md):
  POST   /sandboxes        acquire by template name
  DELETE /sandboxes/{id}   release one
  POST   /revoke           release all sandboxes for a run_id

Authentication: bearer token shared fleet-wide. Network reachability is
gated by the tailnet ACL (only tag:trainer can reach this port).

Design: trainers reference a SandboxTemplate by name. The broker looks
up the template, inlines its `spec.podTemplate` into a fresh Sandbox CR,
and waits for the agent-sandbox controller to mark it Ready. The pod
runs in the cluster pod network; trainers reach it through a Tailscale
subnet route that advertises the pod CIDR.

Why we do not use the upstream `k8s_agent_sandbox` Python SDK on the
broker side either:
  * its `SandboxClient.create_sandbox` is SandboxClaim+WarmPool centric;
    with one Sandbox per task we always cold-create.
  * its `K8sHelper` has no `create_sandbox`, so we would end up at
    `CustomObjectsApi` regardless.
Keep it simple: talk to the apiserver directly. The Sandbox /
SandboxTemplate CR schemas are the integration contract.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import threading
import time
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from kubernetes import client, config

# --- configuration ----------------------------------------------------------

NAMESPACE = os.environ.get("HECATON_NAMESPACE", "hecaton-sandboxes")
HECATON_TOKEN = os.environ["HECATON_TOKEN"]
TAILSCALE_API_KEY = os.environ.get("TAILSCALE_API_KEY", "")
TAILNET = os.environ.get("TAILSCALE_TAILNET", "-")  # "-" = default tailnet
SANDBOX_IDLE_TIMEOUT_S = 2 * 60 * 60                        # 2h
TRAINER_IDLE_TIMEOUT_S = 6 * 60 * 60                         # 6h
SANDBOX_READY_TIMEOUT_S = 180

# CR coordinates. Versions follow whatever the pinned agent-sandbox
# release ships in its manifest.yaml / extensions.yaml — currently
# v1alpha1 for both groups.
SB_GROUP = "agents.x-k8s.io"
SB_VERSION = "v1alpha1"
SB_PLURAL = "sandboxes"

TMPL_GROUP = "extensions.agents.x-k8s.io"
TMPL_VERSION = "v1alpha1"
TMPL_PLURAL = "sandboxtemplates"

# Labels we put on every Sandbox we create.
LABEL_OWNER = "hecaton.io/owner"
LABEL_RUN_ID = "hecaton.io/run-id"
LABEL_TEMPLATE = "hecaton.io/template"


@dataclass
class SandboxState:
    id: str
    run_id: str
    template: str
    host: str
    port: int
    node: str
    last_active: float


_state_lock = threading.Lock()
_sandboxes: dict[str, SandboxState] = {}


@dataclass
class TrainerState:
    run_id: str
    device_id: str  # Tailscale device ID, reported at registration
    last_active: float


_trainer_lock = threading.Lock()
_trainers: dict[str, TrainerState] = {}  # run_id → TrainerState


# --- kubernetes plumbing ----------------------------------------------------

try:
    config.load_incluster_config()
except config.ConfigException:
    config.load_kube_config()

_core = client.CoreV1Api()
_custom = client.CustomObjectsApi()


def _load_template(name: str) -> dict:
    """Fetch a SandboxTemplate and return its `spec.podTemplate.spec`."""
    try:
        tmpl = _custom.get_namespaced_custom_object(
            group=TMPL_GROUP, version=TMPL_VERSION,
            namespace=NAMESPACE, plural=TMPL_PLURAL,
            name=name,
        )
    except client.exceptions.ApiException as exc:
        if exc.status == 404:
            raise HTTPException(404, detail=f"template {name!r} not found") from exc
        raise
    pod_spec = tmpl.get("spec", {}).get("podTemplate", {}).get("spec")
    if not pod_spec:
        raise HTTPException(500, detail=f"template {name!r} has no podTemplate.spec")
    return pod_spec


def _first_container_port(pod_spec: dict) -> int:
    """Return the first container's first port number, for trainer to dial."""
    containers = pod_spec.get("containers", [])
    if not containers:
        raise HTTPException(500, detail="template has no containers")
    ports = containers[0].get("ports", [])
    if not ports:
        raise HTTPException(500, detail="template's first container has no ports")
    return int(ports[0]["containerPort"])


def _create_sandbox_cr(name: str, run_id: str, template: str, pod_spec: dict) -> None:
    body = {
        "apiVersion": f"{SB_GROUP}/{SB_VERSION}",
        "kind": "Sandbox",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "labels": {
                LABEL_OWNER: "hecaton",
                LABEL_RUN_ID: run_id,
                LABEL_TEMPLATE: template,
            },
        },
        "spec": {"podTemplate": {"spec": pod_spec}},
    }
    _custom.create_namespaced_custom_object(
        group=SB_GROUP, version=SB_VERSION,
        namespace=NAMESPACE, plural=SB_PLURAL,
        body=body,
    )


def _delete_sandbox_cr(name: str) -> None:
    _custom.delete_namespaced_custom_object(
        group=SB_GROUP, version=SB_VERSION,
        namespace=NAMESPACE, plural=SB_PLURAL,
        name=name,
    )


def _list_sandbox_crs(run_id: str) -> list[dict]:
    resp = _custom.list_namespaced_custom_object(
        group=SB_GROUP, version=SB_VERSION,
        namespace=NAMESPACE, plural=SB_PLURAL,
        label_selector=f"{LABEL_OWNER}=hecaton,{LABEL_RUN_ID}={run_id}",
    )
    return resp.get("items", [])


def _wait_ready_pod_ip(name: str) -> tuple[str, str]:
    """Poll the Sandbox CR until Ready, then return (pod IP, node name)."""
    deadline = time.monotonic() + SANDBOX_READY_TIMEOUT_S
    while time.monotonic() < deadline:
        sb = _custom.get_namespaced_custom_object(
            group=SB_GROUP, version=SB_VERSION,
            namespace=NAMESPACE, plural=SB_PLURAL,
            name=name,
        )
        status = sb.get("status", {})
        for cond in status.get("conditions", []):
            if cond.get("type") == "Ready" and cond.get("status") == "True":
                pod_name = status.get("podName") or name
                pod = _core.read_namespaced_pod(pod_name, NAMESPACE)
                return pod.status.pod_ip, pod.spec.node_name or ""
        time.sleep(1)
    raise HTTPException(504, detail=f"sandbox {name} not Ready within timeout")


# --- HTTP API ---------------------------------------------------------------

app = FastAPI()


def _check_auth(authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, detail="missing bearer token")
    if not secrets.compare_digest(authorization[len("Bearer "):], HECATON_TOKEN):
        raise HTTPException(403, detail="bad token")


@app.post("/register", status_code=201)
async def register_trainer(req: Request, _: None = Depends(_check_auth)) -> dict:
    """Register a trainer and its Tailscale device for lifecycle tracking."""
    body = await req.json()
    run_id = body["run_id"]
    device_id = body.get("device_id", "")
    with _trainer_lock:
        _trainers[run_id] = TrainerState(
            run_id=run_id, device_id=device_id, last_active=time.monotonic(),
        )
    return {"registered": True}


@app.post("/heartbeat/{sandbox_id}", status_code=204)
def heartbeat(sandbox_id: str, _: None = Depends(_check_auth)) -> None:
    """Update last-active timestamp for a sandbox (and its trainer)."""
    with _state_lock:
        st = _sandboxes.get(sandbox_id)
        if st:
            st.last_active = time.monotonic()
            # Also refresh the trainer's last-active
            with _trainer_lock:
                tr = _trainers.get(st.run_id)
                if tr:
                    tr.last_active = time.monotonic()


@app.post("/sandboxes", status_code=201)
async def acquire(req: Request, _: None = Depends(_check_auth)) -> dict:
    body = await req.json()
    run_id = body["run_id"]
    template = body["template"]

    pod_spec = _load_template(template)
    port = _first_container_port(pod_spec)

    name = f"sb-{secrets.token_hex(6)}"
    _create_sandbox_cr(name, run_id, template, pod_spec)

    try:
        host, node = _wait_ready_pod_ip(name)
    except HTTPException:
        _delete_sandbox_cr(name)
        raise

    now = time.monotonic()
    state = SandboxState(
        id=name, run_id=run_id, template=template,
        host=host, port=port, node=node, last_active=now,
    )
    with _state_lock:
        _sandboxes[name] = state
    with _trainer_lock:
        tr = _trainers.get(run_id)
        if tr:
            tr.last_active = now

    return {"id": name, "run_id": run_id, "template": template,
            "host": host, "port": port, "node": node}


@app.delete("/sandboxes/{sandbox_id}", status_code=204)
def release(sandbox_id: str, _: None = Depends(_check_auth)) -> None:
    _release_one(sandbox_id)


@app.post("/revoke")
def revoke(body: dict, _: None = Depends(_check_auth)) -> dict:
    run_id = body["run_id"]
    items = _list_sandbox_crs(run_id=run_id)
    for it in items:
        _release_one(it["metadata"]["name"])
    return {"released": len(items)}


def _release_one(sandbox_id: str) -> None:
    _delete_sandbox_cr(sandbox_id)
    with _state_lock:
        _sandboxes.pop(sandbox_id, None)


# --- idle reaper ------------------------------------------------------------


def _kick_trainer_device(device_id: str) -> None:
    """Remove a trainer node from the tailnet via Tailscale API."""
    if not TAILSCALE_API_KEY or not device_id:
        return
    try:
        import requests as _req
        resp = _req.delete(
            f"https://api.tailscale.com/api/v2/device/{device_id}",
            headers={"Authorization": f"Bearer {TAILSCALE_API_KEY}"},
            timeout=10,
        )
        if resp.status_code < 300:
            print(f"reaper: kicked trainer device {device_id}", flush=True)
        else:
            print(f"reaper: failed to kick device {device_id}: HTTP {resp.status_code}", flush=True)
    except Exception as e:
        print(f"reaper: tailscale API error for {device_id}: {e}", flush=True)


async def _reaper() -> None:
    while True:
        await asyncio.sleep(60)
        now = time.monotonic()

        # --- sandbox idle reap (2h) ---
        stale_sandboxes: list[str] = []
        with _state_lock:
            for sid, st in _sandboxes.items():
                if now - st.last_active > SANDBOX_IDLE_TIMEOUT_S:
                    stale_sandboxes.append(sid)
        for sid in stale_sandboxes:
            try:
                _release_one(sid)
                print(f"reaper: released idle sandbox {sid}", flush=True)
            except Exception as e:
                print(f"reaper: failed to release {sid}: {e}", flush=True)

        # --- trainer idle reap (6h) ---
        stale_trainers: list[str] = []
        with _trainer_lock:
            for run_id, tr in _trainers.items():
                if now - tr.last_active > TRAINER_IDLE_TIMEOUT_S:
                    stale_trainers.append(run_id)
        for run_id in stale_trainers:
            try:
                # Release all sandboxes for this trainer
                items = _list_sandbox_crs(run_id=run_id)
                for it in items:
                    _release_one(it["metadata"]["name"])
                # Kick from tailnet
                with _trainer_lock:
                    tr = _trainers.pop(run_id, None)
                if tr:
                    _kick_trainer_device(tr.device_id)
                print(f"reaper: evicted idle trainer {run_id}", flush=True)
            except Exception as e:
                print(f"reaper: failed to evict trainer {run_id}: {e}", flush=True)


_reaper_task: asyncio.Task | None = None


@app.on_event("startup")
async def _start_reaper() -> None:
    global _reaper_task
    _reaper_task = asyncio.create_task(_reaper())
