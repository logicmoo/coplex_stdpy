"""Smoke tests that verify the packaged ``coplex_stdpy`` distribution works.

These intentionally avoid depending on a real model provider or on
``plugin_admin`` (only available inside a compatible plugin host) so they run
the same way from a plain ``pip install`` as from a source checkout. They
also avoid pytest's ``tmp_path`` fixture, which touches a shared, numbered
base temp directory that is not always writable in restricted/shared
environments; a private ``tempfile.TemporaryDirectory()`` sidesteps that.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


def test_public_api_exports() -> None:
    import coplex_stdpy

    assert coplex_stdpy.LLMTaskHarness is coplex_stdpy.CodexHarness
    assert callable(coplex_stdpy.OpenAICompatibleAdapter.validate_base_url)
    assert coplex_stdpy.HarnessTaskManager.PERMISSION_RANK == {
        "read-only": 0,
        "workspace-write": 1,
        "full-access": 2,
    }
    assert coplex_stdpy.ToolDefinition is not None


def test_harness_constructs_and_closes() -> None:
    from coplex_stdpy import LLMTaskHarness

    def adapter(request: dict) -> dict:
        return {"content": "ok", "tool_calls": []}

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        with LLMTaskHarness(adapter, root=root, permission_profile="read-only") as agent:
            assert agent.root == root
            assert agent.closed is False
        assert agent.closed is True


def test_plugin_module_imports_without_plugin_admin() -> None:
    """``coplex_stdpy.plugin`` must import even outside a plugin host."""

    from coplex_stdpy import plugin

    assert plugin.PLUGIN_ID == "coplex_stdpy"
    if not plugin._HAVE_PLUGIN_ADMIN:
        with pytest.raises(RuntimeError):
            plugin.initialization_report({})


def test_create_router_works_standalone() -> None:
    from coplex_stdpy import plugin

    router = plugin.create_router({"executionEnabled": False})
    paths = {route.path for route in router.routes}
    assert "/coplex_stdpy" in paths
    assert "/coplex_stdpy/health" in paths
    assert "/coplex_stdpy/tasks" in paths


def test_repository_root_defaults_to_cwd(monkeypatch) -> None:
    from coplex_stdpy import plugin

    monkeypatch.delenv("COPLEX_STDPY_REPOSITORY_ROOT", raising=False)
    original_cwd = Path.cwd()
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp).resolve()
        try:
            monkeypatch.chdir(cwd)
            assert plugin._repository_root({}) == cwd
        finally:
            # Restore cwd before the temp dir cleanup below runs: Windows
            # cannot remove a directory that is still a process's cwd.
            monkeypatch.chdir(original_cwd)


def test_repository_root_honors_explicit_override(monkeypatch) -> None:
    from coplex_stdpy import plugin

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp).resolve()
        monkeypatch.setenv("COPLEX_STDPY_REPOSITORY_ROOT", str(target))
        assert plugin._repository_root({}) == target
