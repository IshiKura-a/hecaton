"""Install kube-prometheus-stack + per-vendor GPU exporters.

Three sub-steps with separate failure domains so iteration on one
doesn't pay for the others:

  core        helm upgrade kube-prometheus-stack          (~80s, slow)
  dashboards  reapply hecaton-dashboards ConfigMap        (~1s, fast)
  exporters   label nodes + apply dcgm / amd DaemonSets   (~3s, fast)

Invoke with sub-step name (default 'all' for full install used by
bootstrap/install.sh).
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from . import die, hecaton_root, inventory, log, remote, versions
from .k8s import (
    helm,
    helm_repo_present,
    kubectl_apply_stdin,
    kubectl_capture,
)

NS = "monitoring"
REPO_NAME = "prometheus-community"
REPO_URL = "https://prometheus-community.github.io/helm-charts"
CHART = "prometheus-community/kube-prometheus-stack"


def install_chart(chart_version: str) -> None:
    if not helm_repo_present(REPO_NAME):
        log(f"adding helm repo: {REPO_NAME}")
        helm("repo", "add", REPO_NAME, REPO_URL)
    helm("repo", "update", REPO_NAME)

    values = hecaton_root() / "platform" / "monitoring" / "values.yaml"
    log(f"installing kube-prometheus-stack {chart_version} into ns={NS}")
    helm(
        "upgrade", "--install", "kube-prometheus-stack", CHART,
        "--version", chart_version,
        "--namespace", NS, "--create-namespace",
        "--values", str(values),
        "--wait", "--timeout", "5m",
    )


def apply_dashboards() -> None:
    """Hot-load dashboards via grafana sidecar."""
    d = hecaton_root() / "platform" / "monitoring" / "dashboards"
    jsons = sorted(d.glob("*.json")) if d.is_dir() else []
    if not jsons:
        die(f"no dashboard json found in {d}")
    log("applying dashboards ConfigMap")
    with tempfile.TemporaryDirectory(prefix="hecaton-dashboards.") as td:
        rendered: list[Path] = []
        for j in jsons:
            out = Path(td) / j.name
            out.write_text(render_dashboard(j))
            rendered.append(out)

        # Build the ConfigMap with kubectl --dry-run, then add the label,
        # then apply. Same pipeline as the bash version, just via stdin.
        cm = kubectl_capture(
            "create", "configmap", "hecaton-dashboards",
            "-n", NS,
            *[f"--from-file={j}" for j in rendered],
            "--dry-run=client", "-o", "yaml",
        )
        labelled = kubectl_capture(
            "label", "--local", "-f", "-",
            "grafana_dashboard=1",
            "--dry-run=client", "-o", "yaml",
            stdin=cm,
        )
        kubectl_apply_stdin(labelled)


def render_dashboard(path: Path) -> str:
    """Render dashboard JSON, injecting host-specific settings where needed."""
    if path.name not in {"hecaton-nodes.json", "hecaton-capacity.json"}:
        return path.read_text()

    data = json.loads(path.read_text())
    hosts = monitoring_hosts()
    render_node_variable(data, hosts)
    if path.name == "hecaton-nodes.json":
        render_nodes_dashboard(data, hosts)
    elif path.name == "hecaton-capacity.json":
        render_capacity_dashboard(data)
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


@dataclass(frozen=True)
class MonitoringHost:
    inventory_name: str
    ssh_host: str
    k8s_node: str
    os_nodename: str
    node_exporter_instance: str
    disk_root: str
    disk_mountpoint: str


@cache
def monitoring_hosts() -> list[MonitoringHost]:
    log("resolving monitoring host names and disk mountpoints")
    hosts = inventory.load_hosts()
    if not hosts:
        die("no hosts in inventory")
    out: list[MonitoringHost] = []
    for h in hosts:
        ip = remote.tailnet_ip(h)
        out.append(
            MonitoringHost(
                inventory_name=h.name,
                ssh_host=h.ssh_host,
                k8s_node=remote.k8s_node_name(h),
                os_nodename=remote.os_nodename(h),
                node_exporter_instance=f"{ip}:9100",
                disk_root=h.disk_root,
                disk_mountpoint=remote.disk_mountpoint(h),
            )
        )
    return out


def render_node_variable(data: dict, hosts: list[MonitoringHost]) -> None:
    """Make dashboard node filters use Kubernetes node names, never OS/SSH names."""
    names = ",".join(h.k8s_node for h in hosts)
    found = False
    for var in data.get("templating", {}).get("list", []):
        if var.get("name") != "node":
            continue
        found = True
        var["type"] = "custom"
        var.pop("datasource", None)
        var["query"] = names
        var["current"] = {"selected": True, "text": "All", "value": "$__all"}
        var["options"] = []
        var["multi"] = True
        var["includeAll"] = True
        var["allValue"] = ".*"
        var["refresh"] = 0
    if not found:
        die("dashboard is missing required $node variable")


def render_nodes_dashboard(data: dict, hosts: list[MonitoringHost]) -> None:
    exprs = node_dashboard_exprs(hosts)
    seen = {
        "cpu": False,
        "memory": False,
        "disk": False,
        "network": False,
        "gpu_util_nvidia": False,
        "gpu_mem_nvidia": False,
    }
    for panel in data.get("panels", []):
        title = panel.get("title")
        targets = panel.get("targets") or []
        if title == "Node CPU usage (cores)" and targets:
            seen["cpu"] = True
            targets[0]["expr"] = exprs["cpu"]
            targets[0]["legendFormat"] = "{{k8s_node}}"
        elif title == "Node memory used (bytes)" and targets:
            seen["memory"] = True
            targets[0]["expr"] = exprs["memory"]
            targets[0]["legendFormat"] = "{{k8s_node}}"
        elif title == "GPU utilization (NVIDIA / AMD)" and targets:
            seen["gpu_util_nvidia"] = True
            targets[0]["expr"] = exprs["gpu_util_nvidia"]
            targets[0]["legendFormat"] = "{{node}} gpu{{gpu}}"
        elif title in {"Node disk usage (rootfs %)", "Sandbox disk usage (%)"} and targets:
            seen["disk"] = True
            panel["title"] = "Sandbox disk usage (%)"
            panel["description"] = (
                "Uses hosts.yaml disk_root per Kubernetes node; disk_root defaults to /. "
                "Phase 27 renders the per-node mountpoint query."
            )
            targets[0]["expr"] = exprs["disk"]
            targets[0]["legendFormat"] = "{{k8s_node}} {{mountpoint}}"
        elif title == "Node network throughput (bytes/s)" and len(targets) >= 2:
            seen["network"] = True
            targets[0]["expr"] = exprs["network_rx"]
            targets[0]["legendFormat"] = "{{k8s_node}} rx"
            targets[1]["expr"] = exprs["network_tx"]
            targets[1]["legendFormat"] = "{{k8s_node}} tx"
        elif title == "GPU memory used (NVIDIA)" and targets:
            seen["gpu_mem_nvidia"] = True
            targets[0]["expr"] = exprs["gpu_mem_nvidia"]
            targets[0]["legendFormat"] = "{{node}} gpu{{gpu}}"
    missing = [name for name, ok in seen.items() if not ok]
    if missing:
        die(f"hecaton-nodes dashboard is missing required panel(s): {', '.join(missing)}")


def render_capacity_dashboard(data: dict) -> None:
    """Keep capacity panels on Kubernetes node names, matching the $node variable."""
    replacements = {
        'sum by (node) (hecaton_sandboxes{template=~"$template"})':
            'sum by (node) '
            '(hecaton_sandboxes{template=~"$template",node=~"${node:regex}"})',
        'sum by (node) (kube_node_status_allocatable{resource="nvidia_com_gpu"})':
            'sum by (node) '
            '(kube_node_status_allocatable{resource="nvidia_com_gpu",'
            'node=~"${node:regex}"})',
        (
            'sum by (node) '
            '(kube_pod_container_resource_requests{resource="nvidia_com_gpu"}) or '
            '(sum by (node) '
            '(kube_node_status_allocatable{resource="nvidia_com_gpu"}) * 0)'
        ): (
            'sum by (node) '
            '(kube_pod_container_resource_requests{resource="nvidia_com_gpu",'
            'node=~"${node:regex}"}) or '
            '(sum by (node) '
            '(kube_node_status_allocatable{resource="nvidia_com_gpu",'
            'node=~"${node:regex}"}) * 0)'
        ),
    }
    seen = set()
    for panel in data.get("panels", []):
        for target in panel.get("targets") or []:
            expr = target.get("expr")
            if expr in replacements:
                seen.add(expr)
                target["expr"] = replacements[expr]
    missing = [expr for expr in replacements if expr not in seen]
    if missing:
        die("hecaton-capacity dashboard is missing required node-filter target(s)")


def node_dashboard_exprs(hosts: list[MonitoringHost]) -> dict[str, str]:
    return {
        "cpu": prom_or(cpu_expr(h) for h in hosts),
        "memory": prom_or(memory_expr(h) for h in hosts),
        "disk": prom_or(disk_expr(h) for h in hosts),
        "network_rx": prom_or(network_expr(h, "receive") for h in hosts),
        "network_tx": prom_or(network_expr(h, "transmit") for h in hosts),
        "gpu_util_nvidia": (
            'DCGM_FI_DEV_GPU_UTIL * on(pod) group_left(node) '
            'kube_pod_info{namespace="monitoring",node=~"${node:regex}"}'
        ),
        "gpu_mem_nvidia": (
            'DCGM_FI_DEV_FB_USED * 1024 * 1024 * on(pod) group_left(node) '
            'kube_pod_info{namespace="monitoring",node=~"${node:regex}"}'
        ),
    }


def cpu_expr(h: MonitoringHost) -> str:
    instance = prom_label(h.node_exporter_instance)
    metric = (
        'sum by (instance) '
        f'(rate(node_cpu_seconds_total{{mode!="idle",instance="{instance}"}}[5m]))'
    )
    return with_k8s_node_label(f"{metric} * {node_exporter_filter(h)}", h)


def memory_expr(h: MonitoringHost) -> str:
    metric = (
        f'(node_memory_MemTotal_bytes{{instance="{prom_label(h.node_exporter_instance)}"}} - '
        f'node_memory_MemAvailable_bytes{{instance="{prom_label(h.node_exporter_instance)}"}})'
    )
    return with_k8s_node_label(f"{metric} * {node_exporter_filter(h)}", h)


def disk_expr(h: MonitoringHost) -> str:
    fs = (
        f'instance="{prom_label(h.node_exporter_instance)}",'
        f'mountpoint="{prom_label(h.disk_mountpoint)}",'
        'fstype!~"tmpfs|overlay"'
    )
    metric = (
        "(100 * (1 - "
        f"node_filesystem_avail_bytes{{{fs}}} / "
        f"node_filesystem_size_bytes{{{fs}}}"
        "))"
    )
    return with_k8s_node_label(f"{metric} * {node_exporter_filter(h)}", h)


def network_expr(h: MonitoringHost, direction: str) -> str:
    metric = (
        f'sum by (instance) (rate(node_network_{direction}_bytes_total{{'
        f'instance="{prom_label(h.node_exporter_instance)}",'
        'device!~"lo|veth.*|docker.*|cni.*|flannel.*|tailscale.*|br-.*"'
        '}[5m]))'
    )
    return with_k8s_node_label(f"{metric} * {node_exporter_filter(h)}", h)


def node_exporter_filter(h: MonitoringHost) -> str:
    return (
        "on(instance) group_left(nodename) "
        f'node_uname_info{{instance="{prom_label(h.node_exporter_instance)}",'
        f'nodename=~"(?i)^({prom_regex(h.os_nodename)})$",'
        'nodename=~"(?i)^(${node:regex})$"}'
    )


def with_k8s_node_label(expr: str, h: MonitoringHost) -> str:
    replacement = prom_replacement(h.k8s_node)
    return f'label_replace(({expr}), "k8s_node", "{replacement}", "instance", ".*")'


def prom_or(parts) -> str:
    values = [p for p in parts if p]
    if not values:
        die("dashboard expression has no hosts")
    return "\nor\n".join(values)


def prom_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def prom_regex(value: str) -> str:
    return prom_label(re.escape(value))


def prom_replacement(value: str) -> str:
    return value.replace("\\", "\\\\").replace("$", "$$")


# --- exporter manifests ----------------------------------------------------


def dcgm_manifest(image_tag: str) -> str:
    return f"""\
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: dcgm-exporter
  labels: {{ app: dcgm-exporter }}
spec:
  selector:
    matchLabels: {{ app: dcgm-exporter }}
  template:
    metadata:
      labels: {{ app: dcgm-exporter }}
    spec:
      nodeSelector:
        hecaton.io/gpu-vendor: nvidia
      tolerations:
        - operator: Exists
      # Without runtimeClassName + NVIDIA_VISIBLE_DEVICES the pod
      # starts but NVML can't see the driver, so the /metrics
      # endpoint returns 0 series. Same shape as the nvidia
      # device-plugin DaemonSet in phase 22.
      runtimeClassName: nvidia
      containers:
        - name: dcgm-exporter
          image: nvcr.io/nvidia/k8s/dcgm-exporter:{image_tag}
          args: ["-f", "/etc/dcgm-exporter/dcp-metrics-included.csv"]
          env:
            - {{ name: NVIDIA_VISIBLE_DEVICES, value: "all" }}
            - {{ name: NVIDIA_DRIVER_CAPABILITIES, value: "all" }}
          ports:
            - {{ name: metrics, containerPort: 9400 }}
          securityContext:
            runAsNonRoot: false
            runAsUser: 0
            capabilities: {{ add: ["SYS_ADMIN"] }}
---
apiVersion: v1
kind: Service
metadata:
  name: dcgm-exporter
  labels: {{ app: dcgm-exporter, release: kube-prometheus-stack }}
spec:
  selector: {{ app: dcgm-exporter }}
  ports:
    - {{ name: metrics, port: 9400, targetPort: 9400 }}
---
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: dcgm-exporter
  labels: {{ release: kube-prometheus-stack }}
spec:
  selector:
    matchLabels: {{ app: dcgm-exporter }}
  endpoints:
    - port: metrics
      interval: 15s
"""


def amd_smi_manifest(image_tag: str) -> str:
    return f"""\
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: amd-smi-exporter
  labels: {{ app: amd-smi-exporter }}
spec:
  selector:
    matchLabels: {{ app: amd-smi-exporter }}
  template:
    metadata:
      labels: {{ app: amd-smi-exporter }}
    spec:
      nodeSelector:
        hecaton.io/gpu-vendor: amd
      tolerations:
        - operator: Exists
      hostNetwork: true
      containers:
        - name: amd-smi-exporter
          image: rocm/amd-smi-exporter:{image_tag}
          ports:
            - {{ name: metrics, containerPort: 2021 }}
          securityContext:
            runAsNonRoot: false
            runAsUser: 0
          volumeMounts:
            - {{ name: kfd, mountPath: /dev/kfd }}
            - {{ name: dri, mountPath: /dev/dri }}
      volumes:
        - {{ name: kfd, hostPath: {{ path: /dev/kfd }} }}
        - {{ name: dri, hostPath: {{ path: /dev/dri }} }}
---
apiVersion: v1
kind: Service
metadata:
  name: amd-smi-exporter
  labels: {{ app: amd-smi-exporter }}
spec:
  selector: {{ app: amd-smi-exporter }}
  ports:
    - {{ name: metrics, port: 2021, targetPort: 2021 }}
---
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: amd-smi-exporter
  labels: {{ release: kube-prometheus-stack }}
spec:
  selector:
    matchLabels: {{ app: amd-smi-exporter }}
  endpoints:
    - port: metrics
      interval: 15s
"""


def agent_sandbox_controller_servicemonitor() -> str:
    """ServiceMonitor pointing at the agent-sandbox controller's /metrics."""
    return """\
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: agent-sandbox-controller
  labels: { release: kube-prometheus-stack }
spec:
  namespaceSelector:
    matchNames: [agent-sandbox-system]
  selector:
    matchLabels: { app: agent-sandbox-controller }
  endpoints:
    - port: metrics
      interval: 30s
"""


# --- sub-steps -------------------------------------------------------------


def step_core() -> None:
    v = versions.load("monitoring-version.sh")
    chart_v = v.get("KUBE_PROM_STACK_VERSION") or die("KUBE_PROM_STACK_VERSION not set")
    install_chart(chart_v)


def step_dashboards() -> None:
    apply_dashboards()


def step_exporters() -> None:
    """Probe hosts, label nodes, apply vendor-specific exporter manifests.

    Requires phase 22 (RuntimeClass/nvidia) for dcgm to actually see
    GPUs; if 22 hasn't run, dcgm pods schedule but emit no data.
    """
    v = versions.load("monitoring-version.sh")
    dcgm_v = v.get("DCGM_EXPORTER_VERSION") or die("DCGM_EXPORTER_VERSION not set")
    amd_v = v.get("AMD_SMI_EXPORTER_VERSION") or die("AMD_SMI_EXPORTER_VERSION not set")

    hosts = inventory.load_hosts()
    remote.warm(hosts, what="gpu vendor + k8s node name")

    log("labeling nodes by gpu_vendor (autodetected via lspci)")
    by_vendor = remote.label_nodes_by_vendor(hosts)

    if by_vendor[remote.Vendor.NVIDIA]:
        log(f"deploying dcgm-exporter {dcgm_v}")
        kubectl_apply_stdin(dcgm_manifest(dcgm_v), namespace=NS)
    else:
        log("no nvidia hosts in inventory; skipping dcgm-exporter")

    if by_vendor[remote.Vendor.AMD]:
        log(f"deploying amd-smi-exporter {amd_v}")
        kubectl_apply_stdin(amd_smi_manifest(amd_v), namespace=NS)
    else:
        log("no amd hosts in inventory; skipping amd-smi-exporter")

    log("registering agent-sandbox controller scrape target")
    kubectl_apply_stdin(agent_sandbox_controller_servicemonitor(), namespace=NS)


def print_url() -> None:
    server_ip = kubectl_capture(
        "get", "nodes",
        "-l", "node-role.kubernetes.io/control-plane=true",
        "-o", "jsonpath={.items[0].status.addresses[?(@.type=='InternalIP')].address}",
    ).strip()
    if not server_ip:
        die("could not resolve control-plane InternalIP for Grafana URL")
    grafana_url = f"http://{server_ip}:30080"
    log("")
    log("monitoring resources applied.")
    log(f"  grafana:   {grafana_url}  (admin / admin)")
    log("  dashboards (under 'hecaton' tag):")
    log("    Hecaton — Capacity            (GPU headroom, fleet density)")
    log("    Hecaton — Acquire Health      (latency, failure reasons)")
    log("    Hecaton — Sandbox Lifecycle   (pod phases, restarts, controller)")
    log("    Hecaton — Nodes               (CPU, mem, disk, net, GPU)")


# --- main ------------------------------------------------------------------


STEPS = {
    "core": step_core,
    "dashboards": step_dashboards,
    "exporters": step_exporters,
}


def main(argv: list[str]) -> int:
    target = argv[0] if argv else "all"

    if target == "all":
        step_core()
        step_dashboards()
        step_exporters()
        print_url()
        return 0

    fn = STEPS.get(target)
    if fn is None:
        die(f"unknown sub-step: {target!r} (expected: all|{'|'.join(STEPS)})")
    fn()
    if target == "core":
        print_url()
    return 0
