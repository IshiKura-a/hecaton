"""Thin subprocess wrappers around kubectl and helm.

Zero-cleverness pass-throughs. KUBECONFIG plumbing is the caller's job
(every phase wrapper exports it before invoking).
"""

from __future__ import annotations

import json
import subprocess


def kubectl(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["kubectl", *args],
        input=stdin,
        text=True,
        check=True,
    )


def kubectl_capture(*args: str, stdin: str | None = None) -> str:
    r = subprocess.run(
        ["kubectl", *args],
        input=stdin,
        capture_output=True, text=True, check=True,
    )
    return r.stdout


def kubectl_apply_stdin(manifest: str, *, namespace: str | None = None) -> None:
    extra = ["-n", namespace] if namespace else []
    kubectl("apply", *extra, "-f", "-", stdin=manifest)


def helm(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["helm", *args], check=True)


def helm_repo_present(name: str) -> bool:
    r = subprocess.run(
        ["helm", "repo", "list", "-o", "json"],
        capture_output=True, text=True, check=False,
    )
    # `helm repo list` exits non-zero when there are no repos at all.
    if r.returncode != 0:
        if "no repositories" in r.stderr.lower():
            return False
        raise subprocess.CalledProcessError(
            r.returncode, r.args, output=r.stdout, stderr=r.stderr,
        )
    return any(repo.get("name") == name for repo in json.loads(r.stdout))
