"""Kind resolution, and the kinds refused before any API call."""

import kinds
import server


def test_kind_aliases_resolve():
    assert kinds._resolve_kind("pods") == ("v1", "Pod")
    assert kinds._resolve_kind("po") == ("v1", "Pod")
    assert kinds._resolve_kind("Deployment") == ("apps/v1", "Deployment")
    assert kinds._resolve_kind("cj") == ("batch/v1", "CronJob")


def test_secrets_are_refused():
    for k in ("secret", "secrets", "Secret", "serviceaccount"):
        out = kinds._resolve_kind(k)
        assert isinstance(out, str) and "blocked" in out


def test_unknown_kind_errors():
    out = kinds._resolve_kind("frobnicators")
    assert isinstance(out, str) and "unknown" in out.lower()


def test_secrets_refused_before_cluster(no_cluster):
    no_cluster()
    assert "blocked" in server._dispatch("k8s_list", {"kind": "secrets", "namespace": "team-a"})
    assert "blocked" in server._dispatch("k8s_list", {"kind": "secrets", "namespace": "*"})
    assert "blocked" in server._dispatch("k8s_describe", {"kind": "secret", "name": "x", "namespace": "team-a"})


# -- ConfigMap gating -------------------------------------------------
#
# Refused as "not enabled here", distinct from Secret's "blocked".


def _reload_with_configmaps(reload_server, monkeypatch, value):
    monkeypatch.setenv("EXPOSE_CONFIGMAPS", value)
    return reload_server()


def test_configmaps_refused_by_default(monkeypatch, reload_server, no_cluster):
    mod = _reload_with_configmaps(reload_server, monkeypatch, "false")
    try:
        no_cluster()
        out = mod._dispatch("k8s_get", {"kind": "configmap", "name": "app-config", "namespace": "team-a"})
        assert "not enabled" in out and "EXPOSE_CONFIGMAPS" in out
    finally:
        _reload_with_configmaps(reload_server, monkeypatch, "false")


def test_configmaps_readable_when_enabled(monkeypatch, reload_server):
    _reload_with_configmaps(reload_server, monkeypatch, "true")
    try:
        assert kinds._resolve_kind("configmap") == ("v1", "ConfigMap")
        assert kinds._resolve_kind("cm") == ("v1", "ConfigMap")
    finally:
        _reload_with_configmaps(reload_server, monkeypatch, "false")


def test_secrets_stay_blocked_even_with_configmaps_on(monkeypatch, reload_server):
    """The flag opens one kind, not the neighbourhood."""
    _reload_with_configmaps(reload_server, monkeypatch, "true")
    try:
        out = kinds._resolve_kind("secret")
        assert isinstance(out, str) and "blocked" in out
    finally:
        _reload_with_configmaps(reload_server, monkeypatch, "false")
