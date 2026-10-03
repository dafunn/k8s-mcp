"""k8s_get: selecting a subtree by path."""

import pytest

import server
import settings


@pytest.fixture(autouse=True)
def one_pod(fake_cluster):
    fake_cluster(Pod=[{
        "metadata": {"name": "api", "namespace": "team-a"},
        "spec": {"containers": [{"name": "api", "image": "registry.example.com/api:1.2.3",
                                 "resources": {"limits": {"memory": "512Mi"}}}]},
        "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
    }])


def _get(**extra):
    return server._dispatch("k8s_get", {"kind": "pods", "name": "api", "namespace": "team-a", **extra})


def test_path_selects_a_subtree():
    out = _get(path="status.conditions")
    assert "Ready" in out and "containers" not in out


def test_path_indexes_into_lists():
    assert _get(path="spec.containers.0.image").strip() == "spec.containers.0.image: registry.example.com/api:1.2.3"


def test_a_wrong_path_answers_with_the_keys_that_exist():
    out = _get(path="spec.contaners")
    assert "no 'contaners' at spec" in out and "containers" in out


def test_a_bad_index_says_how_long_the_list_is():
    assert "list of 1" in _get(path="spec.containers.7")


def test_path_into_a_scalar_is_refused():
    assert "is a value, not something" in _get(path="status.phase.nope")


def test_no_path_returns_the_whole_object():
    out = _get()
    assert "containers" in out and "conditions" in out


def test_oversized_output_points_at_path(monkeypatch):
    monkeypatch.setattr(settings, "GET_MAX_BYTES", 40)
    out = _get()
    assert "truncated" in out and "path" in out and "k8s_describe" in out
