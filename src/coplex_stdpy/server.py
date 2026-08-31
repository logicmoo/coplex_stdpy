"""FastAPI adapter exposing the :mod:`coplex_stdpy` runtime as an HTTP service.

The task API below (``create_router``) is fully standalone and only needs
``coplex_stdpy`` and ``fastapi``. ``initialize`` and ``create_admin_router``
additionally publish a native settings page through ``plugin_admin``, a
helper module provided by a compatible plugin host (for example the
LogicMOO Workbench). That import is optional: this module can always be
imported, and the core task API always works, even when no such host is
present.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Body, HTTPException, Query, status as http_status
from fastapi.responses import HTMLResponse

from .runtime import HarnessTaskManager, OpenAICompatibleAdapter

try:
    from plugin_admin import (
        action,
        build_admin_router,
        descriptor,
        field,
        initialization_report,
        section,
        status,
        string_list,
        write_manifest_values,
    )

    _HAVE_PLUGIN_ADMIN = True
except ImportError:  # pragma: no cover - exercised outside a plugin host
    _HAVE_PLUGIN_ADMIN = False

    def _require_plugin_admin(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(
            "This feature needs the 'plugin_admin' module from a compatible "
            "plugin host (for example the LogicMOO Workbench). It is not "
            "required for create_router(), which works standalone."
        )

    action = build_admin_router = descriptor = field = _require_plugin_admin
    initialization_report = section = status = string_list = _require_plugin_admin
    write_manifest_values = _require_plugin_admin


_HERE = Path(__file__).resolve().parent
_CONSOLE_PAGE = _HERE / "static" / "console.html"

PLUGIN_ID = "coplex_stdpy"
_manager: HarnessTaskManager | None = None
_manifest: dict[str, Any] = {}

# Real OS-process shutdown/restart is only meaningful for whichever process
# actually owns the running ASGI server (uvicorn.Server). create_router()
# has no such handle by itself -- it may be embedded inside a much larger
# host application (for example the LogicMOO Workbench) that must never be
# taken down by a request to *this* plugin's routes. coplex_stdpy.standalone
# registers real hooks here after it constructs its own uvicorn.Server; an
# embedding host may register its own via register_process_control() if it
# wants /admin/shutdown and /admin/restart to control its process too.
# Left unregistered (the default), both routes answer 501.
_process_control: dict[str, Callable[[], Any] | None] = {"shutdown": None, "restart": None}


def register_process_control(
    *,
    shutdown: Callable[[], Any] | None = None,
    restart: Callable[[], Any] | None = None,
) -> None:
    """Register real process-lifecycle hooks for ``/admin/shutdown`` and
    ``/admin/restart``.

    Only call this from code that actually owns the OS process serving this
    router (see :mod:`coplex_stdpy.standalone`). Each hook is called with no
    arguments from within the request handler and should return quickly;
    do the actual stop/respawn work asynchronously (a background thread,
    for example) so the HTTP response can still be sent.
    """

    if shutdown is not None:
        _process_control["shutdown"] = shutdown
    if restart is not None:
        _process_control["restart"] = restart


def _repository_root(manifest: Mapping[str, Any]) -> Path:
    """Resolve the repository that submitted tasks operate on.

    Resolution order: an explicit ``repositoryRoot`` in the manifest, the
    ``COPLEX_STDPY_REPOSITORY_ROOT`` environment variable, the legacy
    Workbench convention of a manifest ``path`` pointing at
    ``<repo>/workbench/plugins/coplex_stdpy`` (kept for backward
    compatibility), and finally the current working directory -- the sane
    default once this package is installed from PyPI rather than embedded
    in a monorepo.
    """

    explicit = str(
        manifest.get("repositoryRoot")
        or os.environ.get("COPLEX_STDPY_REPOSITORY_ROOT")
        or ""
    ).strip()
    if explicit:
        return Path(explicit).resolve()
    configured = str(manifest.get("path") or "").strip()
    if configured:
        plugin_path = Path(configured).resolve()
        if len(plugin_path.parents) > 2:
            legacy_root = plugin_path.parents[2]
            if (legacy_root / "workbench" / "plugins").is_dir():
                return legacy_root
    return Path.cwd().resolve()


def _state_directory(manifest: Mapping[str, Any]) -> Path:
    """Resolve the durable task store, allowing isolated test/deployment state."""

    repository_root = _repository_root(manifest)
    configured = os.environ.get("COPLEX_STDPY_STATE_DIRECTORY", "").strip()
    raw = Path(configured or "runtime/coplex_stdpy")
    path = (raw if raw.is_absolute() else repository_root / raw).resolve(strict=False)
    if path != repository_root and repository_root not in path.parents:
        raise ValueError("LLM task harness state directory must remain inside the repository")
    return path


def _task_manager() -> HarnessTaskManager:
    global _manager
    if _manager is None:
        if not _manifest:
            raise RuntimeError("LLM Task Harness has not been initialized")
        _manager = HarnessTaskManager(
            _repository_root(_manifest),
            _manifest,
            state_directory=_state_directory(_manifest),
        )
    return _manager


def _http_error(error: Exception) -> HTTPException:
    if isinstance(error, KeyError):
        return HTTPException(status_code=404, detail=f"Unknown task: {error.args[0]}")
    if isinstance(error, PermissionError):
        return HTTPException(status_code=403, detail=str(error))
    if isinstance(error, (TypeError, ValueError, NotADirectoryError, FileNotFoundError)):
        return HTTPException(status_code=400, detail=str(error))
    return HTTPException(status_code=500, detail=str(error))


def initialize(manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate the configuration without contacting the model endpoint."""

    if str(manifest.get("routePrefix") or "") != f"/{PLUGIN_ID}":
        raise ValueError(f"routePrefix must be /{PLUGIN_ID}")
    for key in ("executionEnabled", "allowToolNetwork", "allowApprovalNever"):
        if key in manifest and not isinstance(manifest[key], bool):
            raise ValueError(f"{key} must be a JSON boolean")
    try:
        OpenAICompatibleAdapter.validate_base_url(str(manifest.get("modelBaseUrl") or ""))
    except ValueError as error:
        raise ValueError(f"invalid modelBaseUrl: {error}") from error
    profile = str(manifest.get("defaultPermissionProfile") or "workspace-write")
    if profile not in {"read-only", "workspace-write", "full-access"}:
        raise ValueError(f"Unknown defaultPermissionProfile: {profile}")
    maximum_profile = str(manifest.get("maximumPermissionProfile") or profile)
    if maximum_profile not in {"read-only", "workspace-write", "full-access"}:
        raise ValueError(f"Unknown maximumPermissionProfile: {maximum_profile}")
    if HarnessTaskManager.PERMISSION_RANK[profile] > HarnessTaskManager.PERMISSION_RANK[maximum_profile]:
        raise ValueError("defaultPermissionProfile cannot exceed maximumPermissionProfile")
    approval = str(manifest.get("defaultApprovalMode") or "on-request")
    if approval not in {"never", "on-request", "deny"}:
        raise ValueError(f"Unknown defaultApprovalMode: {approval}")
    if approval == "never" and not bool(manifest.get("allowApprovalNever", False)):
        raise ValueError("defaultApprovalMode never requires allowApprovalNever")
    if bool(manifest.get("allowToolNetwork", False)) and not list(manifest.get("allowedHosts") or []):
        raise ValueError("allowToolNetwork requires at least one allowed host")
    api_key_env = str(manifest.get("apiKeyEnv") or "")
    if not api_key_env or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env):
        raise ValueError("apiKeyEnv must be a non-empty environment-variable name")
    if not str(manifest.get("defaultModel") or "").strip():
        raise ValueError("defaultModel must not be empty")
    for key, minimum, maximum in (
        ("maxWorkers", 1, 16),
        ("maxSteps", 1, 500),
        ("modelTimeoutSeconds", 1, 3600),
        ("taskTimeoutSeconds", 1, 86400),
    ):
        value = manifest.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{key} must be an integer between {minimum} and {maximum}")
    return {
        "ready": initialization_report(manifest)["ready"],
        "executionEnabled": bool(manifest.get("executionEnabled", False)),
        "modelBaseUrl": str(manifest.get("modelBaseUrl") or ""),
        "defaultModel": str(manifest.get("defaultModel") or ""),
    }


def create_router(manifest: dict[str, Any] | None = None) -> APIRouter:
    """Create the task, capability, approval, and input routes.

    The embedded router below is always registered on the workbench. Unless
    embedded-only is requested (COPLEX_STDPY_PLUGIN_MODE=embedded), the
    same routes are additionally served by a detached standalone process on
    its own port, reached through the /coplex_stdpy web_proxy mount, so
    the harness survives workbench restarts.
    """

    global _manager, _manifest
    _manifest = dict(manifest or {})
    if (
        (os.environ.get("COPLEX_STDPY_PLUGIN_MODE") or "standalone").strip().lower() != "embedded"
        and os.environ.get("COPLEX_STDPY_STANDALONE_CHILD") != "1"
    ):
        try:
            from . import standalone as _standalone

            _standalone.launch()
        except Exception:
            # The embedded routes still serve; the mount just answers 502
            # until the standalone process is started manually.
            pass
    if _manager is None:
        _manager = HarnessTaskManager(
            _repository_root(_manifest),
            _manifest,
            state_directory=_state_directory(_manifest),
        )
    else:
        _manager.update_settings(_manifest)
    router = APIRouter(prefix=f"/{PLUGIN_ID}", tags=["llm-task-harness"])

    @router.on_event("shutdown")
    def shutdown_task_manager() -> None:
        global _manager
        if _manager is not None:
            _manager.close()
            _manager = None

    @router.get("", response_class=HTMLResponse, include_in_schema=False)
    def task_console() -> HTMLResponse:
        return HTMLResponse(
            _CONSOLE_PAGE.read_text(encoding="utf-8"),
            headers={
                "Content-Security-Policy": (
                    "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                    "connect-src 'self'; frame-ancestors 'self'"
                ),
            },
        )

    @router.get("/endpoints")
    def plugin_summary() -> dict[str, Any]:
        manager = _task_manager()
        tasks = manager.list(limit=1000)
        return {
            "id": PLUGIN_ID,
            "version": str(_manifest.get("version") or "0.1.0"),
            "executionEnabled": bool(manager.settings.get("executionEnabled", False)),
            "modelBaseUrl": str(manager.settings.get("modelBaseUrl") or ""),
            "defaultModel": str(manager.settings.get("defaultModel") or ""),
            "defaultPermissionProfile": str(
                manager.settings.get("defaultPermissionProfile") or "workspace-write"
            ),
            "maximumPermissionProfile": str(
                manager.settings.get("maximumPermissionProfile") or "workspace-write"
            ),
            "defaultApprovalMode": str(manager.settings.get("defaultApprovalMode") or "on-request"),
            "allowApprovalNever": bool(manager.settings.get("allowApprovalNever", False)),
            "processControlAvailable": {
                "shutdown": _process_control.get("shutdown") is not None,
                "restart": _process_control.get("restart") is not None,
            },
            "taskCounts": {
                state: sum(task["status"] == state for task in tasks)
                for state in sorted({task["status"] for task in tasks})
            },
            "links": {
                "endpoints": f"/{PLUGIN_ID}/endpoints",
                "health": f"/{PLUGIN_ID}/health",
                "capabilities": f"/{PLUGIN_ID}/capabilities",
                "models": f"/{PLUGIN_ID}/models",
                "tasks": f"/{PLUGIN_ID}/tasks",
                "ui": f"/{PLUGIN_ID}",
                "admin": f"/{PLUGIN_ID}/admin",
                "adminShutdown": f"/{PLUGIN_ID}/admin/shutdown",
                "adminRestart": f"/{PLUGIN_ID}/admin/restart",
            },
        }

    @router.get("/health")
    def health() -> dict[str, Any]:
        manager = _task_manager()
        tasks = manager.list(limit=1000)
        active = sum(task["status"] not in manager.TERMINAL for task in tasks)
        return {
            "ok": True,
            "plugin": PLUGIN_ID,
            "executionEnabled": bool(manager.settings.get("executionEnabled", False)),
            "activeTasks": active,
            "storedTasks": len(tasks),
        }

    @router.get("/capabilities")
    def capabilities() -> dict[str, Any]:
        return _task_manager().capabilities()

    @router.get("/models")
    def models() -> dict[str, Any]:
        try:
            data = _task_manager().models()
        except Exception as error:  # noqa: BLE001 - translated to an API result
            raise HTTPException(status_code=502, detail=str(error)) from error
        return {"object": "list", "data": data}

    @router.post("/tasks", status_code=http_status.HTTP_202_ACCEPTED)
    def create_task(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        try:
            return _task_manager().submit(body)
        except Exception as error:  # noqa: BLE001
            raise _http_error(error) from error

    @router.get("/tasks")
    def list_tasks(limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
        tasks = _task_manager().list(limit=limit)
        return {"tasks": tasks, "count": len(tasks)}

    @router.get("/tasks/{task_id}")
    def read_task(task_id: str) -> dict[str, Any]:
        try:
            return _task_manager().get(task_id)
        except Exception as error:  # noqa: BLE001
            raise _http_error(error) from error

    @router.get("/tasks/{task_id}/events")
    def read_events(task_id: str, after: int = Query(0, ge=0)) -> dict[str, Any]:
        try:
            events = _task_manager().events(task_id, after=after)
        except Exception as error:  # noqa: BLE001
            raise _http_error(error) from error
        return {"taskId": task_id, "events": events, "count": len(events)}

    @router.post("/tasks/{task_id}/cancel")
    def cancel_task(task_id: str) -> dict[str, Any]:
        try:
            return _task_manager().cancel(task_id)
        except Exception as error:  # noqa: BLE001
            raise _http_error(error) from error

    @router.post("/tasks/{task_id}/approvals/{call_id}")
    def decide_approval(
        task_id: str,
        call_id: str,
        body: dict[str, Any] = Body(default_factory=dict),
    ) -> dict[str, Any]:
        try:
            return _task_manager().decide_approval(task_id, call_id, str(body.get("decision") or ""))
        except Exception as error:  # noqa: BLE001
            raise _http_error(error) from error

    @router.post("/tasks/{task_id}/input")
    def provide_input(task_id: str, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        try:
            return _task_manager().provide_input(task_id, str(body.get("response") or ""))
        except Exception as error:  # noqa: BLE001
            raise _http_error(error) from error

    @router.post("/admin/shutdown")
    def request_shutdown() -> dict[str, Any]:
        hook = _process_control.get("shutdown")
        if hook is None:
            raise HTTPException(
                status_code=http_status.HTTP_501_NOT_IMPLEMENTED,
                detail=(
                    "process-level shutdown is unavailable: this process did not "
                    "register a shutdown hook (only coplex_stdpy.standalone does)"
                ),
            )
        hook()
        return {"ok": True, "action": "shutdown"}

    @router.post("/admin/restart")
    def request_restart() -> dict[str, Any]:
        hook = _process_control.get("restart")
        if hook is None:
            raise HTTPException(
                status_code=http_status.HTTP_501_NOT_IMPLEMENTED,
                detail=(
                    "process-level restart is unavailable: this process did not "
                    "register a restart hook (only coplex_stdpy.standalone does)"
                ),
            )
        hook()
        return {"ok": True, "action": "restart"}

    return router


def create_admin_router(manifest: dict[str, Any]) -> APIRouter:
    """Publish the native Workbench administration descriptor."""

    resolved = dict(manifest)

    def describe() -> dict[str, Any]:
        manager = _task_manager()
        tasks = manager.list(limit=1000)
        active = [task for task in tasks if task["status"] not in manager.TERMINAL]
        settings = manager.settings
        return descriptor(
            resolved,
            title="LLM Task Harness administration",
            summary="A provider-neutral coding-agent runtime with explicit execution and approval gates.",
            status_items=[
                status("Execution", "enabled" if settings.get("executionEnabled") else "disabled",
                       "warn" if settings.get("executionEnabled") else "ok",
                       detail="Execution is opt-in because task tools can edit files and start approved processes."),
                status("Model endpoint", settings.get("modelBaseUrl") or "not configured", "neutral"),
                status("Default model", settings.get("defaultModel") or "not configured", "neutral"),
                status("Active tasks", len(active), "warn" if active else "ok"),
                status("Stored tasks", len(tasks), "neutral"),
                status("Security boundary", "application guardrails", "warn",
                       detail="Use a container, VM, or restricted account for untrusted model output."),
            ],
            sections=[
                section(
                    "execution",
                    "Execution and approval policy",
                    [
                        field("executionEnabled", "Enable task execution", "boolean", bool(settings.get("executionEnabled", False)),
                              help_text="When off, POST /coplex_stdpy/tasks returns 403."),
                        field("defaultPermissionProfile", "Default permission profile", "select",
                              settings.get("defaultPermissionProfile") or "workspace-write",
                              options=["read-only", "workspace-write", "full-access"],
                              help_text="This selects tool categories, not an OS sandbox; approved processes execute host code."),
                        field("maximumPermissionProfile", "Maximum permission profile", "select",
                              settings.get("maximumPermissionProfile") or "workspace-write",
                              options=["read-only", "workspace-write", "full-access"],
                              help_text="API callers cannot request a profile above this administrator ceiling."),
                        field("defaultApprovalMode", "Default approval mode", "select",
                              settings.get("defaultApprovalMode") or "on-request",
                              options=["on-request", "deny", "never"],
                              help_text="On-request pauses write, execution, and network calls for a decision."),
                        field("allowApprovalNever", "Permit approval mode never", "boolean",
                              bool(settings.get("allowApprovalNever", False)),
                              help_text="Explicitly opt in before trusted tasks may auto-allow risky tools."),
                        field("allowToolNetwork", "Allow network tools", "boolean", bool(settings.get("allowToolNetwork", False)),
                              help_text="The full-access profile is also required for web tools."),
                        field("allowedHosts", "Allowed network hosts", "stringList", settings.get("allowedHosts") or [],
                              help_text="Required when network tools are enabled; private and loopback addresses remain blocked."),
                    ],
                    description="Every submitted task records its effective profile and approval mode.",
                ),
                section(
                    "provider",
                    "Model provider",
                    [
                        field("modelBaseUrl", "OpenAI-compatible base URL", "text",
                              settings.get("modelBaseUrl") or "http://127.0.0.1:8801/v1",
                              help_text="Remote endpoints require HTTPS; HTTP is accepted only for loopback development."),
                        field("defaultModel", "Default model", "text", settings.get("defaultModel") or "yourself/same"),
                        field("apiKeyEnv", "API key environment variable", "text", settings.get("apiKeyEnv") or "OPENAI_API_KEY",
                              help_text="Only the variable name is stored. Its value is never returned by this plugin."),
                    ],
                    description="The adapter uses chat completions and normalized function tools.",
                ),
                section(
                    "limits",
                    "Concurrency and limits",
                    [
                        field("maxWorkers", "Concurrent tasks", "number", int(settings.get("maxWorkers", 2))),
                        field("maxSteps", "Maximum model steps", "number", int(settings.get("maxSteps", 50))),
                        field("modelTimeoutSeconds", "Model-call timeout (seconds)", "number", int(settings.get("modelTimeoutSeconds", 120))),
                        field("taskTimeoutSeconds", "Whole-task timeout (seconds)", "number", int(settings.get("taskTimeoutSeconds", 1800))),
                    ],
                    description="Changing worker concurrency takes full effect after a Workbench restart.",
                ),
            ],
            actions=[
                action("probe-models", "Probe model endpoint", description="List models from the configured /models route."),
            ],
        )

    def apply_settings(values: Mapping[str, Any]) -> dict[str, Any]:
        global _manifest
        updates: dict[str, Any] = {}
        if "executionEnabled" in values:
            updates["executionEnabled"] = _boolean_setting(values["executionEnabled"], "executionEnabled")
        if "allowToolNetwork" in values:
            updates["allowToolNetwork"] = _boolean_setting(values["allowToolNetwork"], "allowToolNetwork")
        if "allowApprovalNever" in values:
            updates["allowApprovalNever"] = _boolean_setting(values["allowApprovalNever"], "allowApprovalNever")
        if "allowedHosts" in values:
            updates["allowedHosts"] = string_list(values["allowedHosts"])
        for key in (
            "modelBaseUrl",
            "defaultModel",
            "apiKeyEnv",
            "defaultPermissionProfile",
            "maximumPermissionProfile",
            "defaultApprovalMode",
        ):
            if key in values:
                updates[key] = str(values[key] or "").strip()
        try:
            OpenAICompatibleAdapter.validate_base_url(
                updates.get("modelBaseUrl", str(_task_manager().settings.get("modelBaseUrl") or ""))
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=f"invalid modelBaseUrl: {error}") from error
        api_key_env = updates.get("apiKeyEnv", str(_task_manager().settings.get("apiKeyEnv") or ""))
        if not api_key_env or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env):
            raise HTTPException(status_code=400, detail="apiKeyEnv must be a non-empty environment-variable name")
        if not updates.get("defaultModel", str(_task_manager().settings.get("defaultModel") or "")).strip():
            raise HTTPException(status_code=400, detail="defaultModel must not be empty")
        profile = updates.get("defaultPermissionProfile", str(_task_manager().settings.get("defaultPermissionProfile") or ""))
        if profile not in {"read-only", "workspace-write", "full-access"}:
            raise HTTPException(status_code=400, detail="invalid defaultPermissionProfile")
        maximum_profile = updates.get(
            "maximumPermissionProfile",
            str(_task_manager().settings.get("maximumPermissionProfile") or profile),
        )
        if maximum_profile not in {"read-only", "workspace-write", "full-access"}:
            raise HTTPException(status_code=400, detail="invalid maximumPermissionProfile")
        if HarnessTaskManager.PERMISSION_RANK[profile] > HarnessTaskManager.PERMISSION_RANK[maximum_profile]:
            raise HTTPException(status_code=400, detail="defaultPermissionProfile exceeds maximumPermissionProfile")
        approval = updates.get("defaultApprovalMode", str(_task_manager().settings.get("defaultApprovalMode") or ""))
        if approval not in {"on-request", "deny", "never"}:
            raise HTTPException(status_code=400, detail="invalid defaultApprovalMode")
        allow_never = updates.get(
            "allowApprovalNever",
            bool(_task_manager().settings.get("allowApprovalNever", False)),
        )
        if approval == "never" and not allow_never:
            raise HTTPException(status_code=400, detail="defaultApprovalMode never requires allowApprovalNever")
        effective_network = updates.get(
            "allowToolNetwork",
            bool(_task_manager().settings.get("allowToolNetwork", False)),
        )
        effective_hosts = updates.get(
            "allowedHosts",
            list(_task_manager().settings.get("allowedHosts") or []),
        )
        if effective_network and not effective_hosts:
            raise HTTPException(status_code=400, detail="allowToolNetwork requires at least one allowed host")
        for key, minimum, maximum in (
            ("maxWorkers", 1, 16),
            ("maxSteps", 1, 500),
            ("modelTimeoutSeconds", 1, 3600),
            ("taskTimeoutSeconds", 1, 86400),
        ):
            if key in values:
                try:
                    number = int(values[key])
                except (TypeError, ValueError) as error:
                    raise HTTPException(status_code=400, detail=f"{key} must be an integer") from error
                if not minimum <= number <= maximum:
                    raise HTTPException(status_code=400, detail=f"{key} must be between {minimum} and {maximum}")
                updates[key] = number
        stored = write_manifest_values(resolved, updates)
        _manifest = {**_manifest, **stored}
        _task_manager().update_settings(stored)
        return stored

    async def probe_models(_: dict[str, Any]) -> dict[str, Any]:
        models = await asyncio.to_thread(_task_manager().models)
        return {
            "ok": True,
            "count": len(models),
            "models": [str(model.get("id") or "") for model in models[:100]],
        }

    return build_admin_router(
        resolved,
        describe=describe,
        apply_settings=apply_settings,
        actions={"probe-models": probe_models},
        initialize=lambda _: initialize({**resolved, **_task_manager().settings}),
    )


def _boolean_setting(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise HTTPException(status_code=400, detail=f"{name} must be a boolean")


def resolve_ui_pages(
    _: Mapping[str, Any],
    pages: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve the task console as an iframe and configuration natively."""

    resolved: list[dict[str, Any]] = []
    for page in pages:
        item = dict(page)
        if item.get("id") == "task-console":
            item.update({
                "external": True,
                "address": f"/{PLUGIN_ID}",
            })
        elif item.get("external"):
            item["address"] = str(item.get("descriptor") or "")
        else:
            item.update({
                "external": False,
                "address": f"/api{item.get('descriptor') or f'/{PLUGIN_ID}/admin'}",
            })
        resolved.append(item)
    return resolved


__all__ = [
    "create_admin_router",
    "create_router",
    "initialize",
    "register_process_control",
    "resolve_ui_pages",
]
