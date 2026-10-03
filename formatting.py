"""Rendering cluster objects as text, in the shapes `kubectl get`/`describe`/`top` use.

Pure functions over plain dicts; no cluster access. The resource-quantity
parsers live here because they only exist to render usage lines.
"""

from __future__ import annotations

from typing import Any

def _age(ts: Any) -> str:
    return str(ts) if ts else "-"


def _summarize_pod(item: dict, include_namespace: bool = False) -> str:
    meta = item.get("metadata", {})
    status = item.get("status", {})
    name = meta.get("name", "?")
    phase = status.get("phase", "?")
    cstats = status.get("containerStatuses") or []
    ready = sum(1 for c in cstats if c.get("ready"))
    total = len(cstats)
    restarts = sum(int(c.get("restartCount", 0)) for c in cstats)
    reason = status.get("reason", "")
    # Surface the most useful container waiting/terminated reason (this is where
    # ImagePullBackOff / CrashLoopBackOff / ErrImagePull live).
    for c in cstats:
        st = c.get("state", {}) or {}
        w = st.get("waiting") or {}
        t = st.get("terminated") or {}
        if w.get("reason"):
            reason = w["reason"]
            break
        if t.get("reason") and t.get("reason") != "Completed":
            reason = t["reason"]
            break
    # The node the pod landed on, so "what is on this node" can be answered from a list.
    node = (item.get("spec") or {}).get("nodeName") or "-"
    prefix = f"{meta.get('namespace', '?')}\t" if include_namespace else ""
    return (
        f"{prefix}{name}\t{phase}\t{ready}/{total}\t{restarts}\t{reason or '-'}"
        f"\t{node}\t{_age(meta.get('creationTimestamp'))}"
    )


def _summarize_generic(item: dict, include_namespace: bool = False) -> str:
    meta = item.get("metadata", {})
    name = meta.get("name", "?")
    status = item.get("status", {})
    # A compact status hint that works across kinds.
    hint = ""
    if isinstance(status, dict):
        if "readyReplicas" in status or "replicas" in status:
            hint = f"replicas={status.get('readyReplicas', 0)}/{status.get('replicas', 0)}"
        elif "succeeded" in status or "failed" in status:
            hint = f"succeeded={status.get('succeeded', 0)} failed={status.get('failed', 0)}"
        elif "phase" in status:
            hint = str(status.get("phase"))
    prefix = f"{meta.get('namespace', '?')}\t" if include_namespace else ""
    return f"{prefix}{name}\t{hint or '-'}\t{_age(meta.get('creationTimestamp'))}"


def _strip_noise(obj: dict) -> dict:
    """Drop high-noise / low-signal fields before returning a full object."""
    meta = obj.get("metadata")
    if isinstance(meta, dict):
        meta.pop("managedFields", None)
        ann = meta.get("annotations")
        if isinstance(ann, dict):
            ann.pop("kubectl.kubernetes.io/last-applied-configuration", None)
    return obj


def _filters_note(label_selector: str = "", node: str = "") -> str:
    """Spell out the filters behind an empty result, so "none found" reads as
    "the filter matched nothing" rather than "this kind is not visible here".
    """
    applied = []
    if label_selector:
        applied.append(f"label_selector={label_selector!r}")
    if node:
        applied.append(f"node={node!r}")
    if not applied:
        return "."
    return (
        " matching " + " and ".join(applied)
        + ". The kind is readable here - it is the filter that matched nothing, "
        "so retry without it to see what exists."
    )


def _format_event_line(e: dict, include_namespace: bool = False) -> str:
    io = e.get("involvedObject", {}) or {}
    fields = [
        str(e.get("type", "")),
        str(e.get("reason", "")),
        f"{io.get('kind', '')}/{io.get('name', '')}",
        str(e.get("count", 1)),
        str(e.get("lastTimestamp") or e.get("eventTime") or ""),
        str(e.get("message", "")).replace("\n", " ")[:200],
    ]
    if include_namespace:
        ns = e.get("metadata", {}).get("namespace", "?")
        fields.insert(0, ns)
    return "\t".join(fields)


def _describe_pod(pod: dict) -> list[str]:
    """The pod half of a describe: node, container states, what killed the last
    container, restart counts - the fields a full YAML buries under spec.
    """
    meta, spec, status = pod.get("metadata", {}), pod.get("spec", {}), pod.get("status", {})
    out = [
        f"Name:      {meta.get('name', '?')}",
        f"Namespace: {meta.get('namespace', '?')}",
        f"Node:      {spec.get('nodeName') or '(unscheduled)'}",
        f"Status:    {status.get('phase', '?')}" + (f" ({status.get('reason')})" if status.get("reason") else ""),
        f"IP:        {status.get('podIP') or '-'}",
        f"Start:     {status.get('startTime') or '-'}",
    ]
    if meta.get("labels"):
        out.append("Labels:    " + ", ".join(f"{k}={v}" for k, v in sorted(meta["labels"].items())))
    if spec.get("restartPolicy"):
        out.append(f"Restarts:  policy={spec['restartPolicy']}")

    conditions = status.get("conditions") or []
    if conditions:
        out.append("")
        out.append("Conditions:")
        for c in conditions:
            line = f"  {c.get('type', '?'):<18} {c.get('status', '?')}"
            if c.get("reason"):
                line += f"  {c['reason']}"
            if c.get("message"):
                line += f": {str(c['message'])[:120]}"
            out.append(line)

    statuses = {c.get("name"): c for c in (status.get("containerStatuses") or [])}
    init_statuses = {c.get("name"): c for c in (status.get("initContainerStatuses") or [])}
    for label, containers, table in (
        ("Init containers", spec.get("initContainers") or [], init_statuses),
        ("Containers", spec.get("containers") or [], statuses),
    ):
        if not containers:
            continue
        out.append("")
        out.append(f"{label}:")
        for c in containers:
            cs = table.get(c.get("name"), {})
            out.append(f"  {c.get('name', '?')}")
            out.append(f"    Image:    {c.get('image', '?')}")
            out.append(f"    Ready:    {cs.get('ready', False)}   Restarts: {cs.get('restartCount', 0)}")
            state = cs.get("state") or {}
            for kind_, body in state.items():
                detail = " ".join(
                    f"{k}={v}" for k, v in (body or {}).items()
                    if k in ("reason", "exitCode", "signal", "startedAt", "finishedAt", "message")
                )
                out.append(f"    State:    {kind_}" + (f"  {detail[:200]}" if detail else ""))
            last = cs.get("lastState") or {}
            for kind_, body in last.items():
                # exit code of the previous container: 137 is an OOM kill
                detail = " ".join(
                    f"{k}={v}" for k, v in (body or {}).items()
                    if k in ("reason", "exitCode", "signal", "startedAt", "finishedAt", "message")
                )
                out.append(f"    Last:     {kind_}" + (f"  {detail[:200]}" if detail else ""))
            res = c.get("resources") or {}
            if res:
                bits = []
                for field in ("requests", "limits"):
                    block = res.get(field) or {}
                    if block:
                        bits.append(f"{field}: " + ", ".join(f"{k}={v}" for k, v in sorted(block.items())))
                if bits:
                    out.append("    Resources: " + "; ".join(bits))
    volumes = spec.get("volumes") or []
    if volumes:
        out.append("")
        out.append("Volumes:   " + ", ".join(
            f"{v.get('name')}({next((k for k in v if k != 'name'), '?')})" for v in volumes
        ))
    return out


def _describe_generic(obj: dict, k8s_kind: str) -> list[str]:
    """A describe for kinds that are not pods: identity, then whatever status says."""
    meta, status = obj.get("metadata", {}), obj.get("status", {}) or {}
    out = [
        f"Name:      {meta.get('name', '?')}",
        f"Namespace: {meta.get('namespace', '?')}",
        f"Kind:      {k8s_kind}",
    ]
    if meta.get("labels"):
        out.append("Labels:    " + ", ".join(f"{k}={v}" for k, v in sorted(meta["labels"].items())))
    scalars = {k: v for k, v in status.items() if isinstance(v, (str, int, bool))}
    if scalars:
        out.append("")
        out.append("Status:")
        out += [f"  {k}: {v}" for k, v in sorted(scalars.items())]
    for c in status.get("conditions") or []:
        out.append(
            f"  {c.get('type', '?')}={c.get('status', '?')}"
            + (f" ({c.get('reason')})" if c.get("reason") else "")
        )
    return out


def _parse_cpu(value: Any) -> float:
    """A Kubernetes CPU quantity as cores.

    Metrics report nanocores, specs report cores or millicores, and the same
    field can arrive in either form depending on which API answered.
    """
    v = str(value or "0").strip()
    for suffix, scale in (("n", 1e-9), ("u", 1e-6), ("m", 1e-3)):
        if v.endswith(suffix):
            return float(v[:-1]) * scale
    return float(v or 0)


def _parse_mem(value: Any) -> float:
    """A Kubernetes memory quantity as bytes. Binary suffixes are checked first,
    so `Ki` is not read as `K`."""
    v = str(value or "0").strip()
    for unit, mult in (
        ("Ki", 2**10), ("Mi", 2**20), ("Gi", 2**30), ("Ti", 2**40),
        ("K", 1e3), ("M", 1e6), ("G", 1e9), ("T", 1e12),
    ):
        if v.endswith(unit):
            return float(v[: -len(unit)]) * mult
    return float(v or 0)


def _sum_resources(pod: dict, field: str) -> tuple[float, float]:
    """(cpu cores, memory bytes) summed over a pod's containers for requests or limits.

    Unset counts as zero, which downstream reads as "no limit".
    """
    cpu = mem = 0.0
    for c in pod.get("spec", {}).get("containers", []) or []:
        block = (c.get("resources") or {}).get(field) or {}
        cpu += _parse_cpu(block.get("cpu"))
        mem += _parse_mem(block.get("memory"))
    return cpu, mem


def _pod_usage_line(pod: dict, usage: dict | None, include_namespace: bool) -> tuple[float, str]:
    """One pod's usage row, and the memory fraction it sorts by."""
    meta = pod.get("metadata", {})
    name = meta.get("name", "?")
    prefix = f"{meta.get('namespace', '?')}\t" if include_namespace else ""
    if usage is None:
        return -1.0, f"{prefix}{name}\t(no metrics)\t(no metrics)\t-"

    containers = usage.get("containers", []) or []
    cpu_used = sum(_parse_cpu((c.get("usage") or {}).get("cpu")) for c in containers)
    mem_used = sum(_parse_mem((c.get("usage") or {}).get("memory")) for c in containers)
    cpu_limit, mem_limit = _sum_resources(pod, "limits")

    mib = 2**20
    # No limit: show the bare figure; there is no fraction to report.
    cpu_txt = (f"{cpu_used:.3f}/{cpu_limit:.2f} ({cpu_used / cpu_limit * 100:.0f}%)"
               if cpu_limit else f"{cpu_used:.3f} cores")
    mem_txt = (f"{mem_used / mib:.0f}/{mem_limit / mib:.0f}Mi ({mem_used / mem_limit * 100:.0f}%)"
               if mem_limit else f"{mem_used / mib:.0f}Mi")
    restarts = sum(int(c.get("restartCount", 0))
                   for c in (pod.get("status", {}).get("containerStatuses") or []))
    frac = mem_used / mem_limit if mem_limit else 0.0
    return frac, f"{prefix}{name}\t{cpu_txt}\t{mem_txt}\t{restarts}"
