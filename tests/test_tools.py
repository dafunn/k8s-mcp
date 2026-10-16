"""Which tools are listed, and dispatch of the ones that are not."""

import asyncio

import pytest

import server
import settings


def _tool_names():
    return {t.name for t in asyncio.run(server.list_tools(None, None)).tools}


@pytest.mark.parametrize("name", ["k8s_node_usage", "k8s_pod_usage", "k8s_describe"])
def test_tool_is_listed(name):
    assert name in _tool_names()


def test_unknown_tool():
    assert "unknown tool" in server._dispatch("k8s_bogus", {})


# -- Log gating -------------------------------------------------------


def test_logs_tool_hidden_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "EXPOSE_LOGS", False)
    names = _tool_names()
    assert "k8s_logs" not in names
    assert {"k8s_namespaces", "k8s_list", "k8s_get", "k8s_events"} <= names


def test_logs_tool_shown_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "EXPOSE_LOGS", True)
    assert "k8s_logs" in _tool_names()


def test_logs_dispatch_refused_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "EXPOSE_LOGS", False)
    out = server._dispatch("k8s_logs", {"name": "p", "namespace": "team-a"})
    assert "not enabled" in out


# -- Release version ------------------------------------------------


def test_release_version_is_dev_when_unset(monkeypatch):
    monkeypatch.delenv("K8S_MCP_VERSION", raising=False)
    assert server.release_version() == "dev"


def test_release_version_is_dev_when_blank(monkeypatch):
    monkeypatch.setenv("K8S_MCP_VERSION", "  ")
    assert server.release_version() == "dev"


def test_release_version_reports_the_build_value(monkeypatch):
    monkeypatch.setenv("K8S_MCP_VERSION", "0.2.0-3-gabc1234")
    assert server.release_version() == "0.2.0-3-gabc1234"


def test_server_reports_the_release_version():
    assert server._server.version == server.release_version()
