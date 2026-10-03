"""The namespace boundary: the allowlist, "all namespaces", and patterns."""

import pytest

import kinds
import server
import settings
from tests.conftest import event, pod


# -- Allowlist --------------------------------------------------------


def test_check_ns_allows_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "ALLOWED_NAMESPACES", None)
    assert kinds._check_ns("anything") is None


def test_check_ns_blocks_offlist(monkeypatch):
    monkeypatch.setattr(settings, "ALLOWED_NAMESPACES", frozenset({"team-a", "team-b"}))
    assert kinds._check_ns("team-b") is None
    err = kinds._check_ns("team-c")
    assert err and "not in the allowed set" in err


@pytest.mark.parametrize("tool, args", [
    ("k8s_list", {"kind": "pods", "namespace": "team-c"}),
    ("k8s_events", {"namespace": "team-c"}),
    ("k8s_pod_usage", {"namespace": "team-c"}),
    ("k8s_describe", {"kind": "pods", "name": "x", "namespace": "team-c"}),
    ("k8s_get", {"kind": "deployments", "name": "x", "namespace": "team-c"}),
])
def test_offlist_namespace_refused_before_cluster(no_cluster, tool, args):
    no_cluster(allow=["team-a"])
    assert "not in the allowed set" in server._dispatch(tool, args)


# -- All namespaces ---------------------------------------------------
#
# k8s_list and k8s_events follow the same pattern: unscoped is one cluster-wide
# call, scoped loops the allowlist and never makes one.

ALL_NS = [
    pytest.param("k8s_list", {"kind": "pods"}, "Pod", lambda ns: pod("a", ns), id="list"),
    pytest.param("k8s_events", {}, "Event", event, id="events"),
]


@pytest.mark.parametrize("tool, args, kind, make", ALL_NS)
def test_all_namespaces_unscoped_makes_one_cluster_wide_call(fake_cluster, tool, args, kind, make):
    fake = fake_cluster(**{kind: [make("team-a"), make("team-b")]})
    out = server._dispatch(tool, {**args, "namespace": "*"})
    assert fake.calls == [{}]
    assert "NAMESPACE" in out
    assert "team-a" in out and "team-b" in out


@pytest.mark.parametrize("tool, args, kind, make", ALL_NS)
def test_namespace_omitted_also_means_all_namespaces(fake_cluster, tool, args, kind, make):
    fake = fake_cluster(**{kind: [make("team-a")]})
    out = server._dispatch(tool, args)
    assert fake.calls == [{}]
    assert "team-a" in out


@pytest.mark.parametrize("tool, args, kind, make", ALL_NS)
def test_all_namespaces_scoped_loops_allowlist_never_cluster_wide(fake_cluster, tool, args, kind, make):
    # The boundary test: a scoped tier must union the allowlist, never issue a
    # cluster-wide call, even when asked to list "all namespaces".
    fake = fake_cluster(allow=["team-a", "team-b"], **{kind: [make("team-a"), make("team-b")]})
    out = server._dispatch(tool, {**args, "namespace": "*"})
    assert fake.calls == [{"namespace": "team-a"}, {"namespace": "team-b"}]
    assert "team-a" in out and "team-b" in out


@pytest.mark.parametrize("tool, args, kind, make", ALL_NS)
def test_specific_namespace_output_unchanged(fake_cluster, tool, args, kind, make):
    fake = fake_cluster(**{kind: [make("team-a")]})
    out = server._dispatch(tool, {**args, "namespace": "team-a"})
    assert fake.calls == [{"namespace": "team-a"}]
    assert "NAMESPACE" not in out


def test_all_namespaces_scoped_passes_label_selector_per_call(fake_cluster):
    fake = fake_cluster(allow=["team-a", "team-b"])
    server._dispatch("k8s_list", {"kind": "pods", "namespace": "*", "label_selector": "app=foo"})
    assert fake.calls == [
        {"namespace": "team-a", "label_selector": "app=foo"},
        {"namespace": "team-b", "label_selector": "app=foo"},
    ]


def test_cluster_scoped_kind_blocked_on_scoped_tier(no_cluster):
    no_cluster(allow=["team-a"])
    assert "cluster-scoped" in server._dispatch("k8s_list", {"kind": "nodes", "namespace": "*"})
    assert "cluster-scoped" in server._dispatch("k8s_list", {"kind": "namespaces"})
    assert "cluster-scoped" in server._dispatch("k8s_node_usage", {})


def test_cluster_scoped_kind_allowed_unscoped(fake_cluster):
    node = {"metadata": {"name": "node-1", "creationTimestamp": "2026-07-03T00:00:00Z"}, "status": {}}
    fake = fake_cluster(Node=[node])
    out = server._dispatch("k8s_list", {"kind": "nodes", "namespace": "*"})
    assert fake.calls == [{}]
    assert "node-1" in out


# -- Namespace patterns -----------------------------------------------
#
# On a scoped tier the pattern filters the allowlist.


def _deployments(name, *namespaces):
    return [{"metadata": {"name": name, "namespace": ns}, "spec": {"containers": []}, "status": {}}
            for ns in namespaces]


def test_star_searches_every_accessible_namespace(fake_cluster):
    fake_cluster(Deployment=_deployments("api", "team-alpha"),
                 namespaces=["default", "team-alpha", "kube-system"])
    out = server._dispatch("k8s_get", {"kind": "deployments", "name": "api",
                                       "namespace": "*", "path": "metadata.namespace"})
    assert out.strip().endswith("team-alpha")


def test_partial_wildcard_narrows_the_search(fake_cluster):
    fake = fake_cluster(Deployment=_deployments("api", "team-alpha"),
                        namespaces=["default", "team-alpha", "team-alto"])
    server._dispatch("k8s_get", {"kind": "deployments", "name": "api", "namespace": "team-al*"})
    assert fake.asked == ["team-alpha", "team-alto"]   # default was never queried


def test_a_name_in_two_namespaces_is_reported_not_guessed(fake_cluster):
    fake_cluster(Deployment=_deployments("api", "team-alpha", "team-alto"),
                 namespaces=["team-alpha", "team-alto"])
    out = server._dispatch("k8s_get", {"kind": "deployments", "name": "api", "namespace": "team-al*"})
    assert "2 objects named 'api'" in out
    assert "team-alpha/api" in out and "team-alto/api" in out
    assert "Pass one namespace" in out


def test_describe_reports_ambiguity_too(fake_cluster):
    fake_cluster(Deployment=_deployments("x", "a", "b"), namespaces=["a", "b"])
    out = server._dispatch("k8s_describe", {"kind": "deployments", "name": "x", "namespace": "*"})
    assert "2 objects named 'x'" in out


def test_not_found_names_the_namespaces_searched(fake_cluster):
    fake_cluster(namespaces=["team-alpha", "team-alto"])
    out = server._dispatch("k8s_get", {"kind": "deployments", "name": "nope", "namespace": "team-al*"})
    assert "2 namespaces matching 'team-al*'" in out and "team-alpha, team-alto" in out


def test_a_pattern_matching_nothing_lists_what_is_visible(fake_cluster):
    fake_cluster(namespaces=["team-alpha", "default"])
    out = server._dispatch("k8s_get", {"kind": "deployments", "name": "x", "namespace": "zzz*"})
    assert "no namespace matches 'zzz*'" in out and "team-alpha" in out


def test_a_pattern_cannot_escape_the_allowlist(fake_cluster):
    """The pattern filters the allowed set; it never reaches for a live list."""
    fake = fake_cluster(allow=["team-a", "team-b"], Deployment=_deployments("x", "team-a"),
                        namespaces=["team-a", "team-b", "kube-system"])
    server._dispatch("k8s_get", {"kind": "deployments", "name": "x", "namespace": "*"})
    assert fake.asked == ["team-a", "team-b"]   # kube-system is visible to the fake, not to us


def test_a_pattern_matching_only_offlist_namespaces_finds_nothing(no_cluster):
    no_cluster(allow=["team-a"])
    out = server._dispatch("k8s_get", {"kind": "deployments", "name": "x", "namespace": "kube-*"})
    assert "no namespace matches 'kube-*'" in out


def test_a_permission_error_is_not_reported_as_not_found(fake_cluster):
    """Swallowing anything but a 404 would turn a refusal into 'it does not exist'."""
    fake_cluster(namespaces=["a", "b"], fail=RuntimeError("(403) Reason: Forbidden"))
    out = server._dispatch("k8s_get", {"kind": "deployments", "name": "x", "namespace": "*"})
    assert "Forbidden" in out and "no Deployment named" not in out
