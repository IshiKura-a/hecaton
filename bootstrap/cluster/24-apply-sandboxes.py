"""Render sandbox declarations under config/sandboxes/ into k8s SandboxTemplate
CRs and reconcile them with the cluster.

Two file kinds are recognized, dispatched on the top-level `kind` field:

- `kind: Sandbox` — one fully-authored sandbox. The yaml directly describes
  one SandboxTemplate.
- `kind: SandboxSource` — pull a list of images from an external dataset
  and render one SandboxTemplate per row, merging source-level defaults
  under per-row overrides. Backends today: `huggingface`, `local`.

Apply semantics: every rendered template carries the label
`hecaton.io/managed-by=hecaton`. After applying the new set, any existing
SandboxTemplate carrying that label whose name is no longer in the new
set is deleted. Removing an entry from the dataset / yaml therefore
propagates to the cluster on the next run. The broker likewise refuses
to acquire templates without this label, so cluster state stays
aligned with git.

Run via bootstrap/cluster/24-apply-sandboxes.sh (which sets up the venv
with huggingface_hub + pyyaml and invokes us with the sandboxes dir).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

NAMESPACE = "hecaton-sandboxes"
API_VERSION = "extensions.agents.x-k8s.io/v1alpha1"
MANAGED_LABEL = "hecaton.io/managed-by"
MANAGED_VALUE = "hecaton"
SOURCE_LABEL = "hecaton.io/source"  # filename the template came from

# k8s name: lowercase RFC1123 with dots/dashes, ≤253 chars. We render
# this from image references so we sanitize aggressively.
_NAME_RE = re.compile(r"[^a-z0-9.-]+")


# ---------------------------------------------------------------------------


@dataclass
class RenderedTemplate:
    name: str
    source_file: str
    body: dict[str, Any]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: 24-apply-sandboxes.py <config/sandboxes-dir>", file=sys.stderr)
        return 2

    src_dir = Path(argv[1])
    if not src_dir.is_dir():
        print(f"not a directory: {src_dir}", file=sys.stderr)
        return 2

    files = sorted(p for p in src_dir.glob("*.yaml") if p.is_file())
    if not files:
        print(f"no yaml files in {src_dir}", file=sys.stderr)
        return 0

    rendered: list[RenderedTemplate] = []
    for f in files:
        rendered.extend(_render_file(f))

    _check_unique_names(rendered)
    _kubectl_apply(rendered)
    _gc_stale({r.name for r in rendered})

    print(f"applied {len(rendered)} SandboxTemplate(s) from {len(files)} file(s)")
    return 0


# ---- file dispatch -------------------------------------------------------


def _render_file(path: Path) -> list[RenderedTemplate]:
    with path.open() as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict):
        raise SystemExit(f"{path}: top-level must be a mapping")
    kind = doc.get("kind")
    if kind == "Sandbox":
        return [_render_one(path, doc)]
    if kind == "SandboxSource":
        return _render_source(path, doc)
    raise SystemExit(f"{path}: unsupported kind {kind!r} (want Sandbox or SandboxSource)")


def _render_one(path: Path, doc: dict[str, Any]) -> RenderedTemplate:
    """Render a hand-authored single-sandbox yaml."""
    name = doc.get("name")
    image = doc.get("image")
    if not name or not image:
        raise SystemExit(f"{path}: Sandbox needs name and image")
    return _build(
        source_file=path.name,
        name=name,
        image=image,
        port=int(doc.get("port", 8888)),
        gpu=int(doc.get("gpu", 0)),
        gpu_vendor=str(doc.get("gpu_vendor", "nvidia")),
        cpu=str(doc.get("cpu", "1")),
        memory=str(doc.get("memory") or doc.get("resources", {}).get("memory", "2Gi")),
        env=dict(doc.get("env") or {}),
    )


def _render_source(path: Path, doc: dict[str, Any]) -> list[RenderedTemplate]:
    """Pull a remote dataset and render one SandboxTemplate per row."""
    source = doc.get("source") or {}
    src_type = source.get("type")
    if src_type == "huggingface":
        local_path = _fetch_huggingface(source, str(path))
    elif src_type == "local":
        local_path = _fetch_local(source, str(path), path)
    else:
        raise SystemExit(f"{path}: unknown source.type {src_type!r}")
    rows = _parse_rows(local_path, str(path))

    defaults_port = int(doc.get("port", 8888))
    defaults_gpu = int(doc.get("gpu", 0))
    defaults_vendor = str(doc.get("gpu_vendor", "nvidia"))
    defaults_cpu = str(doc.get("cpu", "1"))
    defaults_memory = str(doc.get("memory", "2Gi"))
    defaults_env = dict(doc.get("env") or {})

    out: list[RenderedTemplate] = []
    for row in rows:
        image = row.get("image")
        if not image:
            raise SystemExit(f"{path}: row missing image: {row!r}")
        name = row.get("name") or _derive_name(image)
        env = {**defaults_env, **(row.get("env") or {})}
        out.append(
            _build(
                source_file=path.name,
                name=name,
                image=image,
                port=int(row.get("port", defaults_port)),
                gpu=int(row.get("gpu", defaults_gpu)),
                gpu_vendor=str(row.get("gpu_vendor", defaults_vendor)),
                cpu=str(row.get("cpu", defaults_cpu)),
                memory=str(row.get("memory", defaults_memory)),
                env=env,
            )
        )
    return out


# ---- backends ------------------------------------------------------------


def _fetch_huggingface(source: dict[str, Any], context: str) -> str:
    repo = source.get("repo")
    file = source.get("file")
    revision = source.get("revision", "main")
    if not repo or not file:
        raise SystemExit(f"{context}: huggingface source needs repo and file")

    # Imported here so the script is importable without huggingface_hub
    # installed (helpful for unit tests of the rendering logic).
    from huggingface_hub import hf_hub_download

    return hf_hub_download(
        repo_id=repo,
        filename=file,
        revision=revision,
        repo_type="dataset",
        token=os.environ.get("HF_TOKEN"),  # optional, for private datasets
    )


def _fetch_local(source: dict[str, Any], context: str, source_yaml: Path) -> str:
    """Read rows from a path on the laptop's filesystem.

    Used for air-gapped environments and for testing the rendering
    pipeline without needing a HuggingFace dataset. `path` is resolved
    relative to the source yaml's parent directory unless absolute.
    """
    raw = source.get("path")
    if not raw:
        raise SystemExit(f"{context}: local source needs path")
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = source_yaml.parent / p
    if not p.is_file():
        raise SystemExit(f"{context}: local source path not found: {p}")
    return str(p)


def _parse_rows(local_path: str, context: str) -> list[dict[str, Any]]:
    if local_path.endswith(".jsonl"):
        with open(local_path) as fh:
            return [json.loads(line) for line in fh if line.strip()]
    if local_path.endswith(".parquet"):
        # pyarrow is a hefty dep; load lazily so jsonl users don't pay.
        import pyarrow.parquet as pq

        return pq.read_table(local_path).to_pylist()
    raise SystemExit(f"{context}: unsupported file extension for {local_path!r}")


# ---- rendering -----------------------------------------------------------


def _build(
    *,
    source_file: str,
    name: str,
    image: str,
    port: int,
    gpu: int,
    gpu_vendor: str,
    cpu: str,
    memory: str,
    env: dict[str, str],
) -> RenderedTemplate:
    container: dict[str, Any] = {
        "name": "sandbox",
        "image": image,
        "ports": [{"containerPort": port}],
        "resources": {"requests": {"cpu": cpu, "memory": memory}},
    }
    if gpu > 0:
        if gpu_vendor not in ("nvidia", "amd"):
            raise SystemExit(
                f"{source_file}: gpu_vendor must be 'nvidia' or 'amd', got {gpu_vendor!r}"
            )
        key = f"{gpu_vendor}.com/gpu"
        container["resources"]["requests"][key] = str(gpu)
        container["resources"].setdefault("limits", {})[key] = str(gpu)
    if env:
        container["env"] = [{"name": k, "value": str(v)} for k, v in sorted(env.items())]

    body = {
        "apiVersion": API_VERSION,
        "kind": "SandboxTemplate",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "labels": {
                MANAGED_LABEL: MANAGED_VALUE,
                SOURCE_LABEL: _label_safe(source_file),
            },
        },
        "spec": {"podTemplate": {"spec": {"containers": [container]}}},
    }
    return RenderedTemplate(name=name, source_file=source_file, body=body)


def _derive_name(image: str) -> str:
    """ghcr.io/foo/swe-task-0001:v1 → swe-task-0001-v1.

    Strip registry/org, keep last path segment plus tag (or digest prefix
    if pinned by digest). Sanitize to RFC1123 lowercase.
    """
    ref = image
    digest_suffix = ""
    if "@sha256:" in ref:
        head, _, digest = ref.partition("@sha256:")
        digest_suffix = "-" + digest[:8]
        ref = head
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        repo, tag = last.split(":", 1)
        candidate = f"{repo}-{tag}{digest_suffix}"
    else:
        candidate = f"{last}{digest_suffix}"
    name = _NAME_RE.sub("-", candidate.lower()).strip("-.")
    if not name:
        raise SystemExit(f"could not derive a valid name from image {image!r}")
    return name[:253]


def _label_safe(s: str) -> str:
    # Label values: ≤63 chars, [a-z0-9._-]. Filename is a sensible breadcrumb.
    cleaned = _NAME_RE.sub("-", s.lower()).strip("-.")
    return cleaned[:63] or "unnamed"


def _check_unique_names(rendered: list[RenderedTemplate]) -> None:
    seen: dict[str, str] = {}
    for r in rendered:
        if r.name in seen:
            raise SystemExit(
                f"duplicate SandboxTemplate name {r.name!r} "
                f"(from {seen[r.name]} and {r.source_file})"
            )
        seen[r.name] = r.source_file


# ---- kubectl -------------------------------------------------------------


def _kubectl_apply(rendered: list[RenderedTemplate]) -> None:
    if not rendered:
        return
    # Ensure namespace exists; harmless if it already does.
    subprocess.run(
        ["kubectl", "create", "namespace", NAMESPACE],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # `kubectl apply -f -` with a multi-doc stream. Use a temp file for
    # readable error messages when apply fails on a specific doc.
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tmp:
        for r in rendered:
            tmp.write("---\n")
            yaml.safe_dump(r.body, tmp, sort_keys=False)
        tmp_path = tmp.name
    try:
        subprocess.run(["kubectl", "apply", "-n", NAMESPACE, "-f", tmp_path], check=True)
    finally:
        os.unlink(tmp_path)


def _gc_stale(current: set[str]) -> None:
    """Delete any SandboxTemplate this script previously created but is no
    longer in the desired set."""
    out = subprocess.run(
        [
            "kubectl", "get", "sandboxtemplates",
            "-n", NAMESPACE,
            "-l", f"{MANAGED_LABEL}={MANAGED_VALUE}",
            "-o", "jsonpath={.items[*].metadata.name}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    existing = set(filter(None, out.stdout.split()))
    stale = sorted(existing - current)
    if not stale:
        return
    print(f"deleting {len(stale)} stale SandboxTemplate(s): {' '.join(stale)}")
    subprocess.run(
        ["kubectl", "delete", "sandboxtemplates", "-n", NAMESPACE, *stale],
        check=True,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
