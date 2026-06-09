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
├── scaffolds/    Agent tool sets (R2E-Gym, ...) staged into sandbox pods.
├── scripts/      Laptop preflight + trainer-host setup.
├── examples/     End-to-end demos (`trainer-smoke/run_bare.py` and
│                 `trainer-smoke/run_r2egym.py`).
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

- Broker creates a `Sandbox` CR from a `SandboxTemplate` (one per task type, 1:1 with the docker image), waits for Ready, returns the pod IP and container port to the trainer. Trainers can optionally name a *scaffold* (R2E-Gym, ...) at acquire time; the broker mounts that scaffold's tools into the pod via hostPath (see [Scaffold tools](#scaffold-tools)).
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

Optional: drop one `SandboxTemplate` YAML per task into `config/templates/` (or you can apply them later by hand). Scaffold tool sets live under `scaffolds/<scaffold>/` and are versioned with the repo (see [Scaffold tools](#scaffold-tools))— nothing to fill in unless you're adding or overriding a scaffold.

### 2. Run (laptop)

```bash
bash bootstrap/install.sh
```

That covers everything in order — Tailscale, k3s server, k3s agents, GPU plugins, agent-sandbox controller, templates, subnet router, scaffold tools staging, broker image build + deploy. Every phase is idempotent, so re-running after any fix is safe.

When it's done:

```bash
KUBECONFIG=config/kubeconfig kubectl get nodes -o wide
```

### 3. Connect a trainer

The trainer host sets itself up — the laptop has no access to it. On the trainer:

```bash
git clone https://github.com/IshiKura-a/hecaton.git ~/hecaton
cd ~/hecaton

export TS_AUTHKEY_TRAINER=tskey-auth-...
export HECATON_BROKER_URL=http://<any-fleet-host-tailnet-ip>:30443
export HECATON_TOKEN=...
export HECATON_RUN_ID=my-run-2026-06-08
export HECATON_SDK_PATH=$PWD/envs
bash scripts/setup-trainer.sh
```

The script joins the tailnet, installs `hecaton-envs` editable from `$HECATON_SDK_PATH`, and prints the env block your trainer process needs to `source`. Picking up an SDK change is just `git pull` in `~/hecaton/` — no re-run needed because the install is editable.

The trainer process then `from hecaton_envs import SandboxProvider`, `provider.acquire(template="...")` (optionally `scaffold="r2egym"` to mount a scaffold's tools), `sb.exec("...")` for raw bash or `sb.invoke(action)` when a scaffold is bound, `provider.release(sb)`.

A complete docker-based demo lives in [examples/trainer-smoke](examples/trainer-smoke/). For iterating on hecaton itself (SDK / broker / scaffolds), see [Testing & deploying changes](#testing--deploying-changes) — that flow uses a throwaway dev trainer, not your real one.

## Adding a new sandbox type

A sandbox type = one OCI image that implements the [agent-sandbox HTTP contract](https://github.com/kubernetes-sigs/agent-sandbox), wrapped in a `SandboxTemplate` CR the broker references by name.

1. **Build the image** and push it somewhere fleet nodes can pull from (ghcr.io / your registry / offline import). The image only needs to expose the agent-sandbox `/execute` endpoint; if any scaffold you'll use ships a `requirements.txt`, also ensure `python3 -m pip` works and PyPI is reachable.

2. **Drop a SandboxTemplate yaml** into [config/templates/](config/templates/). Use [config/examples/templates/swe-python.yaml](config/examples/templates/swe-python.yaml) as a starting point — it's a complete reference with image, port, resource requests. The `metadata.name` is what trainers pass to `provider.acquire(template=...)`.

3. **Apply**:

   ```bash
   bash bootstrap/cluster/24-apply-templates.sh   # or `bash bootstrap/install.sh` for full re-bootstrap
   ```

   Idempotent (`kubectl apply`). New templates land immediately; trainers can acquire by name on the next call.

4. **Verify**:

   ```bash
   KUBECONFIG=config/kubeconfig kubectl get sandboxtemplates -n hecaton-sandboxes
   make dev host=<alias>   # smoke (acquire → exec → release)
   ```

Nothing in the broker or trainer SDK knows about specific templates — sandbox types are purely cluster data.

## Scaffold tools

A *scaffold* is a named bundle of agent tools (R2E-Gym's `file_editor`, `execute_bash`, `search`; or any other set of executables) that the trainer wants available inside the sandbox. SandboxTemplates stay scaffold-agnostic — scaffolds are layered in at acquire time so one sandbox image can serve many scaffolds.

### How the pieces fit

- **Source of truth**: `scaffolds/<scaffold>/` in this repo — one directory per scaffold. `scaffolds/r2egym/` ships out of the box; add your own by dropping a new directory in.
- **Staged on every host**: [`bootstrap/cluster/27-stage-agent-tools.sh`](bootstrap/cluster/27-stage-agent-tools.sh) syncs `scaffolds/` to `/opt/hecaton/agent-tools/<scaffold>/` on every fleet host. While staging it also rewrites every `*.py` shebang to `#!/usr/bin/env python3` so tools resolve their interpreter through the sandbox image's PATH rather than a hard-coded path. Files end up mode `0555` (r-x, no write). The phase always re-stages; it refuses to run while any Sandbox CR exists, since replacing files under a live sandbox would silently swap its mounted tools.
- **Mounted at acquire time**: the broker, when `acquire(scaffold="<name>")` is called, appends a hostPath volume to the pod spec and mounts it readOnly at `/opt/agent-tools` in every container. The broker does **not** touch the image's PATH — tools are invoked by absolute path.
- **Python deps installed at acquire time**: if the scaffold dir contains a `requirements.txt`, the SDK runs `pip install -r /opt/agent-tools/requirements.txt` against the pod before returning the handle. Scaffold deps stay with the scaffold; sandbox images don't have to bake them in.
- **Invoked via an adapter**: trainer code calls `sb.invoke(action)`, which dispatches through a `ScaffoldAdapter`. The adapter renders the action to an absolute-path command (e.g. `/opt/agent-tools/file_editor view --path ...`) and parses the response back into a scaffold-native observation. The built-in `R2EGymAdapter` accepts any object with a `to_bashcmd()` method (an r2egym `Action` qualifies; trainers install r2egym themselves). Adding a scaffold = drop tools under `scaffolds/<name>/` + register a `ScaffoldAdapter` under that name (see `hecaton_envs.scaffolds`).

### Sandbox image contract

The sandbox image (whatever the SandboxTemplate references) must provide:

- **`python3` on PATH** — used to run the staged tool scripts and the acquire-time `pip install`.
- **`pip` reachable** as `python3 -m pip`, and network access to PyPI (or a fleet-internal mirror). Skip this only if no scaffold you use ships a `requirements.txt`.

That's the entire contract. Everything else about the image is the task author's call. Nothing scaffold-specific belongs in the image.

### Updating a scaffold's tools

Change the files under `scaffolds/<scaffold>/` (or add a patch under `scaffolds/<scaffold>/_patches/`), then re-run bootstrap:

```bash
bash bootstrap/install.sh
```

Phase 27 will refuse to re-stage while any sandbox is alive (it would silently swap tools mid-rollout, since hostPath is a bind mount). Release everything first — `provider.revoke(...)` on each trainer, or `kubectl delete sandbox -n hecaton-sandboxes --all` for ops — then re-run.

## Testing & deploying changes

Three commands, one per scope:

| Scope | Command | What it does |
| --- | --- | --- |
| First-time install of a fresh fleet | `bash bootstrap/install.sh` | Tailscale → k3s → device plugins → agent-sandbox → templates → subnet router → scaffolds → broker. Idempotent; re-run after any fix. |
| Iterate on your laptop → dev fleet | `make dev [host=<alias>]` | Hash-gated: only rebuilds & redeploys what actually changed. With `host=`, also runs a smoke script on that trainer. |
| Promote a dev change to production | `make release` | Refuses dirty tree, `git push` HEAD on main, waits for `broker-image.yml` CI to publish `ghcr.io/.../hecaton-broker:sha-<sha>`, pins `.env`, redeploys the broker. |

### `make dev` in detail

One command brings the cluster + an optional test trainer up to your current checkout. Each phase compares a content hash against the deployed state and skips itself if nothing changed.

```bash
make dev                       # stage scaffolds + redeploy broker (whatever changed)
make dev host=<ssh-alias>      # also rsync sources + rebuild trainer image
                               # if needed + run run_r2egym.py
make dev host=<alias> smoke=run_<other>.py   # pick a different smoke script
```

| Phase | Triggers when… | Action |
| --- | --- | --- |
| scaffold | `scaffolds/` content hash differs from last stage | wraps `bootstrap/cluster/27-stage-agent-tools.sh` |
| broker | `platform/broker/` content hash ≠ the image tag the cluster is running | build + import + `kubectl set image` to `hecaton-broker:dev-<hash>` |
| trainer-image | trainer Dockerfile or entrypoint hash changed (and `host=` set) | rsync repo to host, `docker build` in `HECATON_SOURCE=mount` mode |
| smoke | `host=` set | `docker run` the trainer image with the repo bind-mounted as the host user; `trainer-entrypoint.sh` `pip install --user`s the mounted SDK so SDK / smoke script changes never need a rebuild |

Surgical targets exist for each phase (`make dev-scaffold`, `make dev-broker`, `make dev-trainer-image host=…`, `make dev-smoke host=…`). Env knobs: `SKIP_BROKER=1`, `FORCE_BROKER=1`, etc.

Provenance: every locally built broker image carries `hecaton.git.sha` / `hecaton.git.dirty` / `hecaton.build.timestamp` labels — `docker inspect` (or `kubectl describe pod`) tells you exactly which commit any running broker came from.

### `make release` in detail

`make release` is `make dev`'s production sibling — instead of building local images, it pushes to GitHub and lets CI build the canonical `ghcr.io` image. Preconditions: clean tree on `main`, `gh` authenticated (covered by `bash scripts/preflight.sh`). The same flow without the wrapper:

```bash
git push origin main                                     # triggers .github/workflows/broker-image.yml
# wait for it...
# edit .env: BROKER_IMAGE=ghcr.io/<owner>/hecaton-broker:sha-<full-sha>
bash bootstrap/cluster/26-install-broker.sh
```

## Conventions

- Numbered scripts run in order within a phase; `bootstrap/install.sh` chains the phases.
- Every script is idempotent.
- Secrets only in `.env` (gitignored). Real `config/hosts.yaml`, `config/tailnet-policy.hujson`, `config/templates/`, `config/kubeconfig`, `config/k3s-node-token` are gitignored; only the `config/examples/` siblings are committed.
- hecaton stores no ssh credentials. Each host's `ssh_host` in `config/hosts.yaml` is passed straight to `ssh`; configure auth in your `~/.ssh/config`.
- Pinned upstream versions (k3s, GPU plugin images, agent-sandbox release) live in `lib/*-version.sh`.
- After cloning, install the pre-commit hook (secret scan + `ruff check` on staged Python):
  ```bash
  bash scripts/install-hooks.sh
  ```
  Ruff config lives at the repo root in `ruff.toml`. The hook uses `ruff` from PATH, falling back to `uvx ruff` — install one of them.
