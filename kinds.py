"""The readable-kind allowlist: caller strings -> (api_version, Kind).

`Secret` is deliberately absent, so no tool can read one whatever the RBAC
says. Built at import from `settings`; tests reload in dependency order to see
a changed flag.
"""

from __future__ import annotations

from errors import ErrorText
import settings

# Keys are lowercase aliases (singular + plural); values are (api_version, Kind).
KIND_MAP: dict[str, tuple[str, str]] = {}


def _reg(api_version: str, kind: str, *aliases: str) -> None:
    for a in aliases:
        KIND_MAP[a] = (api_version, kind)


_reg("v1", "Pod", "pod", "pods", "po")
_reg("v1", "Event", "event", "events", "ev")
_reg("v1", "Service", "service", "services", "svc")
_reg("v1", "Endpoints", "endpoints", "endpoint", "ep")
_reg("v1", "PersistentVolumeClaim", "persistentvolumeclaim", "persistentvolumeclaims", "pvc")
_reg("v1", "Node", "node", "nodes")  # cluster-scoped; needs RBAC
_reg("metrics.k8s.io/v1beta1", "NodeMetrics", "nodemetrics", "nodemetric")  # metrics-server; cluster-scoped
_reg("metrics.k8s.io/v1beta1", "PodMetrics", "podmetrics", "podmetric")  # per-pod usage, same API; namespaced
_reg("v1", "Namespace", "namespace", "namespaces", "ns")
_reg("apps/v1", "Deployment", "deployment", "deployments", "deploy")
_reg("apps/v1", "ReplicaSet", "replicaset", "replicasets", "rs")
_reg("apps/v1", "StatefulSet", "statefulset", "statefulsets", "sts")
_reg("apps/v1", "DaemonSet", "daemonset", "daemonsets", "ds")
_reg("batch/v1", "Job", "job", "jobs")
_reg("batch/v1", "CronJob", "cronjob", "cronjobs", "cj")
_reg("networking.k8s.io/v1", "Ingress", "ingress", "ingresses", "ing")
if settings.EXPOSE_CONFIGMAPS:
    _reg("v1", "ConfigMap", "configmap", "configmaps", "cm")

# Refused with an explicit message rather than "unknown kind".
_REFUSED_KINDS = {"secret", "secrets", "serviceaccount", "serviceaccounts"}

# Readable only when their flag is set; refused as "not enabled here", not "blocked".
_GATED_KINDS = {
    "configmap": "EXPOSE_CONFIGMAPS",
    "configmaps": "EXPOSE_CONFIGMAPS",
    "cm": "EXPOSE_CONFIGMAPS",
}

# Cluster-scoped kinds have no per-namespace boundary, so an all-namespaces
# list is refused on a scoped tier.
_CLUSTER_SCOPED_KINDS = {"Node", "Namespace", "NodeMetrics"}


def _check_ns(namespace: str) -> ErrorText | None:
    """Return an error string if namespace is not in the allowlist, else None."""
    if settings.ALLOWED_NAMESPACES is not None and namespace not in settings.ALLOWED_NAMESPACES:
        return ErrorText(f"Error: namespace '{namespace}' is not in the allowed set")
    return None


def _resolve_kind(kind: str) -> tuple[str, str] | ErrorText:
    """Map a user kind string to (api_version, Kind), or an error string."""
    key = kind.strip().lower()
    if key in _REFUSED_KINDS:
        return ErrorText(f"Error: kind '{kind}' is not readable on this instance (blocked)")
    if key in _GATED_KINDS and key not in KIND_MAP:
        return ErrorText(
            f"Error: kind '{kind}' is not enabled on this instance "
            f"(set {_GATED_KINDS[key]}=true to allow it)"
        )
    if key not in KIND_MAP:
        return ErrorText(
            f"Error: unknown or unsupported kind '{kind}'. "
            f"Supported: {', '.join(sorted({v[1] for v in KIND_MAP.values()}))}"
        )
    return KIND_MAP[key]
