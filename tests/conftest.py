"""Shared test fixtures.

Settings are read at import and the resource map is rebuilt from them, so a
test that changes the environment reloads through `reload_server`, which
reloads every module in dependency order.

Every test is hermetic: `fake_cluster` stands in for the dynamic client, and
`no_cluster` fails the test if anything tries to connect.
"""

import importlib
import os
import sys

import pytest

# Make the repo-root server.py importable as `server`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cluster  # noqa: E402
import settings  # noqa: E402

# Reload order: each module after the ones it reads from; `server` last.
_RELOAD_ORDER = ("settings", "kinds", "cluster", "formatting", "handlers", "server")


def _reload_server():
    for name in _RELOAD_ORDER:
        mod = sys.modules.get(name)
        if mod is None:
            importlib.import_module(name)
        else:
            importlib.reload(mod)
    return sys.modules["server"]


@pytest.fixture
def reload_server():
    """Re-import the server so module-level config re-reads the environment.

    Returns the callable, not the module: the environment has to be set up
    inside the test before the reload happens.
    """
    return _reload_server


# -- Fake dynamic client ----------------------------------------------


class FakeCluster:
    """Serves canned objects by kind, and records every read in `calls`.

    Reads filter by namespace and name; label and field selectors are recorded,
    not applied. A kind in `missing` is not served at all, as when
    metrics-server is not installed. `fail` is raised on every read except the
    namespace list.
    """

    def __init__(self, objects, namespaces=(), missing=(), fail=None):
        self._objects = {kind: list(items) for kind, items in objects.items()}
        if namespaces:
            self._objects["Namespace"] = [{"metadata": {"name": n}} for n in namespaces]
        self._missing = set(missing)
        self._fail = fail
        self.calls: list[dict] = []
        self.resources = self

    @property
    def asked(self) -> list[str | None]:
        """The namespaces a lookup by name was tried in, in order."""
        return [c.get("namespace") for c in self.calls if "name" in c]

    def get(self, api_version=None, kind=None):
        if kind in self._missing:
            raise RuntimeError(f"the server could not find the requested resource ({kind})")
        return _FakeKind(self, kind)


class _FakeKind:
    def __init__(self, fake, kind):
        self._fake, self._kind = fake, kind

    def get(self, **kwargs):
        self._fake.calls.append(kwargs)
        if self._fake._fail and self._kind != "Namespace":
            raise self._fake._fail
        ns, name = kwargs.get("namespace"), kwargs.get("name")
        items = [i for i in self._fake._objects.get(self._kind, [])
                 if ns is None or i["metadata"].get("namespace") == ns]
        if name is None:
            return _FakeResult({"items": items})
        for i in items:
            if i["metadata"]["name"] == name:
                return _FakeResult(i)
        raise RuntimeError(f'{self._kind} "{name}" not found')


class _FakeResult:
    def __init__(self, body):
        self._body = body

    def to_dict(self):
        return self._body


@pytest.fixture
def fake_cluster(monkeypatch):
    """Install a FakeCluster: `fake_cluster(Pod=[...], allow=["team-a"])`.

    `allow` sets the namespace allowlist; None, the default, is unscoped.
    """
    def install(allow=None, namespaces=(), missing=(), fail=None, **objects):
        monkeypatch.setattr(settings, "ALLOWED_NAMESPACES", frozenset(allow) if allow is not None else None)
        fake = FakeCluster(objects, namespaces, missing, fail)
        monkeypatch.setattr(cluster, "_clients", lambda: (fake, None))
        return fake
    return install


@pytest.fixture
def no_cluster(monkeypatch):
    """Fail the test on any connection; for refusals that must come first."""
    def install(allow=None):
        monkeypatch.setattr(settings, "ALLOWED_NAMESPACES", frozenset(allow) if allow is not None else None)
        monkeypatch.setattr(
            cluster, "_clients", lambda: (_ for _ in ()).throw(AssertionError("must not connect"))
        )
    return install


# -- Object builders --------------------------------------------------


def pod(name, namespace="team-a", phase="Running", node=None, limits=None, restarts=0):
    """A pod with one container; `limits` is that container's resource limits."""
    spec = {"containers": [{"name": "app", "resources": {"limits": limits} if limits else {}}]}
    if node:
        spec["nodeName"] = node
    return {
        "metadata": {"name": name, "namespace": namespace, "creationTimestamp": "2026-07-03T00:00:00Z"},
        "spec": spec,
        "status": {"phase": phase,
                   "containerStatuses": [{"ready": True, "restartCount": restarts, "state": {}}]},
    }


def event(namespace, reason="Scheduled"):
    return {
        "metadata": {"namespace": namespace},
        "type": "Normal",
        "reason": reason,
        "involvedObject": {"kind": "Pod", "name": "x"},
        "count": 1,
        "lastTimestamp": "2026-07-03T00:00:00Z",
        "message": "m",
    }
