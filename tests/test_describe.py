"""k8s_describe: why a pod is failing, with its events."""

import server
from tests.conftest import event, pod


def _crashed_pod():
    return {
        "metadata": {"name": "api-7f", "namespace": "team-a", "labels": {"app": "api"}},
        "spec": {
            "nodeName": "node-a",
            "restartPolicy": "Always",
            "containers": [{
                "name": "api",
                "image": "registry.example.com/api:1.2.3",
                "resources": {"limits": {"memory": "512Mi"}},
            }],
            "volumes": [{"name": "cfg", "configMap": {"name": "app-config"}}],
        },
        "status": {
            "phase": "Running",
            "podIP": "10.0.0.7",
            "conditions": [{"type": "Ready", "status": "False", "reason": "ContainersNotReady"}],
            "containerStatuses": [{
                "name": "api",
                "ready": False,
                "restartCount": 12,
                "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}},
            }],
        },
    }


def _describe(name="api-7f", kind="pods"):
    return server._dispatch("k8s_describe", {"kind": kind, "name": name, "namespace": "team-a"})


def test_describe_surfaces_why_the_last_container_died(fake_cluster):
    fake_cluster(Pod=[_crashed_pod()])
    out = _describe()
    assert "OOMKilled" in out and "exitCode=137" in out   # what killed the previous one
    assert "CrashLoopBackOff" in out                       # what it is doing now
    assert "Restarts: 12" in out
    assert "node-a" in out
    assert "ContainersNotReady" in out
    assert "memory=512Mi" in out


def test_describe_marks_an_unscheduled_pod(fake_cluster):
    fake_cluster(Pod=[pod("pending", phase="Pending")])
    assert "(unscheduled)" in _describe("pending")


def test_describe_appends_events(fake_cluster):
    fake_cluster(Pod=[_crashed_pod()], Event=[event("team-a", reason="BackOff")])
    out = _describe()
    assert "Events:" in out and "BackOff" in out


def test_describe_says_events_age_out_when_there_are_none(fake_cluster):
    """An empty list is not evidence of a healthy history, and should not read as one."""
    fake_cluster(Pod=[_crashed_pod()])
    assert "age out" in _describe()


def test_describe_handles_non_pod_kinds(fake_cluster):
    dep = {"metadata": {"name": "api", "namespace": "team-a"},
           "status": {"replicas": 3, "readyReplicas": 1,
                      "conditions": [{"type": "Available", "status": "False", "reason": "MinimumReplicasUnavailable"}]}}
    fake_cluster(Deployment=[dep])
    out = _describe("api", kind="deployments")
    assert "readyReplicas: 1" in out and "MinimumReplicasUnavailable" in out


def test_describe_requires_all_three_arguments():
    out = server._dispatch("k8s_describe", {"kind": "pods", "name": "x"})
    assert "required" in out
