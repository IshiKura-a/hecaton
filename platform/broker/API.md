# Broker HTTP API

Single control-plane entry point trainers talk to. The data plane
(`execute` / `upload` / `download` / `list` / `exists` on each sandbox
pod) is **not** proxied through this service — the SDK reaches sandbox
pods directly through a Tailscale subnet route over the cluster pod
CIDR.

## Auth

Every request: `Authorization: Bearer <token>`. The token is shared
fleet-wide; Tailscale ACL gates network reachability so only
`tag:trainer` devices reach this port.

## Templates

Trainers reference a `SandboxTemplate` by name. The broker treats the
template's `spec.podTemplate` as the source of truth for image,
resources, mounts, etc. The broker only:

* adds labels (`hecaton.io/owner`, `hecaton.io/run-id`,
  `hecaton.io/template`)
* generates the Sandbox name (`sb-<random>`)

Anything else about the pod is whatever the SandboxTemplate says.

## Endpoints

### `POST /sandboxes`

```json
{ "run_id": "run-2026-06-08", "template": "swe-django-restapi" }
```

→ `201`:
```json
{
  "id":       "sb-7a3f9c",
  "run_id":   "run-2026-06-08",
  "template": "swe-django-restapi",
  "host":     "10.42.1.18",
  "port":     8888
}
```

`host` is the pod IP; `port` is the first containerPort declared in the
template. The call blocks until the Sandbox CR is `Ready`.

### `DELETE /sandboxes/{id}`

`204`.

### `POST /revoke`

```json
{ "run_id": "run-2026-06-08" }
```

→ `200`:
```json
{ "released": 7 }
```

## Idle reaping

The broker tracks `last_active` per sandbox; any `execute` / `upload` /
`download` request against the pod resets it. After **2 hours of
inactivity** the broker tears the sandbox down.

## Non-goals

* Does not proxy data-plane traffic.
* Does not store trajectories or scores.
* Does not validate template contents beyond "has a podTemplate with
  containers and a container port".
