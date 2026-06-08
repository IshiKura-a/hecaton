# hecaton

> *Hecatoncheires (Ἑκατόγχειρες) — the hundred-handed giants of Greek myth, who hurled a hundred boulders at the Titans in a single throw.*

Deployment + glue for running mountains of [agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox) docker containers across a fleet of GPU hosts, fronted by a trainer-facing broker so RL training jobs can acquire sandboxes by name.

## What's in here

```
hecaton/
├── bootstrap/    Bare metal → ready cluster + broker (one-key install).
│   ├── install.sh
│   ├── network/  Tailscale on every host.
│   └── cluster/  k3s server/agents, device plugins, agent-sandbox,
│                 templates, subnet router, broker.
├── platform/
│   ├── broker/   FastAPI service trainers talk to (build + Deployment).
│   └── network/  In-cluster Tailscale subnet router manifest.
├── envs/         Python trainer SDK (`hecaton-envs`) + container entrypoint.
├── scripts/      Laptop preflight + trainer-host setup.
├── examples/     End-to-end demo (`trainer-smoke`: Dockerfile + run.py).
├── ops/          Day-2 (remove-host etc.).
├── lib/          Shared shell helpers + pinned versions of upstream pieces.
└── config/       Real config (gitignored) + examples/ templates (committed).
```

## Architecture

```
trainer container ──tailnet (tag:trainer)──► broker ──k8s API──► agent-sandbox
                                                                  controller
                                                                      │
                                            ┌─────────────────────────┘
                                            ▼
                                    sandbox pod (cluster pod CIDR)
                                            ▲
trainer container ──tailnet subnet route───┘
                    (10.42.0.0/16, advertised by in-cluster subnet router)
```

- Broker creates a `Sandbox` CR from a `SandboxTemplate` (one per task type, 1:1 with the docker image), waits for Ready, returns the pod IP and container port to the trainer.
- Trainer dials the pod IP directly through the tailnet subnet route. No data-plane proxy.
- Tailnet ACL gates network access: `tag:trainer` may only reach `tag:fleet-broker:443` and `10.42.0.0/16:*`.

## Quick start

Five things you fill in once, two commands you run.

### 1. Fill in (one-time, on the laptop)

```bash
cp .env.example .env                                  # 4 tokens (3 Tailscale auth keys + 1 broker bearer)
cp config/examples/hosts.yaml config/hosts.yaml       # list every fleet host (with role: server | agent)
cp config/examples/tailnet-policy.hujson \
   config/tailnet-policy.hujson                       # add your Tailscale login to group:fleet-ops
```

Paste `config/tailnet-policy.hujson` into <https://login.tailscale.com/admin/acls/file> and Save.

For each fleet host: a matching `Host <ssh_host>` block in your `~/.ssh/config` (HostName + User + IdentityFile). hecaton does not store ssh credentials.

Optional: drop one `SandboxTemplate` YAML per task into `config/templates/` (or you can apply them later by hand).

### 2. Run (laptop)

```bash
bash bootstrap/install.sh
```

That covers everything in order — Tailscale, k3s server, k3s agents, GPU plugins, agent-sandbox controller, templates, subnet router, broker image build + deploy. Every phase is idempotent, so re-running after any fix is safe.

When it's done:

```bash
KUBECONFIG=config/kubeconfig kubectl get nodes -o wide
```

### 3. Run (on each trainer host)

```bash
# scp scripts/setup-trainer.sh + envs/ to the host, then:
export TS_AUTHKEY_TRAINER=tskey-auth-...
export HECATON_BROKER_URL=http://<any-fleet-host-tailnet-ip>:30443
export HECATON_TOKEN=...
export HECATON_RUN_ID=my-run-2026-06-08
export HECATON_SDK_PATH=/path/to/envs
bash setup-trainer.sh
```

The trainer process then `from hecaton_envs import SandboxProvider`, `provider.acquire(template="...")`, `sb.exec("...")`, `provider.release(sb)`.

A complete docker-based demo lives in [examples/trainer-smoke](examples/trainer-smoke/).

## Conventions

- Numbered scripts run in order within a phase; `bootstrap/install.sh` chains the phases.
- Every script is idempotent.
- Secrets only in `.env` (gitignored). Real `config/hosts.yaml`, `config/tailnet-policy.hujson`, `config/templates/`, `config/kubeconfig`, `config/k3s-node-token` are gitignored; only the `config/examples/` siblings are committed.
- hecaton stores no ssh credentials. Each host's `ssh_host` in `config/hosts.yaml` is passed straight to `ssh`; configure auth in your `~/.ssh/config`.
- Pinned upstream versions (k3s, GPU plugin images, agent-sandbox release) live in `lib/*-version.sh`.
- After cloning, install the pre-commit hook that scans for accidentally-committed secrets:
  ```bash
  bash scripts/install-hooks.sh
  ```
