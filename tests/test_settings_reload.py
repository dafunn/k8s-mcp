"""The reload contract the module split depends on.

Reloading `server` without reloading `settings` first would rebuild the
resource map from the previous environment, and the assertion would pass while
checking the wrong thing. Nothing else in the suite would catch that: the
ConfigMap gate tests reload through the same helper, so a broken order would
move them together.
"""

import kinds
import server
import settings


def test_reload_rebuilds_the_resource_map_from_the_new_environment(monkeypatch, reload_server):
    monkeypatch.setenv("EXPOSE_CONFIGMAPS", "true")
    reload_server()
    assert settings.EXPOSE_CONFIGMAPS is True
    assert kinds._resolve_kind("configmap") == ("v1", "ConfigMap")

    monkeypatch.setenv("EXPOSE_CONFIGMAPS", "false")
    reload_server()
    assert settings.EXPOSE_CONFIGMAPS is False
    refusal = kinds._resolve_kind("configmap")
    assert isinstance(refusal, str) and "EXPOSE_CONFIGMAPS" in refusal


def test_settings_have_exactly_one_binding(monkeypatch):
    """Every reader goes through the module, so patching `settings` alone moves
    the answer with no reload at all. A per-module copy is what lets a test
    patch one binding while the code consults another."""
    for name in ("ALLOWED_NAMESPACES", "EXPOSE_CONFIGMAPS", "LIST_MAX_ITEMS"):
        assert name not in vars(server), f"server holds its own {name}"

    monkeypatch.setattr(settings, "ALLOWED_NAMESPACES", frozenset({"team-a"}))
    assert kinds._check_ns("team-a") is None
    assert "not in the allowed set" in (kinds._check_ns("team-b") or "")
