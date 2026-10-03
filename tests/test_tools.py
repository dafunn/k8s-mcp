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
