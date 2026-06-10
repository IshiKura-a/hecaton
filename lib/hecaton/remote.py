"""SSH helpers and per-host fact caches.

Mirrors lib/remote.sh + lib/gpu-detect.sh. Cache directories under
.cache/ are byte-for-byte compatible with the bash side: a fact warmed
by either implementation is visible to the other.

Authentication and connection details come from the user's local
OpenSSH setup (~/.ssh/config + ssh-agent); each Host.ssh_host is passed
to ssh as-is.
"""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path

from . import die, hecaton_root, log
from .inventory import Host

_SSH_OPTS = [
    "-o", "ConnectTimeout=10",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "BatchMode=yes",
]


def ssh_capture(host: Host, remote_cmd: str) -> str:
    """Run remote_cmd on host, return stdout (text). Dies on non-zero exit."""
    r = subprocess.run(
        ["ssh", *_SSH_OPTS, host.ssh_host, remote_cmd],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        die(f"ssh {host.name}: rc={r.returncode}: {r.stderr.strip()}")
    return r.stdout


# --- cached facts ----------------------------------------------------------


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


# PCI vendor IDs (more reliable than name strings).
#   1002 = AMD, 10de = NVIDIA
_VENDOR_TAGS = {
    "[1002:": Vendor.AMD,
    "[10de:": Vendor.NVIDIA,
}


def gpu_vendor(host: Host) -> Vendor:
    """GPU vendor for host, detected via `lspci -nn`. Cached per host."""
    cache = _cache_dir("gpu-vendor") / host.name
    if cache.is_file():
        return Vendor(cache.read_text().strip())
    out = ssh_capture(host, "lspci -nn 2>/dev/null")
    vendor = Vendor.NONE
    for tag, v in _VENDOR_TAGS.items():
        if tag in out:
            vendor = v
            break
    cache.write_text(vendor.value + "\n")
    return vendor


# --- parallel warm ---------------------------------------------------------


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
        # Surface any per-host failure instead of swallowing it.
        for f in futs:
            f.result()
