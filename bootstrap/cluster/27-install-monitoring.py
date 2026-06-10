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

import sys
from pathlib import Path

# bootstrap/lib lives next to bootstrap/cluster.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from lib.k8s import (  # noqa: E402
    helm,
    helm_repo_present,
    kubectl_apply_stdin,
    kubectl_capture,
)

from lib import die, hecaton_root, inventory, log, remote, versions  # noqa: E402  # noqa: E402

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
    """Hot-load dashboards via grafana sidecar (watches ConfigMaps labelled grafana_dashboard=1)."""
    d = hecaton_root() / "platform" / "monitoring" / "dashboards"
    jsons = sorted(d.glob("*.json")) if d.is_dir() else []
    if not jsons:
        log(f"  no dashboard json found in {d}, skipping")
        return
    log("applying dashboards ConfigMap")
    # Build the ConfigMap with kubectl --dry-run, then add the label,
    # then apply. Same pipeline as the bash version, just via stdin.
    cm = kubectl_capture(
        "create", "configmap", "hecaton-dashboards",
        "-n", NS,
        *[f"--from-file={j}" for j in jsons],
        "--dry-run=client", "-o", "yaml",
    )
    labelled = kubectl_capture(
        "label", "--local", "-f", "-",
        "grafana_dashboard=1",
        "--dry-run=client", "-o", "yaml",
        stdin=cm,
    )
    kubectl_apply_stdin(labelled)


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


def amd_smi_manifest(image_tag: str) -> str:    return f"""\
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
    grafana_url = f"http://{server_ip}:30080" if server_ip else "http://<server-tailnet-ip>:30080"
    log("")
    log("monitoring resources applied.")
    log(f"  grafana:   {grafana_url}  (admin / admin)")
    log("  dashboards (under 'hecaton' tag):")
    log("    Hecaton — Capacity            (GPU headroom, fleet density)")
    log("    Hecaton — Acquire Health      (latency, failure reasons)")
    log("    Hecaton — Sandbox Lifecycle   (pod phases, restarts, controller)")
    log("    Hecaton — Nodes               (CPU, mem, disk, net, GPU)")


# --- main ------------------------------------------------------------------


_STEPS = {
    "core": step_core,
    "dashboards": step_dashboards,
    "exporters": step_exporters,
}


def main(argv: list[str]) -> int:
    target = argv[1] if len(argv) > 1 else "all"

    if target == "all":
        step_core()
        step_dashboards()
        step_exporters()
        print_url()
        return 0

    fn = _STEPS.get(target)
    if fn is None:
        die(f"unknown sub-step: {target!r} (expected: all|{'|'.join(_STEPS)})")
    fn()
    if target == "core":
        print_url()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
