"""SSH helpers and per-host fact caches.

Authentication comes from the user's local OpenSSH setup
(~/.ssh/config + ssh-agent); each Host.ssh_host is passed to ssh as-is.

The .cache/node-name/<host> file is shared with lib/remote.sh.
"""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path

from . import die, hecaton_root, log
from .inventory import Host
from .k8s import kubectl

_SSH_OPTS = [
    "-o", "ConnectTimeout=10",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "BatchMode=yes",
]


def ssh_capture(host: Host, remote_cmd: str) -> str:
    """Run remote_cmd on host, return stdout. Dies on non-zero exit."""
    r = subprocess.run(
        ["ssh", *_SSH_OPTS, host.ssh_host, remote_cmd],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        die(f"ssh {host.name}: rc={r.returncode}: {r.stderr.strip()}")
    return r.stdout


def _cache_dir(name: str) -> Path:
    d = hecaton_root() / ".cache" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def node_name(host: Host) -> str:
    """k8s node name = remote `hostname` lowercased. Cached per host."""
    cache = _cache_dir("node-name") / host.name
    if cache.is_file():
        return cache.read_text().strip()
    n = ssh_capture(host, "hostname").strip().lower()
    cache.write_text(n + "\n")
    return n


class Vendor(str, Enum):
    NVIDIA = "nvidia"
    AMD = "amd"
    NONE = "none"


def gpu_vendor(host: Host) -> Vendor:
    """GPU vendor for host, detected via `lspci -nn`. Cached per host.

    PCI vendor IDs (more reliable than name strings):
      1002 = AMD, 10de = NVIDIA.
    """
    cache = _cache_dir("gpu-vendor") / host.name
    if cache.is_file():
        return Vendor(cache.read_text().strip())
    out = ssh_capture(host, "lspci -nn 2>/dev/null")
    if "[1002:" in out:
        vendor = Vendor.AMD
    elif "[10de:" in out:
        vendor = Vendor.NVIDIA
    else:
        vendor = Vendor.NONE
    cache.write_text(vendor.value + "\n")
    return vendor


def warm(hosts: list[Host], *, what: str = "facts") -> None:
    """Parallel-warm node_name + gpu_vendor caches for hosts."""
    if not hosts:
        return
    log(f"probing {len(hosts)} host(s) ({what})")
    with ThreadPoolExecutor(max_workers=min(32, len(hosts))) as pool:
        futs = []
        for h in hosts:
            futs.append(pool.submit(node_name, h))
            futs.append(pool.submit(gpu_vendor, h))
        for f in futs:
            f.result()


def label_nodes_by_vendor(hosts: list[Host]) -> dict[Vendor, list[Host]]:
    """Tag each node with hecaton.io/gpu-vendor=<vendor>, return hosts grouped by vendor."""
    by_vendor: dict[Vendor, list[Host]] = {v: [] for v in Vendor}
    for h in hosts:
        v = gpu_vendor(h)
        n = node_name(h)
        kubectl(
            "label", "node", n, f"hecaton.io/gpu-vendor={v.value}",
            "--overwrite",
        )
        log(f"  {h.name} ({n}) -> hecaton.io/gpu-vendor={v.value}")
        by_vendor[v].append(h)
    return by_vendor
