"""API clients and namespace resolution.

Authentication is tried in order: in-cluster projected token, an auth key that
mints short-lived session tokens, or a kubeconfig file watched for change.
Namespace resolution asks the cluster which namespaces this ServiceAccount can
see, filtered by the allowlist.
"""

from __future__ import annotations

import datetime
import fnmatch
import os
from pathlib import Path
from typing import Any

from kubernetes import client, config
from kubernetes.client import ApiClient
from kubernetes.dynamic import DynamicClient

import kinds
import settings

_dyn: DynamicClient | None = None
_core: client.CoreV1Api | None = None
_kubeconfig_mtime: float | None = None
_session_expiry: datetime.datetime | None = None


def _auth_key() -> str:
    """The long-lived minting credential, or empty when this mode is not configured."""
    p = Path(settings.AUTHKEY_FILE)
    if p.exists():
        return p.read_text().strip()
    return os.getenv("K8S_AUTHKEY", "").strip()


def _in_cluster() -> bool:
    """Whether a projected ServiceAccount token is present to authenticate with."""
    return Path("/var/run/secrets/kubernetes.io/serviceaccount/token").exists()


def _kubeconfig_path() -> str:
    """The kubeconfig this process would load, or "" when there is none."""
    explicit = os.getenv("KUBECONFIG", "").strip()
    if explicit:
        return explicit.split(":")[0]  # KUBECONFIG may list several; the first wins
    default = os.path.expanduser("~/.kube/config")
    return default if os.path.exists(default) else ""


def _bearer_config(token: str) -> client.Configuration:
    """An API client configuration authenticating with one bearer token."""
    if not settings.K8S_API_SERVER:
        raise RuntimeError(
            "K8S_API_SERVER is required when authenticating with an auth key - "
            "there is no in-cluster environment to discover it from"
        )
    cfg = client.Configuration()
    cfg.host = settings.K8S_API_SERVER
    # Set both keys: the client reads 'BearerToken' since v36 but falls back only
    # for the token, not its prefix.
    cfg.api_key = {"BearerToken": token, "authorization": token}
    cfg.api_key_prefix = {"BearerToken": "Bearer", "authorization": "Bearer"}
    if settings.K8S_CA_FILE:
        cfg.ssl_ca_cert = settings.K8S_CA_FILE
    else:
        cfg.verify_ssl = settings.K8S_VERIFY_TLS
    return cfg


def _mint_session() -> tuple[str, datetime.datetime]:
    """Exchange the auth key for a short-lived token for the reader account.

    Expiry is taken from the server's response, since a cluster may grant less
    than requested.
    """
    if not settings.SESSION_SA or not settings.SESSION_NAMESPACE:
        raise RuntimeError(
            "K8S_SESSION_SERVICE_ACCOUNT and K8S_SESSION_NAMESPACE are required "
            "when authenticating with an auth key - they name the account to mint for"
        )
    request = client.AuthenticationV1TokenRequest(
        spec=client.V1TokenRequestSpec(expiration_seconds=settings.SESSION_TTL)
    )
    with ApiClient(_bearer_config(_auth_key())) as api:
        resp = client.CoreV1Api(api).create_namespaced_service_account_token(
            name=settings.SESSION_SA, namespace=settings.SESSION_NAMESPACE, body=request
        )
    return resp.status.token, resp.status.expiration_timestamp


def _session_due() -> bool:
    """Whether to mint a new session token: at the halfway mark of the granted lifetime."""
    if _session_expiry is None:
        return True
    remaining = (_session_expiry - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    return remaining <= settings.SESSION_TTL / 2


def _clients() -> tuple[DynamicClient, client.CoreV1Api]:
    """The Kubernetes clients, rebuilt whenever the credential behind them changes:
    an auth-key session is renewed on a clock, a kubeconfig is watched for writes,
    in-cluster needs nothing.
    """
    global _dyn, _core, _kubeconfig_mtime, _session_expiry

    rebuild = _dyn is None or _core is None
    api: ApiClient | None = None

    if _in_cluster():
        if rebuild:
            config.load_incluster_config()
            api = ApiClient()
    elif _auth_key():
        if rebuild or _session_due():
            token, expiry = _mint_session()
            _session_expiry = expiry
            settings.log.info(
                "minted a session token for %s/%s, expires %s",
                settings.SESSION_NAMESPACE, settings.SESSION_SA, expiry.isoformat(),
            )
            api = ApiClient(_bearer_config(token))
            rebuild = True
    else:
        path = _kubeconfig_path()
        mtime: float | None = None
        if path:
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = None
        if rebuild or (path and mtime != _kubeconfig_mtime):
            # Pass the path explicitly: the client's default is frozen at import,
            # so a KUBECONFIG set later would have this watching one file and
            # loading another.
            config.load_kube_config(config_file=path or None)
            api = ApiClient()
            _kubeconfig_mtime = mtime
            rebuild = True

    if api is not None:
        _dyn = DynamicClient(api)
        _core = client.CoreV1Api(api)
    assert _dyn is not None and _core is not None
    return _dyn, _core


def _has_wildcard(pattern: str) -> bool:
    return any(c in pattern for c in "*?[")


def _accessible_namespaces() -> list[str]:
    """Every namespace this instance may look in: the allowlist on a scoped tier,
    else what the cluster lists.
    """
    if settings.ALLOWED_NAMESPACES is not None:
        return sorted(settings.ALLOWED_NAMESPACES)
    dyn, _ = _clients()
    res = dyn.resources.get(api_version="v1", kind="Namespace")
    return sorted(
        i.get("metadata", {}).get("name", "")
        for i in res.get().to_dict().get("items", [])
        if i.get("metadata", {}).get("name")
    )


def _resolve_namespaces(pattern: str) -> list[str] | str:
    """The namespaces a pattern selects, or an error string.

    Three forms: empty or `*` is everything accessible, a glob like `team*` is
    the accessible namespaces matching it, and anything else is a literal name
    checked against the allowlist.
    """
    if pattern in ("", "*"):
        return _accessible_namespaces()
    if not _has_wildcard(pattern):
        err = kinds._check_ns(pattern)
        return err if err else [pattern]
    try:
        candidates = _accessible_namespaces()
    except Exception as e:  # noqa: BLE001
        return f"Error listing namespaces: {e}"
    matched = [n for n in candidates if fnmatch.fnmatchcase(n, pattern)]
    if not matched:
        return (
            f"Error: no namespace matches '{pattern}'. "
            f"Visible: {', '.join(candidates) or '(none)'}"
        )
    return matched


def _find_in_namespaces(res: Any, name: str, namespaces: list[str]) -> list[tuple[str, dict]]:
    """Look for one named object across several namespaces.

    404s are swallowed (a miss is the ordinary case); anything else propagates.
    """
    found: list[tuple[str, dict]] = []
    for ns in namespaces:
        try:
            found.append((ns, res.get(name=name, namespace=ns).to_dict()))
        except Exception as e:  # noqa: BLE001
            if getattr(e, "status", None) == 404 or "not found" in str(e).lower():
                continue
            raise
    return found


def _ambiguous(kind: str, name: str, hits: list[tuple[str, dict]], pattern: str) -> str:
    """The answer when a name exists in more than one matching namespace: list
    the candidates rather than pick one.
    """
    lines = [f"{len(hits)} objects named '{name}' match namespace '{pattern}':"]
    lines += [f"  {ns}/{name}" for ns, _ in hits]
    lines.append("Pass one namespace to choose between them.")
    return "\n".join(lines)


def _not_found(kind: str, name: str, namespaces: list[str], pattern: str) -> str:
    if len(namespaces) == 1:
        return f"Error: no {kind} named '{name}' in namespace '{namespaces[0]}'."
    return (
        f"Error: no {kind} named '{name}' in the {len(namespaces)} namespaces "
        f"matching '{pattern}': {', '.join(namespaces)}"
    )
