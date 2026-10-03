"""Credentials: kubeconfig reload, and auth-key sessions that renew before expiry."""

import datetime
import os

import pytest

import cluster
import server
import settings


# -- Credential reload ------------------------------------------------
#
# The client is rebuilt when the kubeconfig file changes.


def _kubeconfig(tmp_path, token):
    p = tmp_path / "kubeconfig"
    p.write_text(f"""apiVersion: v1
kind: Config
clusters:
  - name: c
    cluster: {{server: https://cluster.example.com:6443, insecure-skip-tls-verify: true}}
contexts:
  - name: ctx
    context: {{cluster: c, user: u}}
current-context: ctx
users:
  - name: u
    user: {{token: {token}}}
""")
    return p


def _reset_client_cache():
    cluster._dyn = cluster._core = None
    cluster._kubeconfig_mtime = None


def test_client_is_rebuilt_when_the_kubeconfig_changes(tmp_path, monkeypatch):
    """Also pins that the file loaded is the file watched."""
    path = _kubeconfig(tmp_path, "first-token")
    monkeypatch.setenv("KUBECONFIG", str(path))
    monkeypatch.setattr(cluster, "_in_cluster", lambda: False)
    monkeypatch.setattr(cluster, "_auth_key", lambda: "")
    # Stub both clients: DynamicClient does API discovery over the network and
    # would reach the developer's own cluster.
    monkeypatch.setattr(cluster, "DynamicClient", lambda api: object())
    monkeypatch.setattr(cluster.client, "CoreV1Api", lambda api: object())
    loaded: list[str | None] = []
    monkeypatch.setattr(cluster.config, "load_kube_config",
                        lambda config_file=None: loaded.append(config_file))
    _reset_client_cache()
    try:
        first, _ = cluster._clients()
        assert loaded == [str(path)]  # the file watched is the file loaded

        again, _ = cluster._clients()
        assert again is first  # unchanged file: same client, no reload churn

        _kubeconfig(tmp_path, "second-token")
        os.utime(path, (1, 1))  # a distinct mtime, as a re-mint would produce
        rebuilt, _ = cluster._clients()
        assert rebuilt is not first
        assert loaded == [str(path), str(path)]
    finally:
        _reset_client_cache()


def test_kubeconfig_path_prefers_the_env_var(monkeypatch):
    monkeypatch.setenv("KUBECONFIG", "/run/secrets/kubeconfig:/other/config")
    assert cluster._kubeconfig_path() == "/run/secrets/kubeconfig"  # first entry wins


def test_kubeconfig_path_is_empty_in_cluster(monkeypatch, tmp_path):
    """No KUBECONFIG and no ~/.kube/config: nothing to watch, and none is needed -
    the in-cluster loader refreshes its own token."""
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.setattr(server.os.path, "exists", lambda p: False)
    assert cluster._kubeconfig_path() == ""


# -- Credential modes -------------------------------------------------
#
# Pins which mode is chosen and that a session renews before expiry.


class _FakeTokenStatus:
    def __init__(self, token, expiry):
        self.token, self.expiration_timestamp = token, expiry


class _FakeTokenResponse:
    def __init__(self, token, expiry):
        self.status = _FakeTokenStatus(token, expiry)


def _in(seconds):
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)


@pytest.fixture
def authkey_mode(tmp_path, monkeypatch):
    """Auth-key mode with the minting call stubbed; counts how often it mints."""
    key = tmp_path / "authkey"
    key.write_text("auth-key-value\n")
    monkeypatch.setattr(settings, "AUTHKEY_FILE", str(key))
    monkeypatch.setattr(settings, "K8S_API_SERVER", "https://cluster.example.com:6443")
    monkeypatch.setattr(settings, "SESSION_SA", "reader")
    monkeypatch.setattr(settings, "SESSION_NAMESPACE", "team-a")
    monkeypatch.setattr(settings, "SESSION_TTL", 21600)
    monkeypatch.setattr(cluster, "_in_cluster", lambda: False)
    cluster._dyn = cluster._core = None
    cluster._session_expiry = None

    minted = []

    def _fake_mint():
        minted.append(1)
        return f"session-{len(minted)}", _in(21600)

    monkeypatch.setattr(cluster, "_mint_session", _fake_mint)
    monkeypatch.setattr(cluster, "ApiClient", lambda *a, **k: object())
    monkeypatch.setattr(cluster, "DynamicClient", lambda api: object())
    monkeypatch.setattr(cluster.client, "CoreV1Api", lambda api: object())
    yield minted
    cluster._dyn = cluster._core = None
    cluster._session_expiry = None


def test_auth_key_mints_once_then_reuses(authkey_mode):
    cluster._clients()
    cluster._clients()
    cluster._clients()
    assert len(authkey_mode) == 1  # a live session is not re-minted on every call


def test_session_renews_at_the_halfway_mark(authkey_mode, monkeypatch):
    cluster._clients()
    assert len(authkey_mode) == 1
    # Just past halfway of a 6h token: due.
    monkeypatch.setattr(cluster, "_session_expiry", _in(21600 // 2 - 60))
    assert cluster._session_due() is True
    cluster._clients()
    assert len(authkey_mode) == 2


def test_session_not_renewed_before_halfway(authkey_mode, monkeypatch):
    cluster._clients()
    monkeypatch.setattr(cluster, "_session_expiry", _in(21600 // 2 + 600))
    assert cluster._session_due() is False
    cluster._clients()
    assert len(authkey_mode) == 1


def test_session_due_when_none_yet(monkeypatch):
    monkeypatch.setattr(cluster, "_session_expiry", None)
    assert cluster._session_due() is True


def test_a_short_grant_renews_sooner(monkeypatch):
    """The server may grant less than asked; renewal follows what it granted."""
    monkeypatch.setattr(settings, "SESSION_TTL", 21600)
    monkeypatch.setattr(cluster, "_session_expiry", _in(3600))  # only an hour left
    assert cluster._session_due() is True


def test_in_cluster_wins_over_an_auth_key(authkey_mode, monkeypatch):
    """A projected token refreshes itself, so it is preferred and mints nothing."""
    monkeypatch.setattr(cluster, "_in_cluster", lambda: True)
    monkeypatch.setattr(cluster.config, "load_incluster_config", lambda: None)
    cluster._dyn = cluster._core = None
    cluster._clients()
    assert authkey_mode == []


def test_bearer_token_carries_its_prefix(monkeypatch):
    """The client falls back for the token but not the prefix; see cluster._bearer_config."""
    monkeypatch.setattr(settings, "K8S_API_SERVER", "https://cluster.example.com:6443")
    cfg = cluster._bearer_config("tok123")
    assert cfg.auth_settings()["BearerToken"]["value"] == "Bearer tok123"


def test_bearer_config_needs_an_api_server(monkeypatch):
    monkeypatch.setattr(settings, "K8S_API_SERVER", "")
    with pytest.raises(RuntimeError, match="K8S_API_SERVER is required"):
        cluster._bearer_config("tok")


def test_mint_needs_a_target_account(monkeypatch, tmp_path):
    key = tmp_path / "authkey"
    key.write_text("k")
    monkeypatch.setattr(settings, "AUTHKEY_FILE", str(key))
    monkeypatch.setattr(settings, "SESSION_SA", "")
    monkeypatch.setattr(settings, "SESSION_NAMESPACE", "")
    with pytest.raises(RuntimeError, match="K8S_SESSION_SERVICE_ACCOUNT"):
        cluster._mint_session()


def test_auth_key_read_from_file_then_env(tmp_path, monkeypatch):
    key = tmp_path / "authkey"
    key.write_text("from-file\n")
    monkeypatch.setattr(settings, "AUTHKEY_FILE", str(key))
    monkeypatch.setenv("K8S_AUTHKEY", "from-env")
    assert cluster._auth_key() == "from-file"
    monkeypatch.setattr(settings, "AUTHKEY_FILE", str(tmp_path / "missing"))
    assert cluster._auth_key() == "from-env"
