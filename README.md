# k8s-mcp

Read-only Kubernetes MCP server. Each deployment runs as one ServiceAccount, whose
RBAC is the access boundary. Environment flags choose which tools are exposed, and
a namespace allowlist that mirrors the RBAC limits what they return. Served over
SSE on `:3000`.

Safety model: the server issues only read verbs (`get`/`list`) - it never
creates, patches, or deletes. `Secret` (and `ServiceAccount`) are refused
unconditionally; those kinds are not in the resource map, so no tool can read one
regardless of what the SA's RBAC allows. Pod logs are gated behind `EXPOSE_LOGS`
(off by default) in case log contents carry sensitive data.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `K8S_ALLOWED_NAMESPACES` | - | Comma-separated allowlist. Argument-bearing tools reject an off-list `namespace`; `k8s_namespaces` returns exactly this set. Unset -> the SA's RBAC is the only boundary. |
| `EXPOSE_LOGS` | `false` | Register `k8s_logs` (pod log reads). |
| `K8S_GET_MAX_BYTES` | `40000` | Cap on a single `k8s_get` response before it is truncated. |
| `EXPOSE_CONFIGMAPS` | `false` | Make ConfigMaps readable. Off by default: they routinely hold values that belong in a Secret. |
| `K8S_LOG_MAX_LINES` | `500` | Cap on `k8s_logs` tail. |
| `K8S_LOG_MAX_BYTES` | `100000` | Byte cap on returned log text (keeps the newest). |
| `K8S_LIST_MAX_ITEMS` | `200` | Row cap per list/events result. |
| `MCP_TRANSPORT` | `stdio` | `sse` for in-cluster HTTP/SSE. |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `3000` | SSE bind. |

Truthy: `1`, `true`, `yes`, `on` (case-insensitive). In-cluster auth uses the pod's
mounted ServiceAccount token automatically; local dev falls back to `~/.kube/config`.

## Tools

Always registered:
- **`k8s_namespaces()`** - the namespaces this instance is scoped to.
- **`k8s_list(kind, namespace, label_selector?, node?)`** - list resources; omit
  `namespace` or pass `"*"` for every namespace the instance can reach. `node`
  narrows a pod list to one node (a `spec.nodeName` field selector) and is refused
  for kinds that are not scheduled onto a node. For pods, shows phase / ready /
  restarts / the container waiting-or-terminated `reason` (ImagePullBackOff,
  CrashLoopBackOff, ...) and the node the pod is scheduled on.
- **`k8s_get(kind, name, namespace, path?)`** - one object as YAML (managedFields
  stripped). `path` selects a subtree - `status`, `spec.containers`,
  `spec.containers.0.image`; a path that does not exist answers with the keys that
  do. Output over `K8S_GET_MAX_BYTES` (40000) is truncated with a pointer back to
  `path`.
- **`k8s_describe(kind, name, namespace)`** - the troubleshooting view: for a pod,
  its node, each container's image, ready state, restart count, current state and
  what killed the previous one (exit code and reason), plus conditions, resources
  and the recent events about it.
- **`k8s_events(namespace, name?)`** - recent events, newest last; `name` filters to
  one object.
- **`k8s_pod_usage(namespace?, name?)`** - CPU and memory in use per pod against the
  limits that pod set, plus its restart count, worst-by-memory-fraction first.
  Namespaced, so it goes through the namespace allowlist.
- **`k8s_node_usage(node?)`** - CPU and memory in use per node against allocatable,
  with its Ready condition and any memory/disk/PID pressure. Reads
  `metrics.k8s.io`. Cluster-scoped, so it is refused on a namespace-scoped tier.

Readable only when `EXPOSE_CONFIGMAPS` is set: ConfigMaps. `Secret` stays refused
regardless.

Registered only when `EXPOSE_LOGS` is set:
- **`k8s_logs(name, namespace, container?, tail_lines?, previous?)`** - pod logs,
  capped.

`namespace` accepts a pattern in `k8s_list`, `k8s_get` and `k8s_describe`: `*` (or
empty) is every namespace the instance can reach, and a glob like `team*` is those
matching it. On a scoped tier the pattern filters the allowlist, so it can only
narrow what was already permitted. A name that exists in more than one matching
namespace is reported with the candidates rather than resolved to whichever came
first.

Supported kinds (the readable-kind allowlist; `Secret` deliberately excluded): pods,
deployments, replicasets, statefulsets, daemonsets, jobs, cronjobs, services,
endpoints, ingresses, persistentvolumeclaims, events, nodes, namespaces, nodemetrics,
podmetrics, and configmaps when `EXPOSE_CONFIGMAPS` is set.
Cluster-scoped kinds (nodes, namespaces, nodemetrics) only resolve if the SA is granted
them via a ClusterRoleBinding. `nodemetrics` additionally needs metrics-server
installed and read access to `metrics.k8s.io/nodes`; without either, `k8s_node_usage`
reports which is missing rather than failing obscurely.

## Credentials

Three ways to authenticate, tried in this order.

**In-cluster.** Uses the pod's projected ServiceAccount token, which the kubelet
rotates. Used whenever a projected token is present; needs no configuration.

**An auth key.** For running outside the cluster. The auth key can only mint
short-lived tokens for one read-only ServiceAccount; it can read nothing itself.
The server exchanges it for a session token at startup and renews the session at
half its lifetime.

Set up the cluster side as a ServiceAccount whose Role grants `create` on
`serviceaccounts/token` with `resourceNames` pinned to the reader account, and an
issued token Secret for it. Revoking is deleting that Secret; allow about ten
seconds for the API server's authentication cache to catch up.

| var | meaning |
| --- | --- |
| `K8S_AUTHKEY_FILE` | path to the mounted auth key (default `/run/secrets/k8s-authkey`; falls back to `K8S_AUTHKEY`) |
| `K8S_API_SERVER` | API server URL - required in this mode, since there is no in-cluster environment to discover it from |
| `K8S_CA_FILE` | CA bundle to verify the API server (else `K8S_VERIFY_TLS`) |
| `K8S_SESSION_SERVICE_ACCOUNT` / `K8S_SESSION_NAMESPACE` | the read-only account to mint sessions for |
| `K8S_SESSION_TTL_SECONDS` | requested session lifetime, default `21600` (6h) |

**A kubeconfig file.** The server watches the file and rebuilds its client when
the file changes, so the next tool call uses the new credential without a restart.

## Deployment scoping (per tier)

A deployment's reach is its tool surface and its namespace scope:
- **surface**: `EXPOSE_LOGS` + the log/list caps.
- **scope**: `K8S_ALLOWED_NAMESPACES` (app-side) mirroring the SA's RBAC
  (`RoleBinding`s in each in-scope namespace -> a read-only `ClusterRole`). The RBAC
  is the enforceable boundary; the env allowlist scopes enumeration output to match.

Example manifests (ServiceAccount, RBAC, Deployment, Service, config):

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: k8s-mcp
  namespace: team-a
spec:
  replicas: 1
  selector: {matchLabels: {app: k8s-mcp}}
  template:
    metadata: {labels: {app: k8s-mcp}}
    spec:
      serviceAccountName: k8s-mcp   # bound to a read-only ClusterRole via
                                     # RoleBindings in each in-scope namespace
      containers:
        - name: k8s-mcp
          image: registry.example.com/example-org/k8s-mcp:latest
          env:
            - {name: K8S_ALLOWED_NAMESPACES, value: "team-a,team-b"}
            - {name: EXPOSE_LOGS, value: "true"}
            - {name: MCP_TRANSPORT, value: "sse"}
          ports: [{containerPort: 3000}]
```

The RBAC (Role + RoleBinding per namespace, read-only verbs, `Secret` excluded)
must mirror `K8S_ALLOWED_NAMESPACES` - see "Deployment scoping" above.

## Image

Build the image from the included `Dockerfile`:

```bash
docker build -t <your-registry>/<your-namespace>/k8s-mcp:latest .
docker push  <your-registry>/<your-namespace>/k8s-mcp:latest
```

## Running locally

```bash
export K8S_ALLOWED_NAMESPACES=default
export EXPOSE_LOGS=true
uv sync --frozen        # Python 3.14 and uv
.venv/bin/python server.py   # stdio; uses ~/.kube/config
```

## Running tests

```bash
uv sync --frozen --extra dev
.venv/bin/python -m pytest tests/ -v        # hermetic, no cluster needed
```
