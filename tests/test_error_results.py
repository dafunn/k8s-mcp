"""call_tool marks refusals and failures with isError, and only those."""

import asyncio

import pytest
from mcp.types import CallToolRequestParams

import server
import settings
from tests.conftest import pod


def _call(name, args):
    return asyncio.run(server.call_tool(None, CallToolRequestParams(name=name, arguments=args)))


def _text(result):
    return result.content[0].text


@pytest.mark.parametrize("tool, args, reason", [
    ("k8s_list", {"kind": "pods", "namespace": "team-c"}, "not in the allowed set"),
    ("k8s_list", {"kind": "secrets", "namespace": "team-a"}, "is not readable on this instance"),
    ("k8s_list", {"kind": "widgets", "namespace": "team-a"}, "unknown or unsupported kind"),
    ("k8s_list", {}, "'kind' is required"),
    ("k8s_get", {"kind": "pods", "name": "x"}, "'kind', 'name', and 'namespace' are required"),
    ("k8s_describe", {"kind": "pods", "name": "x", "namespace": "team-c"}, "not in the allowed set"),
    ("k8s_events", {"namespace": "team-c"}, "not in the allowed set"),
    ("k8s_node_usage", {}, "cluster-scoped and not available"),
    ("k8s_pod_usage", {"namespace": "team-c"}, "not in the allowed set"),
    ("k8s_bogus", {}, "unknown tool 'k8s_bogus'"),
])
def test_refusals_are_error_results(no_cluster, tool, args, reason):
    no_cluster(allow=["team-a"])
    result = _call(tool, args)
    assert result.is_error is True
    assert _text(result).startswith("Error")
    assert reason in _text(result)


def test_disabled_logs_tool_is_an_error_result(monkeypatch):
    monkeypatch.setattr(settings, "EXPOSE_LOGS", False)
    result = _call("k8s_logs", {"name": "p", "namespace": "team-a"})
    assert result.is_error is True
    assert "not enabled on this instance" in _text(result)


def test_not_found_is_an_error_result(fake_cluster):
    fake_cluster(allow=["team-a"], Pod=[pod("api")])
    result = _call("k8s_get", {"kind": "pods", "name": "missing", "namespace": "team-a"})
    assert result.is_error is True
    assert "no Pod named 'missing'" in _text(result)


def test_cluster_failure_is_an_error_result(fake_cluster):
    fake_cluster(allow=["team-a"], fail=RuntimeError("forbidden"))
    result = _call("k8s_list", {"kind": "pods", "namespace": "team-a"})
    assert result.is_error is True
    assert _text(result) == "Error: forbidden"


def test_a_normal_result_is_not_an_error(fake_cluster):
    fake_cluster(allow=["team-a"], Pod=[pod("api")])
    result = _call("k8s_list", {"kind": "pods", "namespace": "team-a"})
    assert not result.is_error
    assert "api" in _text(result)


def test_an_empty_list_is_not_an_error(fake_cluster):
    fake_cluster(allow=["team-a"])
    result = _call("k8s_list", {"kind": "pods", "namespace": "team-a"})
    assert not result.is_error
    assert _text(result).startswith("No Pod found")
