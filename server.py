"""k8s-mcp: read-only Kubernetes MCP server.

Tool exposure is config-driven (settings.py); data scope is the namespace
allowlist plus the ServiceAccount's RBAC. Read verbs only; `Secret` is never
readable.
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server import Server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

from errors import ErrorText
import handlers
import settings


# -- Tool definitions --------------------------------------------------

_BASE_TOOLS: list[Tool] = [
    Tool(
        name="k8s_namespaces",
        description=(
            "List the Kubernetes namespaces this instance is scoped to. When "
            "K8S_ALLOWED_NAMESPACES is set, only those are shown."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="k8s_list",
        description=(
            "List resources of a kind, in one namespace or across every namespace "
            "this instance can access - omit `namespace`, or pass \"*\", for all of "
            "them. For pods it reports phase, ready count, restarts, the container "
            "waiting or terminated reason (ImagePullBackOff, CrashLoopBackOff, ...), "
            "and the node it is scheduled on; `node` narrows a pod list to one node. "
            "Supported kinds: pods, "
            "deployments, replicasets, statefulsets, daemonsets, jobs, cronjobs, "
            "services, endpoints, ingresses, persistentvolumeclaims, events, nodes, "
            "namespaces."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "Resource kind (e.g. pods, deployments)"},
                "namespace": {
                    "type": "string",
                    "description": (
                        'Namespace to list in. Use "*" or leave empty for every '
                        "namespace this instance can access, or a pattern like "
                        '"team*" for those matching it.'
                    ),
                },
                "label_selector": {
                    "type": "string",
                    "description": "Optional label selector (e.g. app=foo)",
                    "default": "",
                },
                "node": {
                    "type": "string",
                    "description": (
                        "Pods only: limit to pods scheduled on this node. Combines "
                        "with `namespace`, so leave that empty for every pod on the "
                        "node, cluster-wide."
                    ),
                    "default": "",
                },
            },
            "required": ["kind"],
        },
    ),
    Tool(
        name="k8s_get",
        description=(
            "Get one resource by name as YAML (read-only; managedFields stripped). "
            "Pass `path` to select part of it - 'status', 'spec.containers', "
            "'spec.containers.0.image'. A path that does not exist answers with the "
            "keys that do. For 'why is this pod unhealthy', prefer k8s_describe, "
            "which summarises the same object together with its events."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "Resource kind"},
                "name": {"type": "string", "description": "Resource name"},
                "namespace": {
                    "type": "string",
                    "description": (
                        'Namespace, or a pattern: `*` searches every namespace this '
                        'instance can reach and `team*` searches those matching it. '
                        "A name found in more than one is reported rather than guessed."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Dotted path into the object, e.g. 'status.conditions' or "
                        "'spec.containers.0.resources'. Omit for the whole object."
                    ),
                    "default": "",
                },
            },
            "required": ["kind", "name", "namespace"],
        },
    ),
    Tool(
        name="k8s_node_usage",
        description=(
            "CPU and memory in use on each node, against what the node has "
            "allocatable, plus its Ready condition and any memory, disk or PID "
            "pressure. The first call for 'is the cluster healthy' or 'why is "
            "scheduling failing'. Needs metrics-server and read access to "
            "metrics.k8s.io; without them it says so, and node conditions are "
            "still readable with k8s_get node."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "node": {
                    "type": "string",
                    "description": "One node by name; omit for every node.",
                    "default": "",
                },
            },
        },
    ),
    Tool(
        name="k8s_describe",
        description=(
            "The troubleshooting view of one object: for a pod, where it is running, "
            "each container's image, ready state, restart count, current state and "
            "what killed the previous one (exit code and reason), its conditions and "
            "resources - then the recent events about it. Prefer this over k8s_get "
            "for 'why is this pod unhealthy'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "Resource kind (usually pods)"},
                "name": {"type": "string", "description": "Object name"},
                "namespace": {
                    "type": "string",
                    "description": (
                        'Namespace, or a pattern: `*` for every namespace this instance '
                        'can reach, `team*` for those matching it.'
                    ),
                },
            },
            "required": ["kind", "name", "namespace"],
        },
    ),
    Tool(
        name="k8s_pod_usage",
        description=(
            "CPU and memory in use per pod, against the limits that pod set, with "
            "its restart count. Rows sort by memory fraction, worst first. Omit "
            "`namespace` for every namespace this instance can reach; pass `name` "
            "with a namespace for one pod."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace": {
                    "type": "string",
                    "description": (
                        'Namespace to report on. Use "*" or leave empty for every '
                        "namespace this instance can access."
                    ),
                    "default": "",
                },
                "name": {
                    "type": "string",
                    "description": "One pod by name; requires a namespace.",
                    "default": "",
                },
            },
        },
    ),
    Tool(
        name="k8s_events",
        description=(
            "List recent events in a namespace, or across all namespaces this "
            "instance can access, newest last. Pass `name` to filter to one "
            "object's events - this is where the kubelet reason for an image pull "
            "failure, scheduling failure, or OOMKill is spelled out."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namespace": {
                    "type": "string",
                    "description": (
                        'Namespace to list events in. Use "*" or leave empty to '
                        "list across every namespace this instance can access."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": "Optional object name to filter events for",
                    "default": "",
                },
            },
        },
    ),
]

_LOGS_TOOL = Tool(
    name="k8s_logs",
    description=(
        "Read a pod's container logs (read-only), most recent lines. Use to see the "
        "actual error a container printed before failing. Capped at K8S_LOG_MAX_LINES "
        "lines / K8S_LOG_MAX_BYTES bytes. Logs may contain sensitive data."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Pod name"},
            "namespace": {"type": "string", "description": "Namespace"},
            "container": {
                "type": "string",
                "description": "Container name (omit for the pod's default/first container)",
                "default": "",
            },
            "tail_lines": {
                "type": "integer",
                "description": "How many trailing lines to fetch (capped)",
                "default": 0,
            },
            "previous": {
                "type": "boolean",
                "description": "Read the previous (crashed) container instance's log",
                "default": False,
            },
        },
        "required": ["name", "namespace"],
    },
)

# -- MCP server -------------------------------------------------------

def release_version() -> str:
    """The release this build reports to clients: K8S_MCP_VERSION,
    stamped into the image from the k8s-mcp-v* tag at build time, else `dev`."""
    return os.environ.get("K8S_MCP_VERSION", "").strip() or "dev"


_server = Server("k8s-mcp", version=release_version())


async def list_tools(_ctx: Any, _params: PaginatedRequestParams) -> ListToolsResult:
    tools = list(_BASE_TOOLS)
    if settings.EXPOSE_LOGS:
        tools.append(_LOGS_TOOL)
    return ListToolsResult(tools=tools)


async def call_tool(_ctx: Any, params: CallToolRequestParams) -> CallToolResult:
    text = _dispatch(params.name, params.arguments or {})
    return CallToolResult(content=[TextContent(type="text", text=text)],
                          is_error=isinstance(text, ErrorText))


# Each handler is registered with its wire method and params model; the runner
# validates a request against the model before calling the handler.
_server.add_request_handler("tools/list", PaginatedRequestParams, list_tools)
_server.add_request_handler("tools/call", CallToolRequestParams, call_tool)


def _dispatch(name: str, args: dict) -> str:
    if name == "k8s_namespaces":
        return handlers._handle_namespaces()
    if name == "k8s_list":
        kind = args.get("kind")
        if not kind:
            return ErrorText("Error: 'kind' is required")
        return handlers._handle_list(
            kind, args.get("namespace") or "", args.get("label_selector", ""), args.get("node", "")
        )
    if name == "k8s_get":
        kind, nm, ns = args.get("kind"), args.get("name"), args.get("namespace")
        if not kind or not nm or not ns:
            return ErrorText("Error: 'kind', 'name', and 'namespace' are required")
        return handlers._handle_get(kind, nm, ns, args.get("path", ""))
    if name == "k8s_describe":
        kind, nm, ns = args.get("kind"), args.get("name"), args.get("namespace")
        if not kind or not nm or not ns:
            return ErrorText("Error: 'kind', 'name', and 'namespace' are required")
        return handlers._handle_describe(kind, nm, ns)
    if name == "k8s_events":
        return handlers._handle_events(args.get("namespace") or "", args.get("name", ""))
    if name == "k8s_node_usage":
        return handlers._handle_node_usage(args.get("node", ""))
    if name == "k8s_pod_usage":
        return handlers._handle_pod_usage(args.get("namespace") or "", args.get("name", ""))
    if name == "k8s_logs":
        if not settings.EXPOSE_LOGS:
            return ErrorText(f"Error: tool '{name}' is not enabled on this instance")
        nm, ns = args.get("name"), args.get("namespace")
        if not nm or not ns:
            return ErrorText("Error: 'name' and 'namespace' are required")
        return handlers._handle_logs(
            nm, ns, args.get("container", ""), int(args.get("tail_lines", 0)), bool(args.get("previous", False))
        )
    return ErrorText(f"Error: unknown tool '{name}'")


# -- Entry point ------------------------------------------------------

if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport == "sse":
        from mcp.server.sse import SseServerTransport
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import Response
        from starlette.routing import Mount, Route
        import uvicorn

        host = os.getenv("MCP_HOST", "0.0.0.0")
        port = int(os.getenv("MCP_PORT", "3000"))

        sse = SseServerTransport("/messages/")

        async def handle_sse(request: Request) -> Response:
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as (read_stream, write_stream):
                await _server.run(
                    read_stream,
                    write_stream,
                    _server.create_initialization_options(),
                )
            # Return an empty Response so Starlette doesn't call None as an
            # ASGI app after the SSE handler hijacks the connection.
            return Response()

        web_app = Starlette(
            routes=[
                Route("/sse", endpoint=handle_sse),
                Mount("/messages/", app=sse.handle_post_message),
            ]
        )
        uvicorn.run(web_app, host=host, port=port)
    else:
        import asyncio
        from mcp.server.stdio import stdio_server

        async def _run_stdio() -> None:
            async with stdio_server() as (read_stream, write_stream):
                await _server.run(
                    read_stream,
                    write_stream,
                    _server.create_initialization_options(),
                )

        asyncio.run(_run_stdio())
