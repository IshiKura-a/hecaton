"""Apply per-host GPU device-plugin DaemonSets, honoring `gpu_count` caps.

For every fleet host: detect GPU vendor, label the k3s node, and apply
a DaemonSet pinned to that single node via kubernetes.io/hostname. The
per-host scoping lets us set HIP_VISIBLE_DEVICES / NVIDIA_VISIBLE_DEVICES
to the first `gpu_count` indices so kubelet only ever sees that many
devices on that node.

Idempotent: re-running converges; rollout-status waits for each DS.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from lib.k8s import kubectl, kubectl_apply_stdin, kubectl_capture  # noqa: E402

from lib import die, inventory, log, remote, versions  # noqa: E402  # noqa: E402

NS = "kube-system"


def device_list(cap: int) -> str:
    """count=4 -> '0,1,2,3'. Used as VISIBLE_DEVICES value."""
    return ",".join(str(i) for i in range(cap))


def visible_env_block(env_name: str, cap: int | None) -> str:
    val = device_list(cap) if cap else "all"
    return f"""
        env:
        - name: {env_name}
          value: "{val}\""""


def amd_daemonset(host: inventory.Host, node: str, image: str) -> str:
    visible = visible_env_block("HIP_VISIBLE_DEVICES", host.gpu_count)
    return f"""\
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: amdgpu-device-plugin-{host.name}
  labels:
    app.kubernetes.io/name: amdgpu-device-plugin
    app.kubernetes.io/managed-by: hecaton
    hecaton.io/host: {host.name}
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: amdgpu-device-plugin
      hecaton.io/host: {host.name}
  updateStrategy: {{ type: RollingUpdate }}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: amdgpu-device-plugin
        hecaton.io/host: {host.name}
    spec:
      priorityClassName: system-node-critical
      nodeSelector:
        kubernetes.io/hostname: {node}
      tolerations: [{{ operator: Exists }}]
      containers:
      - name: amdgpu-device-plugin
        image: {image}
        imagePullPolicy: IfNotPresent
        securityContext: {{ privileged: true }}{visible}
        volumeMounts:
        - {{ name: device-plugin, mountPath: /var/lib/kubelet/device-plugins }}
        - {{ name: sys,           mountPath: /sys }}
        - {{ name: dev-kfd,       mountPath: /dev/kfd }}
        - {{ name: dev-dri,       mountPath: /dev/dri }}
      volumes:
      - {{ name: device-plugin, hostPath: {{ path: /var/lib/kubelet/device-plugins }} }}
      - {{ name: sys,           hostPath: {{ path: /sys }} }}
      - {{ name: dev-kfd,       hostPath: {{ path: /dev/kfd }} }}
      - {{ name: dev-dri,       hostPath: {{ path: /dev/dri }} }}
"""


def nvidia_daemonset(host: inventory.Host, node: str, image: str) -> str:
    visible = visible_env_block("NVIDIA_VISIBLE_DEVICES", host.gpu_count)
    return f"""\
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: nvidia-device-plugin-{host.name}
  labels:
    app.kubernetes.io/name: nvidia-device-plugin
    app.kubernetes.io/managed-by: hecaton
    hecaton.io/host: {host.name}
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: nvidia-device-plugin
      hecaton.io/host: {host.name}
  updateStrategy: {{ type: RollingUpdate }}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: nvidia-device-plugin
        hecaton.io/host: {host.name}
    spec:
      priorityClassName: system-node-critical
      runtimeClassName: nvidia
      nodeSelector:
        kubernetes.io/hostname: {node}
      tolerations: [{{ operator: Exists }}]
      containers:
      - name: nvidia-device-plugin
        image: {image}
        imagePullPolicy: IfNotPresent
        securityContext:
          allowPrivilegeEscalation: false
          capabilities: {{ drop: ["ALL"] }}{visible}
        volumeMounts:
        - {{ name: device-plugin, mountPath: /var/lib/kubelet/device-plugins }}
      volumes:
      - {{ name: device-plugin, hostPath: {{ path: /var/lib/kubelet/device-plugins }} }}
"""


def apply_and_wait(manifest: str, ds_name: str) -> None:
    kubectl_apply_stdin(manifest, namespace=NS)
    kubectl(
        "-n", NS, "rollout", "status",
        f"ds/{ds_name}", "--timeout=180s",
    )


_RESOURCE_FOR_VENDOR = {
    remote.Vendor.AMD: "amd.com/gpu",
    remote.Vendor.NVIDIA: "nvidia.com/gpu",
}


def report_capacity(host: inventory.Host, node: str, vendor: remote.Vendor) -> None:
    res = _RESOURCE_FOR_VENDOR.get(vendor)
    if not res:
        log(f"  {node:<22} (none)")
        return
    # kubectl jsonpath needs the dot escaped: {.status.capacity.amd\.com/gpu}
    escaped = res.replace(".", r"\.")
    out = kubectl_capture(
        "get", "node", node,
        "-o", f"jsonpath={{.status.capacity.{escaped}}}",
    ).strip()
    log(f"  {node:<22} {res} = {out or '0'}")


def wait_for_capacity(host: inventory.Host, node: str, vendor: remote.Vendor) -> None:
    """Wait for kubelet to publish device-plugin capacity after DS rollout."""
    res = _RESOURCE_FOR_VENDOR[vendor]
    escaped = res.replace(".", r"\.")
    expected = host.gpu_count if host.gpu_count is not None else 1
    for _ in range(60):
        out = kubectl_capture(
            "get", "node", node,
            "-o", f"jsonpath={{.status.capacity.{escaped}}}",
        ).strip()
        try:
            actual = int(out or "0")
        except ValueError:
            actual = 0
        if actual >= expected:
            return
        time.sleep(2)
    die(f"{node}: timed out waiting for {res} capacity >= {expected}")


def main() -> int:
    v = versions.load("gpu-version.sh")
    amd_image = v.get("AMD_DEVICE_PLUGIN_IMAGE") or die("AMD_DEVICE_PLUGIN_IMAGE not set")
    nvidia_image = v.get("NVIDIA_DEVICE_PLUGIN_IMAGE") or die("NVIDIA_DEVICE_PLUGIN_IMAGE not set")

    hosts = inventory.load_hosts()
    remote.warm(hosts, what="gpu vendor + k8s node name")

    log("labeling nodes and applying per-host device-plugin DaemonSets")
    by_vendor = remote.label_nodes_by_vendor(hosts)

    for h in by_vendor[remote.Vendor.AMD]:
        node = remote.node_name(h)
        log(f"==> {h.name} ({node}): vendor=amd cap={h.gpu_count or '<all>'}")
        apply_and_wait(amd_daemonset(h, node, amd_image), f"amdgpu-device-plugin-{h.name}")
        wait_for_capacity(h, node, remote.Vendor.AMD)

    for h in by_vendor[remote.Vendor.NVIDIA]:
        node = remote.node_name(h)
        log(f"==> {h.name} ({node}): vendor=nvidia cap={h.gpu_count or '<all>'}")
        apply_and_wait(nvidia_daemonset(h, node, nvidia_image), f"nvidia-device-plugin-{h.name}")
        wait_for_capacity(h, node, remote.Vendor.NVIDIA)

    log("")
    log("advertised GPU capacity:")
    for h in hosts:
        report_capacity(h, remote.node_name(h), remote.gpu_vendor(h))
    return 0


if __name__ == "__main__":
    sys.exit(main())
