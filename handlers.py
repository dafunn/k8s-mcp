"""One function per tool, each returning the text the caller sees.

Each handler checks scope, resolves the kind, fetches, and hands the result to
`formatting`. Refusals are returned as `ErrorText` rather than raised, so the
caller sees the reason and call_tool marks the result as an error. Imports
settings/kinds/cluster/formatting/errors; nothing imports back.
"""

from __future__ import annotations

from typing import Any

import yaml

import cluster
from errors import ErrorText
import formatting
import kinds
import settings

def _handle_namespaces() -> str:
    if settings.ALLOWED_NAMESPACES is not None:
        return "Allowed namespaces:\n" + "\n".join(sorted(settings.ALLOWED_NAMESPACES))
    try:
        dyn, _ = cluster._clients()
        res = dyn.resources.get(api_version="v1", kind="Namespace")
        items = res.get().to_dict().get("items", [])
        return "\n".join(i.get("metadata", {}).get("name", "?") for i in items)
    except Exception as e:  # noqa: BLE001
        return ErrorText(f"Error: {e}")


def _node_field_selector(k8s_kind: str, node: str) -> str | None:
    """The field selector pinning a Pod list to one node, or None if unset.

    `spec.nodeName` is filtered on the API server, so LIST_MAX_ITEMS still
    bounds the result.
    """
    if not node:
        return None
    if k8s_kind != "Pod":
        raise ValueError(
            f"'node' only applies to pods; {k8s_kind} is not scheduled onto a node"
        )
    return f"spec.nodeName={node}"


def _handle_list(kind: str, namespace: str, label_selector: str = "", node: str = "") -> str:
    resolved = kinds._resolve_kind(kind)
    if isinstance(resolved, str):
        return resolved
    api_version, k8s_kind = resolved
    try:
        field_selector = _node_field_selector(k8s_kind, node)
    except ValueError as e:
        return ErrorText(f"Error: {e}")

    if namespace in ("", "*"):
        return _handle_list_all_namespaces(api_version, k8s_kind, label_selector, node)

    # A glob lists each matching namespace in turn; the all-namespaces path
    # above keeps its single cluster-wide call on an unscoped tier.
    if cluster._has_wildcard(namespace):
        matched = cluster._resolve_namespaces(namespace)
        if isinstance(matched, str):
            return matched
        blocks = [
            _handle_list(kind, ns, label_selector, node) for ns in matched
        ]
        kept = [b for b in blocks if not b.startswith(("No ", "Error:"))]
        if not kept:
            return (
                f"No {k8s_kind} found in the {len(matched)} namespaces matching "
                f"'{namespace}': {', '.join(matched)}" + formatting._filters_note(label_selector, node)
            )
        return "\n\n".join(kept)

    err = kinds._check_ns(namespace)
    if err:
        return err
    try:
        dyn, _ = cluster._clients()
        res = dyn.resources.get(api_version=api_version, kind=k8s_kind)
        kwargs: dict[str, Any] = {"namespace": namespace}
        if label_selector:
            kwargs["label_selector"] = label_selector
        if field_selector:
            kwargs["field_selector"] = field_selector
        items = res.get(**kwargs).to_dict().get("items", [])
        truncated = len(items) > settings.LIST_MAX_ITEMS
        items = items[:settings.LIST_MAX_ITEMS]
        if not items:
            return (
                f"No {k8s_kind} found in namespace '{namespace}'"
                + formatting._filters_note(label_selector, node)
            )
        if k8s_kind == "Pod":
            header = "NAME\tPHASE\tREADY\tRESTARTS\tREASON\tNODE\tCREATED"
            lines = [formatting._summarize_pod(i) for i in items]
        else:
            header = "NAME\tSTATUS\tCREATED"
            lines = [formatting._summarize_generic(i) for i in items]
        out = f"=== {k8s_kind} in {namespace} ({len(items)}) ===\n{header}\n" + "\n".join(lines)
        if truncated:
            out += f"\n[TRUNCATED - showing first {settings.LIST_MAX_ITEMS}. Narrow with label_selector.]"
        return out
    except Exception as e:  # noqa: BLE001
        return ErrorText(f"Error: {e}")


def _handle_list_all_namespaces(
    api_version: str, k8s_kind: str, label_selector: str, node: str = ""
) -> str:
    """List a kind across all namespaces this instance can access.

    Unscoped tier: one cluster-wide call. Scoped tier: loop the allowlist,
    never a cluster-wide call.
    """
    if settings.ALLOWED_NAMESPACES is not None and k8s_kind in kinds._CLUSTER_SCOPED_KINDS:
        return ErrorText(
            f"Error: kind '{k8s_kind}' is cluster-scoped and not available "
            "on a namespace-scoped tier"
        )
    try:
        field_selector = _node_field_selector(k8s_kind, node)
    except ValueError as e:
        return ErrorText(f"Error: {e}")
    try:
        dyn, _ = cluster._clients()
        res = dyn.resources.get(api_version=api_version, kind=k8s_kind)
        if settings.ALLOWED_NAMESPACES is None:
            kwargs: dict[str, Any] = {}
            if label_selector:
                kwargs["label_selector"] = label_selector
            if field_selector:
                kwargs["field_selector"] = field_selector
            items = res.get(**kwargs).to_dict().get("items", [])
        else:
            items = []
            for ns in sorted(settings.ALLOWED_NAMESPACES):
                kwargs = {"namespace": ns}
                if label_selector:
                    kwargs["label_selector"] = label_selector
                if field_selector:
                    kwargs["field_selector"] = field_selector
                items.extend(res.get(**kwargs).to_dict().get("items", []))
        truncated = len(items) > settings.LIST_MAX_ITEMS
        items = items[:settings.LIST_MAX_ITEMS]
        if not items:
            return (
                f"No {k8s_kind} found across all namespaces"
                + formatting._filters_note(label_selector, node)
            )
        if k8s_kind == "Pod":
            header = "NAMESPACE\tNAME\tPHASE\tREADY\tRESTARTS\tREASON\tNODE\tCREATED"
            lines = [formatting._summarize_pod(i, include_namespace=True) for i in items]
        else:
            header = "NAMESPACE\tNAME\tSTATUS\tCREATED"
            lines = [formatting._summarize_generic(i, include_namespace=True) for i in items]
        out = f"=== {k8s_kind} across all namespaces ({len(items)}) ===\n{header}\n" + "\n".join(lines)
        if truncated:
            out += f"\n[TRUNCATED - showing first {settings.LIST_MAX_ITEMS}. Narrow with label_selector.]"
        return out
    except Exception as e:  # noqa: BLE001
        return ErrorText(f"Error: {e}")


def _extract_path(obj: Any, path: str) -> Any:
    """Walk a dotted path into an object: `spec.containers.0.image`.

    A missing key answers with the keys that exist at that point.
    """
    current = obj
    walked: list[str] = []
    for segment in [p for p in path.split(".") if p]:
        if isinstance(current, list):
            try:
                current = current[int(segment)]
            except (ValueError, IndexError):
                where = ".".join(walked) or "(root)"
                raise ValueError(
                    f"'{segment}' is not a valid index at {where}; "
                    f"it holds a list of {len(current)}"
                ) from None
        elif isinstance(current, dict):
            if segment not in current:
                where = ".".join(walked) or "(root)"
                raise ValueError(
                    f"no '{segment}' at {where}; available: {', '.join(sorted(current)) or '(nothing)'}"
                )
            current = current[segment]
        else:
            where = ".".join(walked) or "(root)"
            raise ValueError(f"{where} is a value, not something '{segment}' can be read from")
        walked.append(segment)
    return current


def _handle_get(kind: str, name: str, namespace: str, path: str = "") -> str:
    resolved = kinds._resolve_kind(kind)
    if isinstance(resolved, str):
        return resolved
    api_version, k8s_kind = resolved
    namespaces = cluster._resolve_namespaces(namespace)
    if isinstance(namespaces, str):
        return namespaces
    try:
        dyn, _ = cluster._clients()
        res = dyn.resources.get(api_version=api_version, kind=k8s_kind)
        hits = cluster._find_in_namespaces(res, name, namespaces)
    except Exception as e:  # noqa: BLE001
        return ErrorText(f"Error: {e}")
    if not hits:
        return cluster._not_found(k8s_kind, name, namespaces, namespace)
    if len(hits) > 1:
        return cluster._ambiguous(k8s_kind, name, hits, namespace)
    obj = formatting._strip_noise(hits[0][1])

    if path:
        try:
            obj = _extract_path(obj, path)
        except ValueError as e:
            return ErrorText(f"Error: {e}")
        if not isinstance(obj, (dict, list)):
            return f"{path}: {obj}"   # a scalar reads better on one line than as a yaml doc

    text = yaml.safe_dump(obj, default_flow_style=False, sort_keys=False)
    if len(text) > settings.GET_MAX_BYTES:
        # Truncation loses the end of the object, so the trailer points at `path`.
        text = text[:settings.GET_MAX_BYTES] + (
            f"\n[truncated at {settings.GET_MAX_BYTES} bytes. Pass `path` to select a subtree - "
            f"for example path='status' or path='spec.containers' - or use k8s_describe "
            f"for the troubleshooting summary.]"
        )
    return text


def _handle_describe(kind: str, name: str, namespace: str) -> str:
    """One object's troubleshooting view: its state, then the events about it."""
    resolved = kinds._resolve_kind(kind)
    if isinstance(resolved, str):
        return resolved
    api_version, k8s_kind = resolved
    namespaces = cluster._resolve_namespaces(namespace)
    if isinstance(namespaces, str):
        return namespaces
    try:
        dyn, _ = cluster._clients()
        res = dyn.resources.get(api_version=api_version, kind=k8s_kind)
        hits = cluster._find_in_namespaces(res, name, namespaces)
    except Exception as e:  # noqa: BLE001
        return ErrorText(f"Error: {e}")
    if not hits:
        return cluster._not_found(k8s_kind, name, namespaces, namespace)
    if len(hits) > 1:
        return cluster._ambiguous(k8s_kind, name, hits, namespace)
    # The namespace the object was actually found in, which a pattern did not name.
    namespace, obj = hits[0]

    lines = formatting._describe_pod(obj) if k8s_kind == "Pod" else formatting._describe_generic(obj, k8s_kind)

    # Events last, newest at the bottom.
    lines.append("")
    lines.append("Events:")
    try:
        dyn, _ = cluster._clients()
        ev = dyn.resources.get(api_version="v1", kind="Event")
        items = ev.get(
            namespace=namespace, field_selector=f"involvedObject.name={name}"
        ).to_dict().get("items", [])
        items.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")
        items = items[-20:]
        if not items:
            lines.append("  (none - events age out, so this says nothing about older failures)")
        else:
            lines.append("  TYPE\tREASON\tOBJECT\tCOUNT\tLAST\tMESSAGE")
            lines += ["  " + formatting._format_event_line(e) for e in items]
    except Exception as e:  # noqa: BLE001
        lines.append(f"  (could not read events: {e})")
    return "\n".join(lines)


def _handle_events(namespace: str, name: str = "") -> str:
    if namespace in ("", "*"):
        return _handle_events_all_namespaces(name)

    err = kinds._check_ns(namespace)
    if err:
        return err
    try:
        dyn, _ = cluster._clients()
        res = dyn.resources.get(api_version="v1", kind="Event")
        kwargs: dict[str, Any] = {"namespace": namespace}
        if name:
            kwargs["field_selector"] = f"involvedObject.name={name}"
        items = res.get(**kwargs).to_dict().get("items", [])
        # Order by last-seen time.
        items.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")
        items = items[-settings.LIST_MAX_ITEMS:]
        if not items:
            scope = f" for '{name}'" if name else ""
            return f"No events in namespace '{namespace}'{scope}."
        lines = ["TYPE\tREASON\tOBJECT\tCOUNT\tLAST\tMESSAGE"]
        lines.extend(formatting._format_event_line(e) for e in items)
        return f"=== Events in {namespace} ({len(items)}) ===\n" + "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return ErrorText(f"Error: {e}")


def _handle_node_usage(node: str = "") -> str:
    """CPU and memory in use per node, against allocatable, plus the Ready
    condition and pressure.
    """
    if settings.ALLOWED_NAMESPACES is not None:
        return ErrorText(
            "Error: node usage is cluster-scoped and not available on a "
            "namespace-scoped tier"
        )
    try:
        dyn, _ = cluster._clients()
        node_res = dyn.resources.get(api_version="v1", kind="Node")
        nodes = (
            [node_res.get(name=node).to_dict()] if node
            else node_res.get().to_dict().get("items", [])
        )
    except Exception as e:  # noqa: BLE001
        return ErrorText(f"Error reading nodes: {e}")
    try:
        metrics_res = dyn.resources.get(api_version="metrics.k8s.io/v1beta1", kind="NodeMetrics")
        metrics = (
            [metrics_res.get(name=node).to_dict()] if node
            else metrics_res.get().to_dict().get("items", [])
        )
    except Exception as e:  # noqa: BLE001
        return ErrorText(
            f"Error reading node metrics: {e}\n"
            "This needs metrics-server installed and read access to "
            "metrics.k8s.io/nodes; node conditions are still available via "
            "k8s_get node <name>."
        )

    used = {m.get("metadata", {}).get("name"): m.get("usage") or {} for m in metrics}
    lines = ["NAME\tCPU\tMEM\tREADY"]
    for n in sorted(nodes, key=lambda x: x.get("metadata", {}).get("name") or ""):
        name = n.get("metadata", {}).get("name") or "?"
        status = n.get("status") or {}
        allocatable = status.get("allocatable") or {}
        conditions = {c.get("type"): c.get("status") for c in (status.get("conditions") or [])}
        ready = "Ready" if conditions.get("Ready") == "True" else "NotReady"
        # A node under pressure is unhealthy even while it reports Ready, so say so.
        pressure = [k for k in ("MemoryPressure", "DiskPressure", "PIDPressure")
                    if conditions.get(k) == "True"]
        if pressure:
            ready += " (" + ",".join(pressure) + ")"

        u = used.get(name)
        if u is None:
            lines.append(f"{name}\t(no metrics)\t(no metrics)\t{ready}")
            continue
        cpu_used, cpu_cap = formatting._parse_cpu(u.get("cpu")), formatting._parse_cpu(allocatable.get("cpu"))
        mem_used, mem_cap = formatting._parse_mem(u.get("memory")), formatting._parse_mem(allocatable.get("memory"))
        cpu_pct = f" ({cpu_used / cpu_cap * 100:.0f}%)" if cpu_cap else ""
        mem_pct = f" ({mem_used / mem_cap * 100:.0f}%)" if mem_cap else ""
        gib = 2**30
        lines.append(
            f"{name}\t{cpu_used:.2f}/{cpu_cap:.0f} cores{cpu_pct}"
            f"\t{mem_used / gib:.1f}/{mem_cap / gib:.1f} GiB{mem_pct}\t{ready}"
        )
    if len(lines) == 1:
        return "No nodes found."
    return f"=== Node usage ({len(lines) - 1}) ===\n" + "\n".join(lines)


def _handle_pod_usage(namespace: str = "", name: str = "") -> str:
    """CPU and memory in use per pod against its limits, with restart count.

    Rows sort by memory fraction, worst first.
    """
    namespaces: list[str]
    if namespace in ("", "*"):
        if settings.ALLOWED_NAMESPACES is None:
            namespaces = []  # cluster-wide in one call
        else:
            namespaces = sorted(settings.ALLOWED_NAMESPACES)
    else:
        err = kinds._check_ns(namespace)
        if err:
            return err
        namespaces = [namespace]

    if name and not namespaces:
        return ErrorText("Error: 'namespace' is required when asking for one pod by name")

    try:
        dyn, _ = cluster._clients()
        pod_res = dyn.resources.get(api_version="v1", kind="Pod")
    except Exception as e:  # noqa: BLE001
        return ErrorText(f"Error reading pods: {e}")
    try:
        metrics_res = dyn.resources.get(api_version="metrics.k8s.io/v1beta1", kind="PodMetrics")
    except Exception as e:  # noqa: BLE001
        return ErrorText(
            f"Error reading pod metrics: {e}\n"
            "This needs metrics-server installed and read access to "
            "metrics.k8s.io/pods; k8s_list pods still reports phase, restarts and "
            "the failure reason without it."
        )

    pods: list[dict] = []
    metrics: list[dict] = []
    try:
        # scoped tier: per allowed namespace; unscoped: one cluster-wide call
        scopes: list[dict] = [{"namespace": ns} for ns in namespaces] or [{}]
        for kwargs in scopes:
            if name:
                kwargs = {**kwargs, "name": name}
                pods.append(pod_res.get(**kwargs).to_dict())
                metrics.append(metrics_res.get(**kwargs).to_dict())
            else:
                pods.extend(pod_res.get(**kwargs).to_dict().get("items", []))
                metrics.extend(metrics_res.get(**kwargs).to_dict().get("items", []))
    except Exception as e:  # noqa: BLE001
        return ErrorText(f"Error: {e}")

    used = {
        (m.get("metadata", {}).get("namespace"), m.get("metadata", {}).get("name")): m
        for m in metrics
    }
    include_ns = len(namespaces) != 1
    rows = [
        formatting._pod_usage_line(
            pod,
            used.get((pod.get("metadata", {}).get("namespace"), pod.get("metadata", {}).get("name"))),
            include_ns,
        )
        for pod in pods
    ]
    if not rows:
        return "No pods found."
    rows.sort(key=lambda r: r[0], reverse=True)
    truncated = len(rows) > settings.LIST_MAX_ITEMS
    rows = rows[:settings.LIST_MAX_ITEMS]

    scope_txt = namespaces[0] if len(namespaces) == 1 else "all namespaces"
    header = ("NAMESPACE\tNAME\tCPU\tMEM\tRESTARTS" if include_ns
              else "NAME\tCPU\tMEM\tRESTARTS")
    out = (f"=== Pod usage in {scope_txt} ({len(rows)}) ===\n{header}\n"
           + "\n".join(r[1] for r in rows))
    if truncated:
        out += f"\n[TRUNCATED - showing the {settings.LIST_MAX_ITEMS} highest by memory fraction.]"
    return out


def _handle_events_all_namespaces(name: str = "") -> str:
    """List events across all namespaces this instance can access; same scoping
    rule as _handle_list_all_namespaces. Event is always namespaced, so no
    cluster-scoped-kind check.
    """
    try:
        dyn, _ = cluster._clients()
        res = dyn.resources.get(api_version="v1", kind="Event")
        field_selector = f"involvedObject.name={name}" if name else ""
        if settings.ALLOWED_NAMESPACES is None:
            kwargs: dict[str, Any] = {}
            if field_selector:
                kwargs["field_selector"] = field_selector
            items = res.get(**kwargs).to_dict().get("items", [])
        else:
            items = []
            for ns in sorted(settings.ALLOWED_NAMESPACES):
                kwargs = {"namespace": ns}
                if field_selector:
                    kwargs["field_selector"] = field_selector
                items.extend(res.get(**kwargs).to_dict().get("items", []))
        # Order by last-seen time.
        items.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")
        items = items[-settings.LIST_MAX_ITEMS:]
        if not items:
            scope = f" for '{name}'" if name else ""
            return f"No events across all namespaces{scope}."
        lines = ["NAMESPACE\tTYPE\tREASON\tOBJECT\tCOUNT\tLAST\tMESSAGE"]
        lines.extend(formatting._format_event_line(e, include_namespace=True) for e in items)
        return f"=== Events across all namespaces ({len(items)}) ===\n" + "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return ErrorText(f"Error: {e}")


def _handle_logs(
    name: str, namespace: str, container: str = "", tail_lines: int = 0, previous: bool = False
) -> str:
    err = kinds._check_ns(namespace)
    if err:
        return err
    tail = tail_lines if tail_lines and tail_lines > 0 else settings.LOG_MAX_LINES
    tail = min(tail, settings.LOG_MAX_LINES)
    try:
        _, core = cluster._clients()
        text = core.read_namespaced_pod_log(
            name=name,
            namespace=namespace,
            container=container or None,
            tail_lines=tail,
            previous=previous,
            timestamps=False,
        )
        if len(text) > settings.LOG_MAX_BYTES:
            text = text[-settings.LOG_MAX_BYTES:]
            text = "[...truncated to last %d bytes...]\n" % settings.LOG_MAX_BYTES + text
        return text or f"(no log output for {namespace}/{name})"
    except Exception as e:  # noqa: BLE001
        return ErrorText(f"Error: {e}")
