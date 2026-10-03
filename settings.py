"""Deployment settings, read once at import.

Read values through the module (`settings.EXPOSE_CONFIGMAPS`), not by importing
the names; `kinds` rebuilds its map from these at import, and tests reload in
dependency order.
"""

from __future__ import annotations

import logging
import os
import sys


_allowlist_raw = os.getenv("K8S_ALLOWED_NAMESPACES", "").strip()
ALLOWED_NAMESPACES: frozenset[str] | None = (
    frozenset(ns.strip() for ns in _allowlist_raw.split(",") if ns.strip())
    if _allowlist_raw
    else None
)

EXPOSE_LOGS = os.getenv("EXPOSE_LOGS", "false").lower() in {"1", "true", "yes", "on"}
# Off by default: ConfigMaps routinely hold values that belong in a Secret.
EXPOSE_CONFIGMAPS = os.getenv("EXPOSE_CONFIGMAPS", "false").lower() in {"1", "true", "yes", "on"}

LOG_MAX_LINES = int(os.getenv("K8S_LOG_MAX_LINES", "500"))
LOG_MAX_BYTES = int(os.getenv("K8S_LOG_MAX_BYTES", "100000"))
LIST_MAX_ITEMS = int(os.getenv("K8S_LIST_MAX_ITEMS", "200"))
GET_MAX_BYTES = int(os.getenv("K8S_GET_MAX_BYTES", "40000"))

# -- Credentials -------------------------------------------------------
# Tried in order: in-cluster projected token; an auth key that mints
# short-lived session tokens for one read-only ServiceAccount; a kubeconfig
# file, watched for changes. See README "Credentials".
AUTHKEY_FILE = os.getenv("K8S_AUTHKEY_FILE", "/run/secrets/k8s-authkey")
K8S_API_SERVER = os.getenv("K8S_API_SERVER", "").strip().rstrip("/")
K8S_CA_FILE = os.getenv("K8S_CA_FILE", "").strip()
K8S_VERIFY_TLS = os.getenv("K8S_VERIFY_TLS", "true").lower() not in {"0", "false", "no", "off"}
SESSION_SA = os.getenv("K8S_SESSION_SERVICE_ACCOUNT", "").strip()
SESSION_NAMESPACE = os.getenv("K8S_SESSION_NAMESPACE", "").strip()
SESSION_TTL = int(os.getenv("K8S_SESSION_TTL_SECONDS", "21600"))  # 6h

logging.basicConfig(stream=sys.stderr, level=os.getenv("LOG_LEVEL", "INFO"), format="%(message)s")
log = logging.getLogger("k8s-mcp")
