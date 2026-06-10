"""hecaton broker — trainer-facing control plane.

Acquires sandbox pods from the cluster on behalf of trainers, hands back
`(host, port)` so the trainer talks to the sandbox pod directly (no data
plane through this service).

Endpoints:
  POST   /register                  trainer announces itself for lifecycle tracking
  POST   /sandboxes                 acquire by template (and optional scaffold)
  POST   /heartbeat/{sandbox_id}    refresh idle timer (SDK auto-calls on every exec)
  DELETE /sandboxes/{id}            release one
  POST   /revoke                    release all sandboxes for a run_id

Authentication: bearer token shared fleet-wide. Network reachability is
gated by the tailnet ACL (only tag:trainer can reach this port).

Design: trainers reference a SandboxTemplate by name. The broker looks
up the template, inlines its `spec.podTemplate` into a fresh Sandbox CR,
and waits for the agent-sandbox controller to mark it Ready. The pod
runs in the cluster pod network; trainers reach it through a Tailscale
subnet route that advertises the pod CIDR.

If the acquire request names a scaffold, the broker also appends a
hostPath volume at `/opt/hecaton/agent-tools/<scaffold>/` and a
readOnly mount at `/opt/agent-tools` to every container in the pod
spec before creating the CR — SandboxTemplate YAML stays
scaffold-agnostic.

Lifecycle: a background reaper releases sandboxes idle for more than
2h and evicts trainers idle for more than 6h (the trainer eviction
also kicks the device off the tailnet via the Tailscale API). On
process start, the broker rehydrates its in-memory account book from
existing Sandbox CRs in the cluster, so a restart does not orphan
running sandboxes.

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
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from kubernetes import client, config
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily

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
LABEL_SCAFFOLD = "hecaton.io/scaffold"

# Marker the sandbox renderer stamps on every managed SandboxTemplate.
# The broker refuses to acquire any template not carrying it, so
# anything dropped into the cluster out-of-band (`kubectl apply` by
# hand, leftover from an earlier deploy, ...) cannot be acquired by
# trainers.
LABEL_MANAGED_BY = "hecaton.io/managed-by"
MANAGED_VALUE = "hecaton"

# Scaffold tools (R2E-Gym etc.) are staged on every host at
# SCAFFOLD_HOST_BASE/<scaffold>/, mode 0555. When a trainer asks for
# a scaffold at acquire time we layer a hostPath mount onto the pod
# spec so the tools land read+execute-only at SCAFFOLD_MOUNT, and we
# prepend that directory to PATH so the scaffold can invoke tools by
# bare name. The SandboxTemplate itself stays scaffold-agnostic.
SCAFFOLD_HOST_BASE = "/opt/hecaton/agent-tools"
SCAFFOLD_MOUNT = "/opt/agent-tools"
# Lowercase DNS-ish label matching the scaffold staging directory
# convention; also blocks path traversal (no '/', no '..', no leading dot).
_SCAFFOLD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


@dataclass
class SandboxState:
    id: str
    run_id: str
    template: str
    host: str
    port: int
    node: str
    last_active: float    # time.monotonic, for the idle reaper
    acquired_at: float    # time.time (wall-clock), for sandbox-age metrics
    scaffold: str = ""


_state_lock = threading.Lock()
_sandboxes: dict[str, SandboxState] = {}


@dataclass
class TrainerState:
    run_id: str
    device_id: str  # Tailscale device ID, reported at registration
    last_active: float


_trainer_lock = threading.Lock()
_trainers: dict[str, TrainerState] = {}  # run_id → TrainerState


# --- prometheus metrics -----------------------------------------------------
#
# Counters/histograms accumulate in the standard way. Gauges that
# reflect live state (sandboxes, age, trainers) are produced by a
# custom Collector — its collect() is called once per scrape and
# returns a fresh snapshot, so there is no clear()/rebuild race.

_METRICS = CollectorRegistry()

_M_ACQUIRES = Counter(
    "hecaton_acquires_total",
    "Sandbox acquire requests handled by the broker.",
    ["template", "scaffold", "node"],
    registry=_METRICS,
)
_M_RELEASES = Counter(
    "hecaton_releases_total",
    "Sandbox release events.",
    ["template", "reason"],  # reason: trainer | reaper | revoke
    registry=_METRICS,
)
_M_ACQUIRE_FAILS = Counter(
    "hecaton_acquire_failures_total",
    "Acquire requests that failed before returning a handle.",
    ["template", "reason"],  # reason: not_ready | invalid_scaffold | template_not_found | ...
    registry=_METRICS,
)
_M_ACQUIRE_LATENCY = Histogram(
    "hecaton_acquire_latency_seconds",
    "Wall-clock seconds from /sandboxes request entry to successful return.",
    ["template"],
    # Tuned for sandbox-pod cold-start: image pull dominates, typical
    # range is 5s (cached) to 3min (timeout). SANDBOX_READY_TIMEOUT_S
    # caps the max at 180s, so no bucket above that is reachable.
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120, 180),
    registry=_METRICS,
)


class _StateCollector:
    """Produces gauge metrics from live broker state on each scrape."""

    def collect(self):  # noqa: ANN201
        now_wall = time.time()

        sandboxes_gauge = GaugeMetricFamily(
            "hecaton_sandboxes",
            "Currently held sandboxes.",
            labels=["template", "run_id", "node", "scaffold"],
        )
        age_gauge = GaugeMetricFamily(
            "hecaton_sandbox_age_seconds",
            "Seconds since this sandbox was acquired.",
            labels=["id", "template", "run_id", "node"],
        )

        with _state_lock:
            snapshot = list(_sandboxes.values())
        for sb in snapshot:
            sandboxes_gauge.add_metric(
                [sb.template, sb.run_id, sb.node, sb.scaffold or "none"], 1,
            )
            age_gauge.add_metric(
                [sb.id, sb.template, sb.run_id, sb.node],
                now_wall - sb.acquired_at,
            )

        trainers_gauge = GaugeMetricFamily(
            "hecaton_trainers",
            "Trainers currently registered with the broker.",
        )
        with _trainer_lock:
            trainers_gauge.add_metric([], len(_trainers))

        yield sandboxes_gauge
        yield age_gauge
        yield trainers_gauge


_METRICS.register(_StateCollector())


# --- kubernetes plumbing ----------------------------------------------------

try:
    config.load_incluster_config()
except config.ConfigException:
    config.load_kube_config()

_core = client.CoreV1Api()
_custom = client.CustomObjectsApi()


def _load_template(name: str) -> dict:
    """Fetch a SandboxTemplate and return its `spec.podTemplate.spec`.

    Refuses templates without the hecaton.io/managed-by label — the
    sandbox renderer is the only sanctioned source of acquireable
    templates.
    """
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
    labels = tmpl.get("metadata", {}).get("labels") or {}
    if labels.get(LABEL_MANAGED_BY) != MANAGED_VALUE:
        # Same 404 the trainer sees for a truly missing name — we don't
        # want to leak the existence of unmanaged templates.
        raise HTTPException(404, detail=f"template {name!r} not found")
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


def _inject_scaffold(pod_spec: dict, scaffold: str) -> None:
    """Layer a scaffold's tools onto `pod_spec` in place.

    Adds a hostPath volume pointing at SCAFFOLD_HOST_BASE/<scaffold>/
    (mode 0555) and mounts it readOnly at SCAFFOLD_MOUNT in every
    container. The SandboxTemplate stays scaffold-agnostic — selection
    happens here, in the trainer-facing acquire path.

    Tools are invoked by absolute path, so we deliberately don't touch
    PATH — the sandbox image's own PATH stays intact.

    The caller must have validated `scaffold` against `_SCAFFOLD_RE`
    before getting here: the value is interpolated into a hostPath.
    """
    volumes = pod_spec.setdefault("volumes", [])
    volumes.append({
        "name": "agent-tools",
        "hostPath": {
            "path": f"{SCAFFOLD_HOST_BASE}/{scaffold}",
            "type": "Directory",
        },
    })

    for container in pod_spec.get("containers", []):
        mounts = container.setdefault("volumeMounts", [])
        mounts.append({
            "name": "agent-tools",
            "mountPath": SCAFFOLD_MOUNT,
            "readOnly": True,
        })


def _create_sandbox_cr(
    name: str, run_id: str, template: str, pod_spec: dict, scaffold: str = "",
) -> None:
    labels = {
        LABEL_OWNER: "hecaton",
        LABEL_RUN_ID: run_id,
        LABEL_TEMPLATE: template,
    }
    if scaffold:
        labels[LABEL_SCAFFOLD] = scaffold
    body = {
        "apiVersion": f"{SB_GROUP}/{SB_VERSION}",
        "kind": "Sandbox",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "labels": labels,
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
    scaffold = body.get("scaffold") or ""

    started = time.monotonic()
    try:
        if scaffold and not _SCAFFOLD_RE.match(scaffold):
            # Reject anything that could escape SCAFFOLD_HOST_BASE/<scaffold>
            # or land outside our staging convention. This is the only
            # input we splice into a hostPath, so validate strictly.
            _M_ACQUIRE_FAILS.labels(template=template, reason="invalid_scaffold").inc()
            raise HTTPException(400, detail=f"invalid scaffold name: {scaffold!r}")

        try:
            pod_spec = _load_template(template)
        except HTTPException as e:
            reason = "template_not_found" if e.status_code == 404 else "template_invalid"
            _M_ACQUIRE_FAILS.labels(template=template, reason=reason).inc()
            raise
        port = _first_container_port(pod_spec)
        if scaffold:
            _inject_scaffold(pod_spec, scaffold)

        name = f"sb-{secrets.token_hex(6)}"
        _create_sandbox_cr(name, run_id, template, pod_spec, scaffold=scaffold)

        try:
            host, node = _wait_ready_pod_ip(name)
        except HTTPException:
            _delete_sandbox_cr(name)
            _M_ACQUIRE_FAILS.labels(template=template, reason="not_ready").inc()
            raise

        now = time.monotonic()
        state = SandboxState(
            id=name, run_id=run_id, template=template,
            host=host, port=port, node=node,
            last_active=now, acquired_at=time.time(),
            scaffold=scaffold,
        )
        with _state_lock:
            _sandboxes[name] = state
        with _trainer_lock:
            tr = _trainers.get(run_id)
            if tr:
                tr.last_active = now

        _M_ACQUIRES.labels(
            template=template,
            scaffold=scaffold or "none",
            node=node or "unknown",
        ).inc()
        _M_ACQUIRE_LATENCY.labels(template=template).observe(time.monotonic() - started)

        return {"id": name, "run_id": run_id, "template": template,
                "scaffold": scaffold,
                "host": host, "port": port, "node": node}
    except HTTPException:
        raise
    except Exception:
        # Anything we didn't explicitly classify still counts as a
        # failure — surface it as "internal" so dashboards alert.
        _M_ACQUIRE_FAILS.labels(template=template, reason="internal").inc()
        raise


@app.delete("/sandboxes/{sandbox_id}", status_code=204)
def release(sandbox_id: str, _: None = Depends(_check_auth)) -> None:
    _release_one(sandbox_id, reason="trainer")


@app.post("/revoke")
def revoke(body: dict, _: None = Depends(_check_auth)) -> dict:
    run_id = body["run_id"]
    items = _list_sandbox_crs(run_id=run_id)
    for it in items:
        _release_one(it["metadata"]["name"], reason="revoke")
    return {"released": len(items)}


def _release_one(sandbox_id: str, *, reason: str) -> None:
    with _state_lock:
        st = _sandboxes.pop(sandbox_id, None)
    try:
        _delete_sandbox_cr(sandbox_id)
    except client.exceptions.ApiException as exc:
        if exc.status != 404:
            raise
    template = st.template if st else "unknown"
    _M_RELEASES.labels(template=template, reason=reason).inc()


# --- prometheus scrape endpoint ---------------------------------------------
#
# Not bearer-gated: the broker is only reachable on the tailnet (ACL
# pins reachability to tag:trainer and tag:fleet-ops), and Prometheus
# inside the cluster is just another tailnet-equivalent peer here.


@app.get("/metrics")
def prometheus_metrics() -> PlainTextResponse:
    # _StateCollector.collect() produces the gauge snapshot on demand;
    # counters/histograms are accumulated in the standard way.
    return PlainTextResponse(
        generate_latest(_METRICS).decode(),
        media_type=CONTENT_TYPE_LATEST,
    )


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

        stale_sandboxes: list[str] = []
        with _state_lock:
            for sid, st in _sandboxes.items():
                if now - st.last_active > SANDBOX_IDLE_TIMEOUT_S:
                    stale_sandboxes.append(sid)
        for sid in stale_sandboxes:
            try:
                _release_one(sid, reason="reaper")
                print(f"reaper: released idle sandbox {sid}", flush=True)
            except Exception as e:
                print(f"reaper: failed to release {sid}: {e}", flush=True)

        stale_trainers: list[str] = []
        with _trainer_lock:
            for run_id, tr in _trainers.items():
                if now - tr.last_active > TRAINER_IDLE_TIMEOUT_S:
                    stale_trainers.append(run_id)
        for run_id in stale_trainers:
            try:
                items = _list_sandbox_crs(run_id=run_id)
                for it in items:
                    _release_one(it["metadata"]["name"], reason="reaper")
                with _trainer_lock:
                    tr = _trainers.pop(run_id, None)
                if tr:
                    _kick_trainer_device(tr.device_id)
                print(f"reaper: evicted idle trainer {run_id}", flush=True)
            except Exception as e:
                print(f"reaper: failed to evict trainer {run_id}: {e}", flush=True)


_reaper_task: asyncio.Task | None = None


def _rehydrate_from_cluster() -> None:
    """Rebuild `_sandboxes` from Sandbox CRs in the cluster.

    The broker's account book is in-memory: a pod restart (image bump,
    OOM, deployment apply) loses every entry, which leaves Sandbox CRs
    in the cluster as orphans the idle reaper never touches. The cluster
    is the durable source of truth, so on startup we list every
    hecaton-owned Sandbox and reconstruct state from it.

    Everything we need is on the CR — labels carry run_id and template,
    the inlined podTemplate carries the container port, the live pod
    carries IP and node. The single field we cannot recover is
    `last_active`, since we never persisted it; we initialize it to
    "now" so the reaper gives each adopted sandbox a fresh idle window
    rather than reaping it immediately. Worst case: a truly idle
    sandbox lingers up to SANDBOX_IDLE_TIMEOUT_S extra after a restart.
    """
    try:
        resp = _custom.list_namespaced_custom_object(
            group=SB_GROUP, version=SB_VERSION,
            namespace=NAMESPACE, plural=SB_PLURAL,
            label_selector=f"{LABEL_OWNER}=hecaton",
        )
    except client.exceptions.ApiException as exc:
        print(f"rehydrate: list failed: {exc}", flush=True)
        return

    now = time.monotonic()
    adopted = 0
    for sb in resp.get("items", []):
        meta = sb.get("metadata", {})
        name = meta.get("name") or ""
        labels = meta.get("labels", {}) or {}
        run_id = labels.get(LABEL_RUN_ID, "")
        template = labels.get(LABEL_TEMPLATE, "")
        if not name or not run_id:
            continue

        # Port is inlined on the Sandbox itself (we put it there at
        # acquire time), so we don't need to round-trip to the template.
        pod_spec = (sb.get("spec", {}) or {}).get("podTemplate", {}).get("spec") or {}
        try:
            port = _first_container_port(pod_spec)
        except HTTPException:
            print(f"rehydrate: skip {name} (no container port)", flush=True)
            continue

        # Pod IP + node come from the live pod. If the pod isn't there
        # or has no IP yet, skip — the controller will reconcile it and
        # we'll pick it up on a future broker restart.
        status = sb.get("status", {}) or {}
        pod_name = status.get("podName") or name
        try:
            pod = _core.read_namespaced_pod(pod_name, NAMESPACE)
        except client.exceptions.ApiException:
            print(f"rehydrate: skip {name} (pod {pod_name} missing)", flush=True)
            continue
        host = pod.status.pod_ip or ""
        if not host:
            print(f"rehydrate: skip {name} (no pod IP yet)", flush=True)
            continue
        node = pod.spec.node_name or ""

        # k8s sets creationTimestamp on every CR (RFC3339 UTC). Use
        # it as the acquire wall-clock so sandbox-age metrics survive
        # broker restarts.
        acquired_at = datetime.fromisoformat(meta["creationTimestamp"]).timestamp()

        with _state_lock:
            _sandboxes[name] = SandboxState(
                id=name, run_id=run_id, template=template,
                host=host, port=port, node=node,
                last_active=now, acquired_at=acquired_at,
                scaffold=labels.get(LABEL_SCAFFOLD, ""),
            )
        with _trainer_lock:
            # Adopt the owning trainer too so /metrics and the idle
            # reaper see it. device_id is unknown here; the trainer's
            # next /register call will fill it in.
            _trainers.setdefault(
                run_id,
                TrainerState(run_id=run_id, device_id="", last_active=now),
            )
        adopted += 1

    print(f"rehydrate: adopted {adopted} sandbox(es) from cluster", flush=True)


@app.on_event("startup")
async def _start_reaper() -> None:
    global _reaper_task
    _rehydrate_from_cluster()
    _reaper_task = asyncio.create_task(_reaper())
