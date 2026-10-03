"""k8s_list: the node filter, the NODE column and empty-result messages."""

import server
from tests.conftest import pod


def test_pod_list_header_matches_the_row_width(fake_cluster):
    fake_cluster(Pod=[pod("api-7f", node="node-a")])
    out = server._dispatch("k8s_list", {"kind": "pods", "namespace": "*"})
    header, row = [l for l in out.splitlines() if "\t" in l][:2]
    assert "NODE" in header
    assert len(header.split("\t")) == len(row.split("\t"))


# -- Node filter ------------------------------------------------------
#
# The filter must reach the API server as a field selector, not run client-side.


def test_node_filter_is_sent_as_a_field_selector(fake_cluster):
    fake = fake_cluster()
    server._dispatch("k8s_list", {"kind": "pods", "namespace": "team-a", "node": "node-a"})
    assert fake.calls[0].get("field_selector") == "spec.nodeName=node-a"


def test_node_filter_works_across_all_namespaces(fake_cluster):
    fake = fake_cluster()
    server._dispatch("k8s_list", {"kind": "pods", "namespace": "*", "node": "node-a"})
    assert fake.calls[0].get("field_selector") == "spec.nodeName=node-a"
    assert "namespace" not in fake.calls[0]


def test_node_filter_applies_per_namespace_on_a_scoped_tier(fake_cluster):
    fake = fake_cluster(allow=["team-a", "team-b"])
    server._dispatch("k8s_list", {"kind": "pods", "namespace": "*", "node": "node-a"})
    assert len(fake.calls) == 2
    assert all(c.get("field_selector") == "spec.nodeName=node-a" for c in fake.calls)
    assert all("namespace" in c for c in fake.calls)  # never a namespace-less call


def test_node_filter_combines_with_a_label_selector(fake_cluster):
    fake = fake_cluster()
    server._dispatch("k8s_list", {"kind": "pods", "namespace": "team-a",
                                  "node": "node-a", "label_selector": "app=api"})
    assert fake.calls[0].get("label_selector") == "app=api"
    assert fake.calls[0].get("field_selector") == "spec.nodeName=node-a"


def test_node_filter_refused_for_kinds_that_have_no_node(no_cluster):
    no_cluster()
    out = server._dispatch("k8s_list", {"kind": "deployments", "namespace": "team-a", "node": "node-a"})
    assert "only applies to pods" in out


def test_no_node_means_no_field_selector(fake_cluster):
    fake = fake_cluster()
    server._dispatch("k8s_list", {"kind": "pods", "namespace": "team-a"})
    assert "field_selector" not in fake.calls[0]


# -- Empty results name their filters ---------------------------------


def test_empty_list_names_the_label_selector(fake_cluster):
    fake_cluster()
    out = server._dispatch("k8s_list", {"kind": "pods", "namespace": "team-a",
                                        "label_selector": "app=nope"})
    assert "app=nope" in out and "the filter that matched nothing" in out


def test_empty_list_without_filters_stays_terse(fake_cluster):
    fake_cluster()
    out = server._dispatch("k8s_list", {"kind": "pods", "namespace": "team-a"})
    assert out == "No Pod found in namespace 'team-a'."


def test_empty_list_names_the_node_filter(fake_cluster):
    fake_cluster()
    assert "node-a" in server._dispatch("k8s_list", {"kind": "pods", "namespace": "team-a", "node": "node-a"})
    assert "node-z" in server._dispatch("k8s_list", {"kind": "pods", "namespace": "*", "node": "node-z"})
