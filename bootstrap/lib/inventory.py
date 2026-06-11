"""Inventory loader. Reads config/hosts.yaml into typed Host objects.

Matches the schema parsed by lib/inventory.sh, but goes through
PyYAML so the .yaml can use any valid YAML — no awk-friendly layout
restrictions.
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

from . import die, hecaton_root


@dataclass(frozen=True)
class Host:
    name: str
    ssh_host: str
    role: str | None = None
    gpu_count: int | None = None
    disk_root: str = "/"


def load_hosts() -> list[Host]:
    f = hecaton_root() / "config" / "hosts.yaml"
    if not f.is_file():
        die(f"missing {f} (copy config/examples/hosts.yaml)")

    with f.open() as fh:
        data = yaml.safe_load(fh) or {}

    raw_hosts = data.get("hosts") or []
    if not isinstance(raw_hosts, list):
        die(f"{f}: 'hosts' must be a list")

    out: list[Host] = []
    for i, item in enumerate(raw_hosts):
        if not isinstance(item, dict):
            die(f"{f}: hosts[{i}] is not a mapping")
        name = item.get("name")
        ssh_host = item.get("ssh_host")
        if not name or not ssh_host:
            die(f"{f}: hosts[{i}] missing required name/ssh_host")
        disk_root = item.get("disk_root") or "/"
        if not isinstance(disk_root, str):
            die(f"{f}: hosts[{i}].disk_root must be a string")
        if not disk_root.startswith("/"):
            die(f"{f}: hosts[{i}].disk_root must be an absolute path")
        if any(ch.isspace() for ch in disk_root):
            die(f"{f}: hosts[{i}].disk_root must not contain whitespace")
        if disk_root != "/":
            disk_root = disk_root.rstrip("/")
        out.append(
            Host(
                name=name,
                ssh_host=ssh_host,
                role=item.get("role"),
                gpu_count=item.get("gpu_count"),
                disk_root=disk_root,
            )
        )
    return out
