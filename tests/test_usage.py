"""k8s_node_usage and k8s_pod_usage: metrics joined to capacity and limits."""

import server
from tests.conftest import pod


# -- Node usage -------------------------------------------------------


def _node(name, cpu="8", mem="32Gi", ready="True", pressure=None):
    conditions = [{"type": "Ready", "status": ready}]
    for p in pressure or []:
        conditions.append({"type": p, "status": "True"})
    return {
        "metadata": {"name": name},
        "status": {"allocatable": {"cpu": cpu, "memory": mem}, "conditions": conditions},
    }


def _node_metrics(name, cpu="2000000000n", mem="8Gi"):
    return {"metadata": {"name": name}, "usage": {"cpu": cpu, "memory": mem}}


def test_node_usage_joins_metrics_to_allocatable(fake_cluster):
    fake_cluster(Node=[_node("node-a")], NodeMetrics=[_node_metrics("node-a")])
    out = server._dispatch("k8s_node_usage", {})
    assert "node-a" in out
    assert "2.00/8 cores (25%)" in out      # 2 of 8 cores
    assert "8.0/32.0 GiB (25%)" in out      # 8 of 32 GiB
    assert "Ready" in out


def test_node_usage_flags_pressure(fake_cluster):
    fake_cluster(Node=[_node("node-b", pressure=["DiskPressure"])], NodeMetrics=[_node_metrics("node-b")])
    assert "DiskPressure" in server._dispatch("k8s_node_usage", {})


def test_node_usage_handles_a_node_with_no_metrics(fake_cluster):
    fake_cluster(Node=[_node("node-a"), _node("node-b")], NodeMetrics=[_node_metrics("node-a")])
    out = server._dispatch("k8s_node_usage", {})
    assert "(no metrics)" in out and "node-b" in out


def test_node_usage_explains_a_missing_metrics_api(fake_cluster):
    fake_cluster(Node=[_node("node-a")], missing={"NodeMetrics"})
    out = server._dispatch("k8s_node_usage", {})
    assert "metrics-server" in out and "k8s_get node" in out


def test_node_usage_single_node(fake_cluster):
    fake_cluster(Node=[_node("node-a"), _node("node-b")],
                 NodeMetrics=[_node_metrics("node-a"), _node_metrics("node-b")])
    out = server._dispatch("k8s_node_usage", {"node": "node-a"})
    assert "node-a" in out and "node-b" not in out


# -- Pod usage --------------------------------------------------------

LIMITS = {"cpu": "1", "memory": "512Mi"}


def _pod_metrics(name, namespace="team-a", cpu="100m", mem="256Mi"):
    return {
        "metadata": {"name": name, "namespace": namespace},
        "containers": [{"name": "app", "usage": {"cpu": cpu, "memory": mem}}],
    }


def _pod_usage():
    return server._dispatch("k8s_pod_usage", {"namespace": "team-a"})


def test_pod_usage_reports_usage_against_limits(fake_cluster):
    fake_cluster(Pod=[pod("api", limits=LIMITS)], PodMetrics=[_pod_metrics("api")])
    out = _pod_usage()
    assert "256/512Mi (50%)" in out       # half its memory limit
    assert "0.100/1.00 (10%)" in out      # a tenth of its cpu limit


def test_pod_usage_sorts_the_worst_first(fake_cluster):
    fake_cluster(Pod=[pod("calm", limits=LIMITS), pod("hot", limits=LIMITS)],
                 PodMetrics=[_pod_metrics("calm", mem="64Mi"), _pod_metrics("hot", mem="500Mi")])
    rows = [l for l in _pod_usage().splitlines() if l.startswith(("calm", "hot"))]
    assert rows[0].startswith("hot")


def test_pod_usage_shows_a_bare_figure_when_unlimited(fake_cluster):
    """No limit means no fraction - a pod cannot exceed a limit it does not have."""
    fake_cluster(Pod=[pod("free")], PodMetrics=[_pod_metrics("free")])
    out = _pod_usage()
    assert "256Mi" in out and "%" not in out


def test_pod_usage_carries_restart_counts(fake_cluster):
    fake_cluster(Pod=[pod("api", limits=LIMITS, restarts=70)], PodMetrics=[_pod_metrics("api")])
    assert "70" in _pod_usage()


def test_pod_usage_handles_a_pod_with_no_metrics(fake_cluster):
    fake_cluster(Pod=[pod("api", limits=LIMITS), pod("new", limits=LIMITS)], PodMetrics=[_pod_metrics("api")])
    out = _pod_usage()
    assert "(no metrics)" in out and "new" in out


def test_pod_usage_explains_a_missing_metrics_api(fake_cluster):
    fake_cluster(Pod=[pod("api", limits=LIMITS)], missing={"PodMetrics"})
    out = _pod_usage()
    assert "metrics-server" in out and "k8s_list" in out
