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
IDLE_TIMEOUT_S = 2 * 60 * 60                                # 2h
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
    last_active: float


_state_lock = threading.Lock()
_sandboxes: dict[str, SandboxState] = {}


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


def _wait_ready_pod_ip(name: str) -> str:
    """Poll the Sandbox CR until Ready, then return the pod IP."""
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
                return pod.status.pod_ip
        time.sleep(1)
    raise HTTPException(504, detail=f"sandbox {name} not Ready within timeout")


# --- HTTP API ---------------------------------------------------------------

app = FastAPI()


def _check_auth(authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, detail="missing bearer token")
    if not secrets.compare_digest(authorization[len("Bearer "):], HECATON_TOKEN):
        raise HTTPException(403, detail="bad token")


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
        host = _wait_ready_pod_ip(name)
    except HTTPException:
        _delete_sandbox_cr(name)
        raise

    state = SandboxState(
        id=name, run_id=run_id, template=template,
        host=host, port=port, last_active=time.monotonic(),
    )
    with _state_lock:
        _sandboxes[name] = state

    return {"id": name, "run_id": run_id, "template": template,
            "host": host, "port": port}


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

async def _reaper() -> None:
    while True:
        await asyncio.sleep(60)
        now = time.monotonic()
        stale: list[str] = []
        with _state_lock:
            for sid, st in _sandboxes.items():
                if now - st.last_active > IDLE_TIMEOUT_S:
                    stale.append(sid)
        for sid in stale:
            try:
                _release_one(sid)
            except Exception as e:
                print(f"reaper: failed to release {sid}: {e}", flush=True)


_reaper_task: asyncio.Task | None = None


@app.on_event("startup")
async def _start_reaper() -> None:
    global _reaper_task
    _reaper_task = asyncio.create_task(_reaper())
