"""Pure formatting: summaries, noise stripping and quantity parsing."""

import formatting
from tests.conftest import pod


def test_pod_summary_surfaces_waiting_reason():
    p = {
        "metadata": {"name": "distiller-x", "creationTimestamp": "2026-07-03T00:00:00Z"},
        "status": {
            "phase": "Pending",
            "containerStatuses": [
                {"ready": False, "restartCount": 0,
                 "state": {"waiting": {"reason": "ImagePullBackOff"}}}
            ],
        },
    }
    line = formatting._summarize_pod(p)
    assert "distiller-x" in line and "ImagePullBackOff" in line and "0/1" in line


def test_pod_summary_includes_the_node():
    line = formatting._summarize_pod(pod("api-7f", node="node-a"), include_namespace=True)
    assert "node-a" in line
    assert line.split("\t")[0] == "team-a"


def test_pod_summary_handles_an_unscheduled_pod():
    """A Pending pod has no nodeName yet - that is a dash, not a crash."""
    line = formatting._summarize_pod(pod("pending-x", phase="Pending"))
    assert "-" in line and "pending-x" in line


def test_strip_noise_removes_managed_fields():
    obj = {"metadata": {"name": "x", "managedFields": [{"a": 1}],
                        "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "{}"}}}
    out = formatting._strip_noise(obj)
    assert "managedFields" not in out["metadata"]
    assert "kubectl.kubernetes.io/last-applied-configuration" not in out["metadata"]["annotations"]


def test_parse_cpu_units():
    assert formatting._parse_cpu("2000000000n") == 2.0
    assert formatting._parse_cpu("500m") == 0.5
    assert formatting._parse_cpu("8") == 8.0
    assert formatting._parse_cpu(None) == 0.0


def test_parse_mem_binary_suffixes_win():
    assert formatting._parse_mem("1Ki") == 1024        # not 1000
    assert formatting._parse_mem("32Gi") == 32 * 2**30
    assert formatting._parse_mem("1M") == 1e6
