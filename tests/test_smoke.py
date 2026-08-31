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


def test_server_module_imports_without_plugin_admin() -> None:
    """``coplex_stdpy.server`` must import even outside a plugin host."""

    from coplex_stdpy import server

    assert server.PLUGIN_ID == "coplex_stdpy"
    if not server._HAVE_PLUGIN_ADMIN:
        with pytest.raises(RuntimeError):
            server.initialization_report({})


def test_create_router_works_standalone() -> None:
    from coplex_stdpy import server

    router = server.create_router({"executionEnabled": False})
    paths = {route.path for route in router.routes}
    assert "/coplex_stdpy" in paths
    assert "/coplex_stdpy/endpoints" in paths
    assert "/coplex_stdpy/health" in paths
    assert "/coplex_stdpy/tasks" in paths
    assert "/coplex_stdpy/admin/shutdown" in paths
    assert "/coplex_stdpy/admin/restart" in paths


def test_bare_root_serves_console_and_endpoints_serves_summary() -> None:
    """The bare mount root (``/coplex_stdpy``) must serve the HTML task
    console, and the JSON runtime summary/links payload must have moved to
    ``/coplex_stdpy/endpoints`` -- not the other way around.
    """

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from coplex_stdpy import server

    app = FastAPI()
    app.include_router(server.create_router({"executionEnabled": False}))
    with TestClient(app) as client:
        console = client.get("/coplex_stdpy")
        assert console.status_code == 200
        assert "text/html" in console.headers["content-type"]
        assert "<html" in console.text.lower()

        endpoints = client.get("/coplex_stdpy/endpoints")
        assert endpoints.status_code == 200
        payload = endpoints.json()
        assert payload["id"] == "coplex_stdpy"
        assert payload["links"]["endpoints"] == "/coplex_stdpy/endpoints"
        assert payload["links"]["ui"] == "/coplex_stdpy"

        # The old /ui route is gone now that it moved to the bare root.
        assert client.get("/coplex_stdpy/ui").status_code == 404


def test_admin_process_control_501s_when_unregistered() -> None:
    """Without a registered hook, /admin/shutdown and /admin/restart must
    never silently succeed -- and must never actually touch the process
    running the test suite.
    """

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from coplex_stdpy import server

    server._process_control["shutdown"] = None
    server._process_control["restart"] = None

    app = FastAPI()
    app.include_router(server.create_router({"executionEnabled": False}))
    with TestClient(app) as client:
        shutdown = client.post("/coplex_stdpy/admin/shutdown")
        assert shutdown.status_code == 501
        restart = client.post("/coplex_stdpy/admin/restart")
        assert restart.status_code == 501

        summary = client.get("/coplex_stdpy/endpoints").json()
        assert summary["processControlAvailable"] == {"shutdown": False, "restart": False}


def test_admin_process_control_invokes_registered_hooks() -> None:
    """register_process_control() wires real hooks in; each route calls
    exactly its own hook and reports success.
    """

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from coplex_stdpy import server

    calls: list[str] = []
    try:
        server.register_process_control(
            shutdown=lambda: calls.append("shutdown"),
            restart=lambda: calls.append("restart"),
        )

        app = FastAPI()
        app.include_router(server.create_router({"executionEnabled": False}))
        with TestClient(app) as client:
            summary = client.get("/coplex_stdpy/endpoints").json()
            assert summary["processControlAvailable"] == {"shutdown": True, "restart": True}

            shutdown = client.post("/coplex_stdpy/admin/shutdown")
            assert shutdown.status_code == 200
            assert shutdown.json() == {"ok": True, "action": "shutdown"}

            restart = client.post("/coplex_stdpy/admin/restart")
            assert restart.status_code == 200
            assert restart.json() == {"ok": True, "action": "restart"}

        assert calls == ["shutdown", "restart"]
    finally:
        # This is process-wide module state: always reset it so no other
        # test observes a hook left over from this one.
        server._process_control["shutdown"] = None
        server._process_control["restart"] = None


def test_repository_root_defaults_to_cwd(monkeypatch) -> None:
    from coplex_stdpy import server

    monkeypatch.delenv("COPLEX_STDPY_REPOSITORY_ROOT", raising=False)
    original_cwd = Path.cwd()
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp).resolve()
        try:
            monkeypatch.chdir(cwd)
            assert server._repository_root({}) == cwd
        finally:
            # Restore cwd before the temp dir cleanup below runs: Windows
            # cannot remove a directory that is still a process's cwd.
            monkeypatch.chdir(original_cwd)


def test_repository_root_honors_explicit_override(monkeypatch) -> None:
    from coplex_stdpy import server

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp).resolve()
        monkeypatch.setenv("COPLEX_STDPY_REPOSITORY_ROOT", str(target))
        assert server._repository_root({}) == target


def test_root_plugin_shim_loads_by_file_path_like_the_workbench_does() -> None:
    """The Workbench loads ``plugin.py`` directly by file path (per
    ``plugin.json``'s ``entrypoint``), not as a package import. Reproduce
    that exact mechanism here instead of a plain ``import plugin``.
    """

    import importlib.util

    repo_root = Path(__file__).resolve().parents[1]
    plugin_path = repo_root / "plugin.py"
    spec = importlib.util.spec_from_file_location("coplex_stdpy_workbench_entry", plugin_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.PLUGIN_ID == "coplex_stdpy"
    router = module.create_router({"executionEnabled": False})
    paths = {route.path for route in router.routes}
    assert "/coplex_stdpy/health" in paths
