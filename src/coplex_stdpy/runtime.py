"""Provider-neutral, policy-gated repository task harness runtime.

The module supplies the reusable core for the ``coplex_stdpy`` Workbench
plugin.  It deliberately separates four concerns:

* :class:`LLMTaskHarness` owns one model/tool loop and its repository boundary.
* :class:`OpenAICompatibleAdapter` translates that loop to an OpenAI-compatible
  ``/chat/completions`` endpoint.
* :class:`HarnessTaskManager` gives the loop durable asynchronous task state,
  ordered events, cancellation, approvals, and human-input pauses.
* the sibling ``plugin.py`` module owns HTTP and administration routes.

This is an application-level guardrail, not an operating-system sandbox.  A
deployment that executes untrusted model output must still use a container,
VM, restricted account, or comparable isolation boundary.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import fnmatch
import hashlib
import http.client
import ipaddress
import json
import os
import platform
import queue
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


Adapter = Callable[[dict[str, Any]], Mapping[str, Any]]
Approval = Callable[[str, Mapping[str, Any], str, str], Any]
EventHandler = Callable[[Mapping[str, Any]], Any]
InputHandler = Callable[[str, Mapping[str, Any]], str]
TranscriptHandler = Callable[[Mapping[str, Any]], Any]


class _DaemonResolverPool:
    """A bounded daemon pool for OS resolver calls that Python cannot cancel."""

    def __init__(self, workers: int = 4, queue_size: int = 32) -> None:
        self.workers = workers
        self._jobs: queue.Queue[tuple[str, int, queue.Queue[tuple[bool, Any]]]] = queue.Queue(
            maxsize=queue_size
        )
        self._lock = threading.Lock()
        self._started = False

    def submit(self, host: str, port: int) -> queue.Queue[tuple[bool, Any]]:
        with self._lock:
            if not self._started:
                for index in range(self.workers):
                    thread = threading.Thread(
                        target=self._worker,
                        daemon=True,
                        name=f"llm-task-dns-resolver-{index + 1}",
                    )
                    thread.start()
                self._started = True
        result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
        try:
            self._jobs.put_nowait((host, port, result))
        except queue.Full as error:
            raise RuntimeError("DNS resolver capacity is exhausted") from error
        return result

    def _worker(self) -> None:
        while True:
            host, port, result = self._jobs.get()
            try:
                value = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                result.put((True, value))
            except BaseException as error:
                result.put((False, error))
            finally:
                self._jobs.task_done()


_DNS_RESOLVER_POOL = _DaemonResolverPool()


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Connect to one already-validated address while retaining the HTTP host."""

    def __init__(self, host: str, address: str, port: int, *, timeout: float) -> None:
        self._pinned_address = address
        self._abort_lock = threading.RLock()
        self._aborted = threading.Event()
        super().__init__(host, port=port, timeout=timeout)

    def request(self, *args: Any, **kwargs: Any) -> None:
        if self._aborted.is_set():
            raise http.client.CannotSendRequest("connection was aborted")
        super().request(*args, **kwargs)

    def connect(self) -> None:
        if self._aborted.is_set():
            raise http.client.CannotSendRequest("connection was aborted")
        sock = socket.create_connection(
            (self._pinned_address, self.port),
            self.timeout,
            self.source_address,
        )
        with self._abort_lock:
            if self._aborted.is_set():
                sock.close()
                raise http.client.CannotSendRequest("connection was aborted")
            self.sock = sock

    def abort(self) -> None:
        self._aborted.set()
        with self._abort_lock:
            super().close()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS connection pinned to a validated address with normal SNI checks."""

    def __init__(self, host: str, address: str, port: int, *, timeout: float) -> None:
        self._pinned_address = address
        self._abort_lock = threading.RLock()
        self._aborted = threading.Event()
        self._connecting_socket: socket.socket | None = None
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())

    def request(self, *args: Any, **kwargs: Any) -> None:
        if self._aborted.is_set():
            raise http.client.CannotSendRequest("connection was aborted")
        super().request(*args, **kwargs)

    def connect(self) -> None:
        if self._aborted.is_set():
            raise http.client.CannotSendRequest("connection was aborted")
        sock = socket.create_connection(
            (self._pinned_address, self.port),
            self.timeout,
            self.source_address,
        )
        with self._abort_lock:
            if self._aborted.is_set():
                sock.close()
                raise http.client.CannotSendRequest("connection was aborted")
            self._connecting_socket = sock
        try:
            wrapped = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            with self._abort_lock:
                self._connecting_socket = None
            sock.close()
            raise
        with self._abort_lock:
            self._connecting_socket = None
            if self._aborted.is_set():
                wrapped.close()
                raise http.client.CannotSendRequest("connection was aborted")
            self.sock = wrapped

    def abort(self) -> None:
        self._aborted.set()
        with self._abort_lock:
            connecting = self._connecting_socket
            self._connecting_socket = None
            super().close()
            if connecting is not None:
                connecting.close()


class _PinnedResponse:
    """Ensure the response and its pinned connection close together."""

    def __init__(
        self,
        response: http.client.HTTPResponse,
        connection: http.client.HTTPConnection,
        on_close: Callable[["_PinnedResponse"], None] | None = None,
    ) -> None:
        self._response = response
        self._connection = connection
        self._on_close = on_close
        self._close_lock = threading.Lock()
        self._closed = False
        self._deadline_timer: threading.Timer | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)

    def __enter__(self) -> "_PinnedResponse":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def attach_deadline_timer(self, timer: threading.Timer) -> None:
        with self._close_lock:
            if self._closed:
                timer.cancel()
                return
            self._deadline_timer = timer
        timer.start()

    def set_timeout(self, timeout: float) -> None:
        with contextlib.suppress(AttributeError, OSError):
            if self._connection.sock is not None:
                self._connection.sock.settimeout(timeout)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            timer = self._deadline_timer
            self._deadline_timer = None
        if timer is not None:
            timer.cancel()
        try:
            self._response.close()
        finally:
            self._connection.close()
            if self._on_close is not None:
                self._on_close(self)


@dataclass(frozen=True)
class ToolDefinition:
    """One dynamically registered tool and its permission risk."""

    name: str
    description: str
    parameters: dict[str, Any]
    risk: str
    handler: Callable[[dict[str, Any]], dict[str, Any]]

    def specification(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class OpenAICompatibleAdapter:
    """Translate normalized harness requests to an OpenAI-compatible server."""

    OPTION_KEYS = {
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "reasoning_effort",
        "seed",
        "tool_choice",
    }

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key_env: str = "OPENAI_API_KEY",
        timeout: float = 120.0,
        transport: Any | None = None,
    ) -> None:
        self.validate_base_url(base_url)
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.transport = transport
        self._client_lock = threading.RLock()
        self._active_clients: set[Any] = set()
        self._request_clients: dict[threading.Event, Any] = {}

    def cancel(self) -> None:
        """Cooperatively close every active provider request."""

        with self._client_lock:
            clients = tuple(self._active_clients)
        for client in clients:
            with contextlib.suppress(Exception):
                client.close()

    def cancel_request(self, cancellation: threading.Event) -> None:
        """Close only the provider client associated with one model request."""

        with self._client_lock:
            client = self._request_clients.get(cancellation)
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()

    @staticmethod
    def validate_base_url(base_url: str) -> urllib.parse.SplitResult:
        """Require authenticated/model traffic to use TLS outside local loopback."""

        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        host = parsed.hostname.casefold().rstrip(".")
        loopback = host == "localhost"
        with contextlib.suppress(ValueError):
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        if parsed.scheme == "http" and not loopback:
            raise ValueError("remote model endpoints require HTTPS; HTTP is allowed only on loopback")
        return parsed

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        token = os.environ.get(self.api_key_env, "").strip() if self.api_key_env else ""
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def _messages(request: Mapping[str, Any]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        instructions = str(request.get("instructions") or "").strip()
        if instructions:
            messages.append({"role": "system", "content": instructions})
        for raw in request.get("messages", []):
            if not isinstance(raw, Mapping):
                continue
            role = str(raw.get("role") or "")
            if role == "assistant":
                item: dict[str, Any] = {
                    "role": "assistant",
                    "content": raw.get("content") or "",
                }
                calls = []
                for call in raw.get("tool_calls", []) or []:
                    if not isinstance(call, Mapping):
                        continue
                    calls.append({
                        "id": str(call.get("id") or uuid.uuid4()),
                        "type": "function",
                        "function": {
                            "name": str(call.get("name") or ""),
                            "arguments": json.dumps(call.get("arguments") or {}),
                        },
                    })
                if calls:
                    item["tool_calls"] = calls
                messages.append(item)
            elif role == "tool":
                content = raw.get("content", "")
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False, default=str)
                messages.append({
                    "role": "tool",
                    "tool_call_id": str(raw.get("tool_call_id") or ""),
                    "content": content,
                })
            elif role in {"system", "user"}:
                content = raw.get("content", "")
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False, default=str)
                messages.append({"role": role, "content": content})
        return messages

    @staticmethod
    def _tools(request: Mapping[str, Any]) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for raw in request.get("tools", []):
            if not isinstance(raw, Mapping):
                continue
            tools.append({
                "type": "function",
                "function": {
                    "name": str(raw.get("name") or ""),
                    "description": str(raw.get("description") or ""),
                    "parameters": raw.get("parameters") or {"type": "object", "properties": {}},
                },
            })
        return tools

    def __call__(self, request: dict[str, Any]) -> Mapping[str, Any]:
        try:
            import httpx
        except ImportError as error:  # pragma: no cover - reported by plugin initialization
            raise RuntimeError("httpx is required for OpenAICompatibleAdapter") from error
        options = request.get("options") if isinstance(request.get("options"), Mapping) else {}
        payload: dict[str, Any] = {
            "model": str(request.get("model") or self.model),
            "messages": self._messages(request),
        }
        tools = self._tools(request)
        if tools:
            payload["tools"] = tools
        for key in self.OPTION_KEYS:
            if key in options:
                payload[key] = options[key]
        cancellation = request.get("cancellation_event")
        if isinstance(cancellation, threading.Event) and cancellation.is_set():
            raise RuntimeError("model request was cancelled before dispatch")
        client = httpx.Client(timeout=self.timeout, transport=self.transport)
        with self._client_lock:
            self._active_clients.add(client)
            if isinstance(cancellation, threading.Event):
                self._request_clients[cancellation] = client
        try:
            if isinstance(cancellation, threading.Event) and cancellation.is_set():
                raise RuntimeError("model request was cancelled before dispatch")
            response = client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        finally:
            with self._client_lock:
                self._active_clients.discard(client)
                if isinstance(cancellation, threading.Event):
                    self._request_clients.pop(cancellation, None)
            client.close()
        choices = body.get("choices") if isinstance(body, Mapping) else None
        if not isinstance(choices, list) or not choices:
            raise ValueError("model response has no choices")
        message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
        if not isinstance(message, Mapping):
            raise ValueError("model response has no assistant message")
        calls: list[dict[str, Any]] = []
        for raw in message.get("tool_calls", []) or []:
            if not isinstance(raw, Mapping):
                continue
            function = raw.get("function") if isinstance(raw.get("function"), Mapping) else {}
            arguments: Any = function.get("arguments") or {}
            if isinstance(arguments, str):
                arguments = json.loads(arguments or "{}")
            calls.append({
                "id": str(raw.get("id") or uuid.uuid4()),
                "name": str(function.get("name") or ""),
                "arguments": arguments,
            })
        usage = body.get("usage") if isinstance(body, Mapping) else None
        return {
            "content": message.get("content") or "",
            "tool_calls": calls,
            "usage": dict(usage) if isinstance(usage, Mapping) else {},
        }

    def list_models(self) -> list[dict[str, Any]]:
        try:
            import httpx
        except ImportError as error:  # pragma: no cover
            raise RuntimeError("httpx is required for OpenAICompatibleAdapter") from error
        with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
            response = client.get(f"{self.base_url}/models", headers=self._headers())
            response.raise_for_status()
            payload = response.json()
        data = payload.get("data") if isinstance(payload, Mapping) else None
        return [dict(item) for item in data or [] if isinstance(item, Mapping)]


class LLMTaskHarness:
    """A single repository-scoped coding-agent run with extensible tools."""

    DEFAULT_INSTRUCTIONS = """You are a persistent coding agent operating in a repository.
Inspect and search before making assumptions. Obey every applicable AGENTS.md.
Preserve unrelated user changes. Make focused, reviewable edits and prefer
patch-based modification. Run relevant tests and diagnose failures. Never claim
a command, edit, or test succeeded without a successful tool result. Continue
until the requested work is fully implemented and verified. Ask only when a
material decision or new authority is required. Avoid destructive operations.
Use subagents for independent work only. At completion, concisely report
changes, verification, and limitations."""

    PROFILE_RISKS = {
        "read-only": frozenset({"read", "state", "model"}),
        "workspace-write": frozenset({"read", "write", "execute", "state", "model"}),
        "full-access": frozenset({"read", "write", "execute", "network", "state", "model"}),
    }
    READ_ONLY_SUBAGENT_TOOLS = frozenset({
        "read_file", "list_files", "search", "file_info",
        "git_status", "git_diff", "git_log", "git_show", "web_get",
    })
    DEFAULT_DENIED_GLOBS = (
        ".git",
        ".git/**",
        "**/.git",
        "**/.git/**",
        ".env",
        "**/.env",
        "**/.env.*",
        "**/.credentials",
        "**/*.pem",
        "**/*.key",
        "**/id_rsa",
        "**/id_ed25519",
    )
    SAFE_ENVIRONMENT_KEYS = frozenset({
        "PATH", "PATHEXT", "SystemRoot", "WINDIR", "TEMP", "TMP", "TMPDIR",
        "LANG", "LC_ALL", "TERM", "ComSpec", "HOME", "USERPROFILE",
    })

    def __init__(
        self,
        adapter: Adapter,
        root: str | os.PathLike[str] = ".",
        *,
        model: str = "default",
        instructions: str | None = None,
        permission_profile: str = "read-only",
        max_steps: int = 50,
        timeout: float = 120.0,
        overall_timeout: float | None = None,
        allow_shell: bool = False,
        allow_network: bool = False,
        allowed_hosts: Sequence[str] = (),
        readable_paths: Sequence[str] = (".",),
        writable_paths: Sequence[str] = (".",),
        denied_globs: Sequence[str] = DEFAULT_DENIED_GLOBS,
        environment: Mapping[str, str] | None = None,
        max_output_bytes: int = 1_000_000,
        max_file_bytes: int = 4_000_000,
        max_download_bytes: int = 10_000_000,
        max_context_bytes: int = 200_000,
        max_message_bytes: int = 2_000_000,
        subagent_limit: int = 4,
        subagent_tools: Sequence[str] = tuple(READ_ONLY_SUBAGENT_TOOLS),
        approval: Approval | None = None,
        event_handler: EventHandler | None = None,
        input_handler: InputHandler | None = None,
        transcript_handler: TranscriptHandler | None = None,
        web_search_adapter: Callable[[str, int], Any] | None = None,
        persistence_file: str | os.PathLike[str] | None = None,
        redact: Sequence[str] = (),
        test_command: Sequence[str] | None = None,
        repeated_failure_limit: int = 3,
    ) -> None:
        if not callable(adapter):
            raise TypeError("adapter must be callable")
        if permission_profile not in self.PROFILE_RISKS:
            raise ValueError(f"unknown permission profile: {permission_profile}")
        if min(max_steps, timeout, subagent_limit, max_output_bytes, max_context_bytes) <= 0:
            raise ValueError("limits and timeouts must be positive")
        self.id = str(uuid.uuid4())
        self.adapter = adapter
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise NotADirectoryError(self.root)
        self.model = model
        self.instructions = instructions or self.DEFAULT_INSTRUCTIONS
        self.permission_profile = permission_profile
        self.max_steps = max_steps
        self.timeout = timeout
        self.overall_timeout = overall_timeout
        self.allow_shell = allow_shell
        self.allow_network = allow_network
        self.allowed_hosts = tuple(host.lower().rstrip(".") for host in allowed_hosts if host)
        if self.allow_network and not self.allowed_hosts:
            raise ValueError("network tools require a non-empty allowed_hosts list")
        self.denied_globs = tuple(str(pattern) for pattern in denied_globs if str(pattern).strip())
        self.readable_roots = tuple(self._initial_scope(path) for path in readable_paths)
        self.writable_roots = tuple(self._initial_scope(path) for path in writable_paths)
        self.environment = {str(key): str(value) for key, value in (environment or {}).items()}
        self.max_output_bytes = max_output_bytes
        self.max_file_bytes = max_file_bytes
        self.max_download_bytes = max_download_bytes
        self.max_context_bytes = max_context_bytes
        self.max_message_bytes = max_message_bytes
        self.subagent_limit = subagent_limit
        self.subagent_tools = frozenset(subagent_tools) & self.READ_ONLY_SUBAGENT_TOOLS
        self.approval = approval
        self.event_handler = event_handler
        self.input_handler = input_handler
        self.transcript_handler = transcript_handler
        self.web_search_adapter = web_search_adapter
        self.redactions = tuple(secret for secret in redact if secret)
        self.test_command = tuple(test_command) if test_command else None
        self.repeated_failure_limit = repeated_failure_limit
        self.messages: list[dict[str, Any]] = []
        self.plan: list[dict[str, Any]] = []
        self.step = 0
        self.cancelled = False
        self.closed = False
        self.current_task: str | None = None
        self._run_anchor_index = 0
        self._started_at: float | None = None
        self._lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._processes: set[subprocess.Popen[bytes]] = set()
        self._network_resources: set[Any] = set()
        self._adapter_threads: set[threading.Thread] = set()
        self._adapter_cancel_events: set[threading.Event] = set()
        self._children: set["LLMTaskHarness"] = set()
        self._failure_counts: dict[str, int] = {}
        self._tools: dict[str, ToolDefinition] = {}
        self._register_builtin_tools()
        self.persistence_file = (
            self._path(persistence_file, "write", may_not_exist=True)
            if persistence_file is not None else None
        )
        self._emit("harness.created", root=str(self.root), model=self.model)

    def __enter__(self) -> "LLMTaskHarness":
        self._ensure_open()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
            should_cancel = bool(
                self.current_task is not None
                or self._processes
                or self._network_resources
                or self._adapter_threads
                or self._children
            )
        if should_cancel:
            self.cancel()
        with self._lock:
            self._emit("harness.closed")

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            for process in tuple(self._processes):
                self._terminate_process_tree(process)
            adapter_cancellations = tuple(self._adapter_cancel_events)
            for cancellation in adapter_cancellations:
                cancellation.set()
            children = tuple(self._children)
            network_resources = tuple(self._network_resources)
            self._emit("run.cancelled")
        for resource in network_resources:
            with contextlib.suppress(Exception):
                abort = getattr(resource, "abort", None)
                (abort if callable(abort) else resource.close)()
        for child in children:
            child.cancel()
        cancel_request = getattr(self.adapter, "cancel_request", None)
        if callable(cancel_request):
            for cancellation in adapter_cancellations:
                with contextlib.suppress(Exception):
                    cancel_request(cancellation)
        else:
            cancel_adapter = getattr(self.adapter, "cancel", None)
            if callable(cancel_adapter):
                with contextlib.suppress(Exception):
                    cancel_adapter()

    def reset(self) -> None:
        self._ensure_open()
        if self._run_lock.locked():
            raise RuntimeError("cannot reset while a run is active")
        with self._lock:
            if any(thread.is_alive() for thread in self._adapter_threads) or self._children:
                raise RuntimeError("cannot reset while adapter or subagent work is active")
            self.messages.clear()
            self.plan.clear()
            self.step = 0
            self.cancelled = False
            self.current_task = None
            self._failure_counts.clear()
            self._emit("conversation.reset")

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "root": str(self.root),
                "model": self.model,
                "permission_profile": self.permission_profile,
                "step": self.step,
                "cancelled": self.cancelled,
                "closed": self.closed,
                "current_task": self.current_task,
                "message_count": len(self.messages),
                "plan": list(self.plan),
            }

    def capabilities(self) -> dict[str, Any]:
        return {
            "tools": [definition.specification() | {"risk": definition.risk}
                      for definition in self._tools.values()],
            "permissionProfiles": sorted(self.PROFILE_RISKS),
            "features": [
                "provider-neutral tool loop",
                "OpenAI-compatible adapter",
                "repository-scoped files and search",
                "atomic writes and patch application",
                "direct process and test execution",
                "read-only Git inspection",
                "optional guarded HTTP retrieval",
                "durable JSONL transcripts and events",
                "approval and human-input pauses",
                "bounded iterative read-only subagents",
                "cancellation and process-tree termination",
                "dynamic tool registration",
                "deterministic context compaction",
            ],
            "securityBoundary": "application guardrails; use OS isolation for untrusted model output",
        }

    def register_tool(
        self,
        name: str,
        description: str,
        parameters: Mapping[str, Any],
        handler: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        risk: str = "read",
        replace: bool = False,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"invalid tool name: {name}")
        if risk not in {"read", "write", "execute", "network", "state", "model"}:
            raise ValueError(f"invalid tool risk: {risk}")
        if name in self._tools and not replace:
            raise ValueError(f"tool already registered: {name}")
        schema = dict(parameters)
        if schema.get("type") != "object":
            raise ValueError("tool parameters must be an object schema")
        self._tools[name] = ToolDefinition(name, description, schema, risk, handler)

    def tool_specs(self) -> list[dict[str, Any]]:
        return [definition.specification() for definition in self._tools.values()]

    def run(
        self,
        task: str,
        *,
        options: Mapping[str, Any] | None = None,
        extra_context: str | None = None,
    ) -> str:
        self._ensure_open()
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must be non-empty text")
        if len(task.encode()) > self.max_message_bytes:
            raise ValueError("task exceeds configured message limit")
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("this harness already has an active run")
        try:
            with self._lock:
                if self.cancelled:
                    raise RuntimeError("agent run was cancelled; call reset() before reusing it")
                self.step = 0
                self.current_task = task
                self._failure_counts.clear()
                self._started_at = time.monotonic()
                self._run_anchor_index = len(self.messages)
            user_message = {"role": "user", "content": task}
            user_size = len(json.dumps(user_message, ensure_ascii=False).encode())
            context_budget = self.max_context_bytes - user_size - 256
            if context_budget < 256:
                raise ValueError("task and mandatory repository context exceed the model context budget")
            context = self.repository_context(
                context_budget,
                additional_instructions=extra_context,
            )
            self._check_running()
            initial = [
                {"role": "system", "content": context},
                user_message,
            ]
            with self._lock:
                if self.cancelled:
                    raise RuntimeError("agent run was cancelled")
                self.messages.extend(initial)
            self._persist_many(initial)
            self._emit("run.started", task=task)
            while True:
                self._check_running()
                with self._lock:
                    self.step += 1
                    step = self.step
                if step > self.max_steps:
                    raise RuntimeError(f"agent exceeded max_steps={self.max_steps}")
                request = {
                    "model": self.model,
                    "instructions": self.instructions,
                    "messages": self._compacted_messages(),
                    "tools": self.tool_specs(),
                    "options": dict(options or {}),
                    "metadata": {"harness_id": self.id, "step": step},
                }
                self._emit("model.request", step=step)
                started = time.monotonic()
                reply = self._invoke_adapter(request)
                self._emit(
                    "model.response",
                    step=step,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    tool_calls=len(reply["tool_calls"]),
                    usage=reply.get("usage", {}),
                )
                assistant = {"role": "assistant", **reply}
                self._append_message(assistant)
                if not reply["tool_calls"]:
                    answer = reply["content"]
                    self._emit("run.finished", answer=answer, steps=step)
                    return answer
                for call in reply["tool_calls"]:
                    self._check_running()
                    result = self.execute_tool(call["name"], call["arguments"], call_id=call["id"])
                    self._record_failure(call, result)
                    self._append_message({
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": call["name"],
                        "content": result,
                    })
        except BaseException as error:
            self._emit("run.failed", error=str(error), error_type=error.__class__.__name__)
            raise
        finally:
            with self._lock:
                self.current_task = None
            self._run_lock.release()

    def execute_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        call_id: str | None = None,
    ) -> dict[str, Any]:
        self._check_running()
        args = dict(arguments or {})
        definition = self._tools.get(name)
        if definition is None:
            return self._error(name, "unknown_tool", f"Unknown tool: {name}")
        risk = definition.risk
        if risk not in self.PROFILE_RISKS[self.permission_profile]:
            return self._error(
                name,
                "permission_error",
                f"{self.permission_profile} does not permit {risk} tools",
            )
        if risk == "network" and not self.allow_network:
            return self._error(name, "permission_error", "network access is disabled")
        if risk == "execute" and not self.allow_shell:
            return self._error(name, "permission_error", "process execution is disabled")
        tool_call_id = call_id or str(uuid.uuid4())
        try:
            self._approve(name, args, risk, tool_call_id)
        except TimeoutError:
            raise
        except Exception as error:
            return self._error(name, "permission_error", str(error))
        self._check_running()
        started = time.monotonic()
        self._emit("tool.started", tool=name, call_id=tool_call_id, arguments=args, risk=risk)
        try:
            data = definition.handler(args)
            result = {"ok": True, "tool": name, **(data or {})}
        except Exception as error:
            result = self._error(name, error.__class__.__name__, str(error))
            if os.environ.get("COPLEX_STDPY_TRACEBACK"):
                result["error"]["traceback"] = traceback.format_exc()
        result["duration_ms"] = round((time.monotonic() - started) * 1000)
        result = self._redact_obj(result)
        self._emit("tool.finished", tool=name, call_id=tool_call_id, result=result)
        return result

    def save(self, path: str | os.PathLike[str]) -> None:
        target = self._path(path, "write", may_not_exist=True)
        payload = {
            "version": 1,
            "id": self.id,
            "messages": self._snapshot_messages(),
            "model": self.model,
            "plan": list(self.plan),
        }
        self._atomic_write_text(target, json.dumps(payload, ensure_ascii=False, indent=2))

    def restore(self, path: str | os.PathLike[str]) -> None:
        target = self._path(path, "read")
        if target.stat().st_size > self.max_file_bytes:
            raise ValueError("state file exceeds configured file limit")
        data = json.loads(target.read_text(encoding="utf-8"))
        messages = data.get("messages") if isinstance(data, Mapping) else None
        if data.get("version") != 1 or not isinstance(messages, list):
            raise ValueError("invalid harness state file")
        normalized = [self._normalize_message(message) for message in messages]
        encoded = json.dumps(normalized, ensure_ascii=False, default=str).encode()
        if len(encoded) > self.max_message_bytes:
            raise ValueError("restored messages exceed configured limit")
        with self._lock:
            self.messages = normalized
            self.plan = [dict(item) for item in data.get("plan", []) if isinstance(item, Mapping)]

    def repository_context(
        self,
        budget: int | None = None,
        *,
        additional_instructions: str | None = None,
    ) -> str:
        limit = self.max_context_bytes if budget is None else int(budget)
        critical = [
            f"Repository root: {self.root}",
            f"Platform: {platform.platform()}",
            f"Python: {platform.python_version()}",
            f"Permission profile: {self.permission_profile}",
        ]
        instructions: list[str] = []
        instruction_bytes = 0
        for candidate in sorted(self.root.rglob("AGENTS.md")):
            if self._is_denied(candidate) or not self._instruction_applies_to_readable_scope(candidate):
                continue
            with contextlib.suppress(OSError, UnicodeDecodeError):
                relative = candidate.relative_to(self.root).as_posix()
                header = f"--- {'AGENTS.md' if relative == 'AGENTS.md' else relative} ---\n"
                prospective = instruction_bytes + len(header.encode()) + candidate.stat().st_size
                if prospective > limit:
                    raise ValueError("applicable repository instructions exceed the model context budget")
                instructions.append(header + candidate.read_text(encoding="utf-8"))
                instruction_bytes = prospective
        if instructions:
            critical.append("Repository instructions:\n" + "\n".join(instructions))
        if additional_instructions:
            critical.append("Ancestor repository instructions:\n" + additional_instructions)
        critical_text = "\n\n".join(critical)
        critical_size = len(critical_text.encode())
        if critical_size > limit:
            raise ValueError("applicable repository instructions exceed the model context budget")

        optional: list[str] = []
        try:
            optional.append(
                f"Git branch:\n{self._run_process(['git', 'branch', '--show-current'], timeout=10)['stdout']}"
            )
        except Exception as error:
            optional.append(f"Git branch: unavailable ({error})")
        try:
            optional.append(f"Git status:\n{self._tool_git_status({})['stdout']}")
        except Exception as error:
            optional.append(f"Git status: unavailable ({error})")
        try:
            listing = self._tool_list_files({"path": ".", "limit": 300})
            optional.append("Bounded file tree:\n" + "\n".join(listing["files"]))
        except Exception:
            pass
        remaining = max(0, limit - critical_size - 2)
        optional_budget = remaining // 2
        if not optional or optional_budget <= 0:
            return critical_text
        optional_text = "\n\n".join(optional).encode()[:optional_budget].decode(errors="ignore")
        return critical_text + "\n\n" + optional_text

    def _register_builtin_tools(self) -> None:
        string = {"type": "string"}
        boolean = {"type": "boolean"}
        integer = {"type": "integer"}
        strings = {"type": "array", "items": string}

        def schema(properties: Mapping[str, Any], required: Sequence[str] = ()) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": dict(properties),
                "required": list(required),
                "additionalProperties": False,
            }

        registrations = [
            ("read_file", "Read a UTF-8 repository file", {"path": string, "start_line": integer, "end_line": integer}, ("path",), "read", self._tool_read_file),
            ("write_file", "Atomically create or replace a UTF-8 repository file", {"path": string, "content": string}, ("path", "content"), "write", self._tool_write_file),
            ("list_files", "List repository files", {"path": string, "glob": string, "limit": integer}, (), "read", self._tool_list_files),
            ("search", "Search repository text", {"query": string, "path": string, "glob": string, "case_sensitive": boolean, "regex": boolean, "limit": integer}, ("query",), "read", self._tool_search),
            ("apply_patch", "Apply a repository-scoped unified Git patch", {"patch": string}, ("patch",), "write", self._tool_apply_patch),
            ("file_info", "Inspect a repository path", {"path": string}, ("path",), "read", self._tool_file_info),
            ("make_directory", "Create a repository directory", {"path": string}, ("path",), "write", self._tool_make_directory),
            ("shell", "Run an approved program directly without an implicit shell", {"command": string, "args": strings, "timeout": integer}, ("command",), "execute", self._tool_shell),
            ("run_tests", "Run an explicit or automatically detected test command", {"command": string, "args": strings, "timeout": integer}, (), "execute", self._tool_run_tests),
            ("git_status", "Read Git status", {}, (), "read", self._tool_git_status),
            ("git_diff", "Read Git diff", {"cached": boolean, "path": string}, (), "read", self._tool_git_diff),
            ("git_log", "Read recent Git history", {"limit": integer}, (), "read", self._tool_git_log),
            ("git_show", "Show a Git commit summary", {"object": string}, (), "read", self._tool_git_show),
            ("web_get", "Retrieve a permitted HTTP(S) URL", {"url": string}, ("url",), "network", self._tool_web_get),
            ("web_search", "Search the web through an injected adapter", {"query": string, "limit": integer}, ("query",), "network", self._tool_web_search),
            ("download", "Download a permitted URL into the repository", {"url": string, "path": string}, ("url", "path"), "network", self._tool_download),
            ("subagents", "Run bounded iterative read-only analysis agents", {"tasks": strings, "instructions": string}, ("tasks",), "model", self._tool_subagents),
            ("update_plan", "Replace the current task plan", {"plan": {"type": "array", "items": {"type": "object"}}, "explanation": string}, ("plan",), "state", self._tool_update_plan),
            ("request_user_input", "Pause for a concise user response", {"prompt": string, "context": {"type": "object"}}, ("prompt",), "state", self._tool_request_user_input),
        ]
        for name, description, properties, required, risk, handler in registrations:
            self.register_tool(name, description, schema(properties, required), handler, risk=risk)

    def _tool_read_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self._path(arguments["path"], "read")
        if not path.is_file():
            raise FileNotFoundError(path)
        size = path.stat().st_size
        if size > self.max_file_bytes:
            raise ValueError(f"file is {size} bytes; limit is {self.max_file_bytes}")
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        start = max(1, int(arguments.get("start_line", 1)))
        end = min(len(lines), int(arguments.get("end_line", len(lines))))
        content, truncated = self._truncate("".join(lines[start - 1:end]))
        return {
            "path": self._relative(path),
            "content": content,
            "start_line": start,
            "end_line": end,
            "total_lines": len(lines),
            "truncated": truncated,
        }

    def _tool_write_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self._path(arguments["path"], "write", may_not_exist=True)
        content = arguments["content"]
        if not isinstance(content, str):
            raise TypeError("content must be text")
        previous_mode = path.stat().st_mode if path.exists() else None
        self._atomic_write_text(path, content)
        if previous_mode is not None:
            path.chmod(previous_mode)
        return {
            "path": self._relative(path),
            "bytes": len(content.encode()),
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        }

    def _tool_list_files(self, arguments: dict[str, Any]) -> dict[str, Any]:
        base = self._path(arguments.get("path", "."), "read")
        pattern = str(arguments.get("glob", "*"))
        limit = max(1, min(int(arguments.get("limit", 1000)), 10_000))
        iterator = base.rglob("*") if base.is_dir() else iter([base])
        files: list[str] = []
        for path in iterator:
            if not path.is_file() or self._is_denied(path) or not fnmatch.fnmatch(path.name, pattern):
                continue
            files.append(self._relative(path))
            if len(files) >= limit:
                break
        return {"files": sorted(files), "truncated": len(files) >= limit}

    def _tool_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments["query"])
        base = self._path(arguments.get("path", "."), "read")
        limit = max(1, min(int(arguments.get("limit", 200)), 5000))
        if shutil.which("rg"):
            command = ["rg", "--line-number", "--no-heading", "--color=never", "--max-count", str(limit)]
            if not arguments.get("case_sensitive", False):
                command.append("--ignore-case")
            if not arguments.get("regex", False):
                command.append("--fixed-strings")
            if arguments.get("glob"):
                command += ["--glob", str(arguments["glob"])]
            for denied in self._expanded_denied_globs():
                command += ["--iglob", f"!{denied}"]
            command += ["-e", query, "--", str(base)]
            result = self._run_process(command, allowed_nonzero={1})
            matches = [line for line in result["stdout"].splitlines() if line][:limit]
            return {**result, "matches": matches, "truncated": result["truncated"] or len(matches) >= limit}
        if arguments.get("regex"):
            raise RuntimeError("regex search requires ripgrep; fallback search accepts fixed text only")
        needle = query if arguments.get("case_sensitive") else query.casefold()
        matches: list[str] = []
        output_bytes = 0
        for path in (base.rglob("*") if base.is_dir() else [base]):
            self._check_running()
            if not path.is_file() or self._is_denied(path):
                continue
            if arguments.get("glob") and not fnmatch.fnmatch(path.name, str(arguments["glob"])):
                continue
            with contextlib.suppress(OSError):
                if path.stat().st_size > self.max_file_bytes:
                    continue
            with contextlib.suppress(UnicodeDecodeError, OSError):
                with path.open("r", encoding="utf-8") as stream:
                    for number, raw_line in enumerate(stream, 1):
                        self._check_running()
                        line = raw_line.rstrip("\r\n")
                        haystack = line if arguments.get("case_sensitive") else line.casefold()
                        if needle not in haystack:
                            continue
                        rendered = f"{self._relative(path)}:{number}:{line}"
                        encoded = rendered.encode()
                        remaining = self.max_output_bytes - output_bytes
                        if len(encoded) > remaining:
                            if remaining > 0:
                                matches.append(encoded[:remaining].decode(errors="replace"))
                            return {"matches": matches, "truncated": True}
                        matches.append(rendered)
                        output_bytes += len(encoded)
                        if len(matches) >= limit:
                            return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    def _tool_apply_patch(self, arguments: dict[str, Any]) -> dict[str, Any]:
        patch = str(arguments["patch"])
        self._validate_patch(patch)
        self._run_process(["git", "apply", "--check", "-"], input_bytes=patch.encode())
        return self._run_process(["git", "apply", "--whitespace=nowarn", "-"], input_bytes=patch.encode())

    def _tool_file_info(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self._path(arguments["path"], "read")
        stat = path.stat()
        return {
            "path": self._relative(path),
            "type": "directory" if path.is_dir() else "file",
            "bytes": stat.st_size,
            "modified": stat.st_mtime,
            "mode": oct(stat.st_mode & 0o777),
        }

    def _tool_make_directory(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self._path(arguments["path"], "write", may_not_exist=True)
        path.mkdir(parents=True, exist_ok=True)
        return {"path": self._relative(path)}

    def _tool_shell(self, arguments: dict[str, Any]) -> dict[str, Any]:
        command = str(arguments["command"])
        args = arguments.get("args", [])
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise TypeError("args must be strings")
        self._reject_dangerous_command(command, args)
        return self._run_process([command, *args], timeout=float(arguments.get("timeout", self.timeout)))

    def _tool_run_tests(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if "command" in arguments:
            command = [str(arguments["command"]), *map(str, arguments.get("args", []))]
        elif self.test_command:
            command = list(self.test_command)
        else:
            command = self._detect_test_command()
        self._reject_dangerous_command(command[0], command[1:])
        result = self._run_process(
            command,
            timeout=float(arguments.get("timeout", self.timeout)),
            allowed_nonzero=set(range(1, 256)),
        )
        result["passed"] = result["exit_code"] == 0
        result["command"] = command
        return result

    def _tool_git_status(self, _: dict[str, Any]) -> dict[str, Any]:
        return self._run_process([
            "git", "status", "--porcelain=v1", "--branch", "--",
            *self._git_readable_pathspecs(),
            *self._git_exclusion_pathspecs(),
        ])

    def _tool_git_diff(self, arguments: dict[str, Any]) -> dict[str, Any]:
        command = ["git", "diff"] + (["--cached"] if arguments.get("cached") else [])
        if arguments.get("path"):
            path = self._path(arguments["path"], "read", may_not_exist=True)
            command += [
                "--",
                f":(literal){self._relative(path)}",
                *self._git_exclusion_pathspecs(),
            ]
        else:
            command += [
                "--",
                *self._git_readable_pathspecs(),
                *self._git_exclusion_pathspecs(),
            ]
        return self._run_process(command)

    def _tool_git_log(self, arguments: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(int(arguments.get("limit", 20)), 200))
        return self._run_process(["git", "log", f"-{limit}", "--oneline", "--decorate"])

    def _tool_git_show(self, arguments: dict[str, Any]) -> dict[str, Any]:
        object_name = str(arguments.get("object", "HEAD"))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@{}^~+-]*", object_name):
            raise ValueError("invalid Git object")
        self._run_process(["git", "rev-parse", "--verify", f"{object_name}^{{commit}}"])
        return self._run_process(["git", "show", "--no-patch", "--format=fuller", object_name])

    def _tool_web_get(self, arguments: dict[str, Any]) -> dict[str, Any]:
        response, final = self._open_url(str(arguments["url"]))
        with response:
            data, truncated = self._read_limited(response, self.max_output_bytes)
            content_type = response.headers.get("Content-Type", "")
            encoding = response.headers.get_content_charset() or "utf-8"
        return {
            "url": final,
            "status": getattr(response, "status", 200),
            "content_type": content_type,
            "content": data.decode(encoding, errors="replace"),
            "truncated": truncated,
        }

    def _tool_web_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.web_search_adapter:
            raise RuntimeError("no web_search_adapter configured")
        limit = max(1, min(int(arguments.get("limit", 10)), 50))
        results = self.web_search_adapter(str(arguments["query"]), limit)
        content, truncated = self._truncate(json.dumps(results, ensure_ascii=False, default=str))
        return {"results": json.loads(content) if not truncated else content, "truncated": truncated}

    def _tool_download(self, arguments: dict[str, Any]) -> dict[str, Any]:
        target = self._path(arguments["path"], "write", may_not_exist=True)
        response, final = self._open_url(str(arguments["url"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        with response, tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
            temporary = Path(output.name)
            total = 0
            try:
                while chunk := self._read_network_chunk(response, 65_536):
                    total += len(chunk)
                    if total > self.max_download_bytes:
                        raise ValueError("download exceeds configured size limit")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
                os.replace(temporary, target)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    temporary.unlink()
                raise
        return {"url": final, "path": self._relative(target), "bytes": total}

    def _tool_subagents(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tasks = arguments["tasks"]
        if not isinstance(tasks, list) or not all(isinstance(task, str) and task.strip() for task in tasks):
            raise TypeError("tasks must be non-empty strings")
        if len(tasks) > 32:
            raise ValueError("at most 32 subagent tasks are allowed")
        extra = str(arguments.get("instructions", "Independent analysis only; do not edit files."))

        def run_one(index_task: tuple[int, str]) -> tuple[int, dict[str, Any]]:
            index, task = index_task
            child: LLMTaskHarness | None = None
            try:
                self._check_running()
                child_timeout = self._remaining_overall_timeout()
                child = LLMTaskHarness(
                    self.adapter,
                    self.root,
                    model=self.model,
                    instructions=self.instructions + "\n" + extra,
                    permission_profile="read-only",
                    max_steps=max(2, min(self.max_steps, 12)),
                    timeout=self.timeout,
                    overall_timeout=child_timeout,
                    allow_shell=False,
                    allow_network=self.allow_network,
                    allowed_hosts=self.allowed_hosts,
                    readable_paths=tuple(str(path.relative_to(self.root) or ".") for path in self.readable_roots),
                    writable_paths=(),
                    denied_globs=self.denied_globs,
                    max_output_bytes=self.max_output_bytes,
                    max_file_bytes=self.max_file_bytes,
                    max_context_bytes=self.max_context_bytes,
                    subagent_limit=1,
                    subagent_tools=(),
                    redact=self.redactions,
                )
                with self._lock:
                    self._children.add(child)
                    cancelled = self.cancelled
                if cancelled:
                    child.cancel()
                child._tools = {
                    name: definition
                    for name, definition in child._tools.items()
                    if name in self.subagent_tools
                }
                return index, {"ok": True, "content": child.run(task)}
            except Exception as error:
                return index, {"ok": False, "error": str(error)}
            finally:
                if child is not None:
                    child.close()
                    with self._lock:
                        self._children.discard(child)

        results: list[dict[str, Any] | None] = [None] * len(tasks)
        adapter_cancel = getattr(self.adapter, "cancel", None)
        adapter_cancel_request = getattr(self.adapter, "cancel_request", None)
        safe_workers = (
            1
            if callable(adapter_cancel) and not callable(adapter_cancel_request)
            else min(self.subagent_limit, len(tasks))
        )
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=safe_workers,
            thread_name_prefix="llm-task-subagent",
        ) as pool:
            futures = [pool.submit(run_one, pair) for pair in enumerate(tasks)]
            for future in concurrent.futures.as_completed(futures):
                index, result = future.result()
                results[index] = result
                self._check_running()
        return {"results": results}

    def _tool_update_plan(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw = arguments.get("plan")
        if not isinstance(raw, list):
            raise TypeError("plan must be a list")
        plan: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, Mapping) or not str(item.get("step") or "").strip():
                raise ValueError("every plan item requires a step")
            status = str(item.get("status") or "pending")
            if status not in {"pending", "in_progress", "completed"}:
                raise ValueError(f"invalid plan status: {status}")
            plan.append({"step": str(item["step"]), "status": status})
        if sum(item["status"] == "in_progress" for item in plan) > 1:
            raise ValueError("at most one plan item may be in progress")
        self.plan = plan
        self._emit("plan.updated", plan=plan, explanation=str(arguments.get("explanation") or ""))
        return {"plan": plan}

    def _tool_request_user_input(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.input_handler is None:
            raise RuntimeError("no user-input handler is configured")
        prompt = str(arguments["prompt"]).strip()
        if not prompt:
            raise ValueError("prompt must not be empty")
        response = self.input_handler(prompt, dict(arguments.get("context") or {}))
        return {"response": str(response)}

    def _invoke_adapter(self, request: dict[str, Any]) -> dict[str, Any]:
        result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
        cancellation = threading.Event()
        adapter_request = {**request, "cancellation_event": cancellation}

        def invoke() -> None:
            try:
                result_queue.put((True, self.adapter(adapter_request)))
            except BaseException as error:
                result_queue.put((False, error))
            finally:
                with self._lock:
                    self._adapter_threads.discard(threading.current_thread())
                    self._adapter_cancel_events.discard(cancellation)

        thread = threading.Thread(target=invoke, daemon=True, name="llm-task-model-call")
        with self._lock:
            self._adapter_threads.add(thread)
            self._adapter_cancel_events.add(cancellation)
        thread.start()
        deadline = time.monotonic() + self.timeout
        stop_reason: str | None = None
        cancellation_requested = False
        while True:
            if self.cancelled:
                stop_reason = "cancelled"
            remaining = deadline - time.monotonic()
            if remaining <= 0 and stop_reason is None:
                stop_reason = "timeout"
            if (
                stop_reason is None
                and self.overall_timeout
                and self._started_at
                and time.monotonic() - self._started_at > self.overall_timeout
            ):
                stop_reason = "overall_timeout"
            if stop_reason is not None:
                if not cancellation_requested:
                    cancellation.set()
                    cancel_request = getattr(self.adapter, "cancel_request", None)
                    if callable(cancel_request):
                        with contextlib.suppress(Exception):
                            cancel_request(cancellation)
                    else:
                        cancel_adapter = getattr(self.adapter, "cancel", None)
                        if callable(cancel_adapter):
                            with contextlib.suppress(Exception):
                                cancel_adapter()
                    cancellation_requested = True
                thread.join(timeout=0.1)
                if thread.is_alive():
                    continue
                if stop_reason == "timeout":
                    raise TimeoutError("model adapter timed out")
                if stop_reason == "overall_timeout":
                    raise TimeoutError("agent exceeded its overall timeout")
                raise RuntimeError("agent run was cancelled")
            try:
                ok, value = result_queue.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            thread.join()
            if self.cancelled:
                raise RuntimeError("agent run was cancelled")
            if time.monotonic() >= deadline:
                raise TimeoutError("model adapter timed out")
            if not ok:
                raise RuntimeError(f"model adapter failed at step {self.step}: {value}") from value
            return self._normalize_reply(value)

    def _normalize_reply(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise TypeError("adapter reply must be a mapping")
        content = raw.get("content", "")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise TypeError("reply.content must be text")
        if len(content.encode()) > self.max_message_bytes:
            raise ValueError("reply content exceeds configured message limit")
        calls = raw.get("tool_calls", []) or []
        if not isinstance(calls, list):
            raise TypeError("reply.tool_calls must be a list")
        normalized: list[dict[str, Any]] = []
        identifiers: set[str] = set()
        for item in calls:
            if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
                raise TypeError("invalid tool call")
            call_id = str(item.get("id") or uuid.uuid4())
            if call_id in identifiers:
                raise ValueError(f"duplicate tool call id: {call_id}")
            identifiers.add(call_id)
            arguments = item.get("arguments", {})
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if not isinstance(arguments, Mapping):
                raise TypeError("tool arguments must be an object")
            normalized.append({
                "id": call_id,
                "name": item["name"],
                "arguments": dict(arguments),
            })
        usage = raw.get("usage")
        return {
            "content": content,
            "tool_calls": normalized,
            "usage": dict(usage) if isinstance(usage, Mapping) else {},
        }

    def _compacted_messages(self) -> list[dict[str, Any]]:
        messages = self._snapshot_messages()
        encoded = json.dumps(messages, ensure_ascii=False, default=str).encode()
        if len(encoded) <= self.max_context_bytes:
            return messages
        anchor = self._run_anchor_index
        mandatory = messages[anchor:anchor + 2]
        if len(mandatory) != 2 or [item.get("role") for item in mandatory] != ["system", "user"]:
            raise RuntimeError("current run is missing mandatory repository context or task")

        turns: list[list[dict[str, Any]]] = []
        index = anchor + 2
        while index < len(messages):
            turn = [messages[index]]
            index += 1
            if turn[0].get("role") == "assistant":
                while index < len(messages) and messages[index].get("role") == "tool":
                    turn.append(messages[index])
                    index += 1
            turns.append(turn)

        def candidate(selected: Sequence[Sequence[dict[str, Any]]]) -> list[dict[str, Any]]:
            kept = [message for turn in selected for message in turn]
            dropped = len(messages) - len(mandatory) - len(kept)
            marker = [{
                "role": "system",
                "content": (
                    f"Context compacted deterministically; {dropped} earlier messages "
                    "remain in the durable transcript."
                ),
            }] if dropped else []
            return [*mandatory, *marker, *kept]

        selected: list[list[dict[str, Any]]] = []
        compacted = candidate(selected)
        if len(json.dumps(compacted, ensure_ascii=False, default=str).encode()) > self.max_context_bytes:
            raise ValueError("mandatory repository context and task exceed the model context budget")
        for turn in reversed(turns):
            proposed = [turn, *selected]
            payload = candidate(proposed)
            if len(json.dumps(payload, ensure_ascii=False, default=str).encode()) > self.max_context_bytes:
                if not selected:
                    raise ValueError("newest complete assistant/tool turn exceeds the model context budget")
                break
            selected = proposed
            compacted = payload
        return compacted

    def _run_process(
        self,
        command: Sequence[str],
        *,
        timeout: float | None = None,
        input_bytes: bytes | None = None,
        allowed_nonzero: set[int] = frozenset(),
    ) -> dict[str, Any]:
        self._check_running()
        environment = {
            key: value for key, value in os.environ.items()
            if key in self.SAFE_ENVIRONMENT_KEYS
        }
        environment.update(self.environment)
        started = time.monotonic()
        process = subprocess.Popen(
            list(command),
            cwd=self.root,
            env=environment,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        with self._lock:
            self._processes.add(process)
        output = bytearray()
        errors = bytearray()
        truncated = {"stdout": False, "stderr": False}

        def drain(stream: Any, target: bytearray, key: str) -> None:
            while chunk := stream.read(65_536):
                remaining = self.max_output_bytes - len(target)
                if remaining > 0:
                    target.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated[key] = True

        stdout_thread = threading.Thread(target=drain, args=(process.stdout, output, "stdout"), daemon=True)
        stderr_thread = threading.Thread(target=drain, args=(process.stderr, errors, "stderr"), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        try:
            if input_bytes is not None and process.stdin is not None:
                process.stdin.write(input_bytes)
                process.stdin.close()
            deadline = time.monotonic() + (timeout or self.timeout)
            while process.poll() is None:
                self._check_running()
                if time.monotonic() >= deadline:
                    self._terminate_process_tree(process)
                    raise TimeoutError(f"command timed out: {command[0]}")
                time.sleep(0.02)
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
        except BaseException:
            if process.poll() is None:
                self._terminate_process_tree(process)
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            raise
        finally:
            with self._lock:
                self._processes.discard(process)
        result = {
            "exit_code": process.returncode,
            "stdout": output.decode(errors="replace"),
            "stderr": errors.decode(errors="replace"),
            "truncated": truncated["stdout"] or truncated["stderr"],
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
        if process.returncode and process.returncode not in allowed_nonzero:
            raise RuntimeError(f"command exited {process.returncode}: {result['stderr'] or result['stdout']}")
        return result

    def _terminate_process_tree(self, process: subprocess.Popen[bytes]) -> None:
        with contextlib.suppress(Exception):
            import psutil

            parent = psutil.Process(process.pid)
            children = parent.children(recursive=True)
            for child in children:
                child.terminate()
            parent.terminate()
            _, alive = psutil.wait_procs([parent, *children], timeout=1)
            for item in alive:
                item.kill()
            return
        with contextlib.suppress(Exception):
            process.terminate()
            process.wait(timeout=1)
            return
        with contextlib.suppress(Exception):
            process.kill()

    def _path(self, value: Any, mode: str, may_not_exist: bool = False) -> Path:
        if not isinstance(value, (str, os.PathLike)):
            raise TypeError("path must be text")
        raw = Path(value)
        candidate = (raw if raw.is_absolute() else self.root / raw).resolve(strict=False)
        scopes = self.readable_roots if mode == "read" else self.writable_roots
        if not any(candidate == scope or scope in candidate.parents for scope in scopes):
            raise PermissionError(f"path is outside permitted {mode} scope: {value}")
        if self._is_denied(candidate):
            raise PermissionError(f"path is denied by policy: {value}")
        if not may_not_exist and not candidate.exists():
            raise FileNotFoundError(candidate)
        return candidate

    def _initial_scope(self, value: str) -> Path:
        path = (self.root / value).resolve(strict=False)
        if path != self.root and self.root not in path.parents:
            raise ValueError(f"scope escapes repository: {value}")
        return path

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() or "."

    def _instruction_applies_to_readable_scope(self, candidate: Path) -> bool:
        """Keep root/ancestor policies and policies inside authorized read scopes."""

        directory = candidate.parent.resolve(strict=False)
        return any(
            directory == scope
            or directory in scope.parents
            or scope in directory.parents
            for scope in self.readable_roots
        )

    def _is_denied(self, path: Path) -> bool:
        with contextlib.suppress(ValueError):
            relative = path.resolve(strict=False).relative_to(self.root).as_posix().casefold()
            return any(
                fnmatch.fnmatchcase(relative, pattern)
                for pattern in self._expanded_denied_globs()
            )
        return True

    def _expanded_denied_globs(self) -> tuple[str, ...]:
        """Return case-folded patterns with ``**/`` also matching repository root."""

        expanded: list[str] = []
        for raw in self.denied_globs:
            pattern = raw.replace("\\", "/").casefold()
            expanded.append(pattern)
            if pattern.startswith("**/"):
                expanded.append(pattern[3:])
        return tuple(dict.fromkeys(expanded))

    def _validate_patch(self, patch: str) -> None:
        if not patch.strip():
            raise ValueError("patch is empty")
        paths: list[str] = []
        patterns = (
            r"^(?:---|\+\+\+)\s+([^\t\n]+)",
            r"^(?:rename from|rename to)\s+(.+)$",
            r"^diff --git\s+a/(.+?)\s+b/(.+)$",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, patch, re.MULTILINE):
                paths.extend(group.strip() for group in match.groups() if group)
        if not paths:
            raise ValueError("patch contains no file paths")
        for name in paths:
            if name == "/dev/null":
                continue
            if '"' in name or "\\" in name:
                raise ValueError("quoted or escaped patch paths are not supported")
            if name.startswith(("a/", "b/")):
                name = name[2:]
            self._path(name, "write", may_not_exist=True)

    @staticmethod
    def _command_basename(command: str) -> str:
        name = Path(command).name.casefold()
        while Path(name).suffix.casefold() in {".exe", ".cmd", ".bat", ".com"}:
            name = Path(name).stem.casefold()
        return name

    def _reject_dangerous_command(self, command: str, args: Sequence[str]) -> None:
        base = self._command_basename(command)
        lowered = [argument.casefold() for argument in args]
        if base in {"rm", "rmdir", "del", "erase", "mkfs", "format", "shutdown", "reboot", "poweroff"}:
            raise PermissionError(f"destructive command is prohibited: {base}")
        if base in {"sh", "bash", "zsh", "fish", "cmd", "powershell", "pwsh"}:
            raise PermissionError(f"implicit shell execution is prohibited: {base}")
        if base in {"python", "python3", "py", "node", "deno", "ruby", "perl"}:
            prohibited = {"-c", "-e", "--eval"}
            if any(argument in prohibited for argument in lowered):
                raise PermissionError(f"inline interpreter execution is prohibited: {base}")
        if base == "git":
            mutating = {"reset", "clean", "checkout", "switch", "rebase", "push", "commit", "restore", "stash", "rm", "mv", "merge"}
            if any(argument in mutating for argument in lowered):
                raise PermissionError("mutating Git commands are prohibited through shell tools")

    def _detect_test_command(self) -> list[str]:
        if (self.root / "pyproject.toml").exists() or (self.root / "pytest.ini").exists():
            return ["python", "-m", "pytest"]
        if (self.root / "package.json").exists():
            return ["npm", "test", "--"]
        if (self.root / "Cargo.toml").exists():
            return ["cargo", "test"]
        if (self.root / "go.mod").exists():
            return ["go", "test", "./..."]
        if (self.root / "Makefile").exists():
            return ["make", "test"]
        raise RuntimeError("could not detect a test command")

    def _git_exclusion_pathspecs(self) -> list[str]:
        return [
            f":(exclude,icase,glob){pattern}"
            for pattern in self._expanded_denied_globs()
            if not pattern.startswith(".git")
        ]

    def _git_readable_pathspecs(self) -> list[str]:
        paths = [self._relative(path) for path in self.readable_roots]
        return ["." if path == "." else f":(top,literal){path}" for path in paths]

    def _open_url(self, url: str) -> tuple[_PinnedResponse, str]:
        current = url
        for _ in range(6):
            self._check_running()
            parsed, addresses = self._validate_url(current)
            host = str(parsed.hostname)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            connection_type = _PinnedHTTPSConnection if parsed.scheme == "https" else _PinnedHTTPConnection
            connection = connection_type(host, addresses[0], port, timeout=self._network_timeout())
            self._track_network_resource(connection)
            connection_timer = self._start_network_deadline_timer(connection)
            target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            try:
                connection.request(
                    "GET",
                    target,
                    headers={"User-Agent": "LLMTaskHarness/1", "Accept": "*/*"},
                )
                response = connection.getresponse()
            except BaseException:
                if connection_timer is not None:
                    connection_timer.cancel()
                self._release_network_resource(connection)
                connection.close()
                self._check_running()
                raise
            if connection_timer is not None:
                connection_timer.cancel()
            if response.status not in {301, 302, 303, 307, 308}:
                pinned = _PinnedResponse(response, connection, self._release_network_resource)
                with self._lock:
                    self._network_resources.discard(connection)
                    self._network_resources.add(pinned)
                    cancelled = self.cancelled
                if cancelled:
                    pinned.close()
                    self._check_running()
                remaining = self._remaining_overall_timeout()
                if remaining is not None:
                    timer = threading.Timer(remaining, pinned.close)
                    timer.daemon = True
                    pinned.attach_deadline_timer(timer)
                return pinned, current
            location = response.headers.get("Location")
            response.close()
            self._release_network_resource(connection)
            connection.close()
            self._check_running()
            if not location:
                raise ValueError("redirect has no Location")
            current = urllib.parse.urljoin(current, location)
        raise ValueError("too many redirects")

    def _track_network_resource(self, resource: Any) -> None:
        with self._lock:
            self._network_resources.add(resource)
            cancelled = self.cancelled
        if cancelled:
            with contextlib.suppress(Exception):
                abort = getattr(resource, "abort", None)
                (abort if callable(abort) else resource.close)()
            self._release_network_resource(resource)
            self._check_running()

    def _release_network_resource(self, resource: Any) -> None:
        with self._lock:
            self._network_resources.discard(resource)

    def _network_timeout(self) -> float:
        remaining = self._remaining_overall_timeout()
        return self.timeout if remaining is None else max(0.001, min(self.timeout, remaining))

    def _start_network_deadline_timer(self, resource: Any) -> threading.Timer | None:
        remaining = self._remaining_overall_timeout()
        if remaining is None:
            return None
        abort = getattr(resource, "abort", None)
        timer = threading.Timer(remaining, abort if callable(abort) else resource.close)
        timer.daemon = True
        timer.start()
        return timer

    def _validate_url(self, url: str) -> tuple[urllib.parse.SplitResult, tuple[str, ...]]:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("only HTTP(S) URLs with hosts are allowed")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("URL credentials are not allowed")
        host = parsed.hostname.lower().rstrip(".")
        if self.allowed_hosts and not any(host == allowed or host.endswith(f".{allowed}") for allowed in self.allowed_hosts):
            raise PermissionError(f"host is not allowed: {host}")
        addresses: list[str] = []
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        for info in self._resolve_host(host, port):
            address = ipaddress.ip_address(info[4][0])
            if (
                not address.is_global
                or address.is_multicast
                or getattr(address, "is_site_local", False)
            ):
                raise PermissionError(f"non-public network address for {host}: {address}")
            addresses.append(str(address))
        if not addresses:
            raise OSError(f"host did not resolve: {host}")
        return parsed, tuple(dict.fromkeys(addresses))

    def _resolve_host(self, host: str, port: int) -> list[tuple[Any, ...]]:
        """Resolve on a bounded daemon worker so cancellation never waits on DNS."""

        results = _DNS_RESOLVER_POOL.submit(host, port)
        deadline = time.monotonic() + self._network_timeout()
        while True:
            self._check_running()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"DNS resolution timed out for {host}")
            try:
                ok, value = results.get(timeout=min(0.05, remaining))
            except queue.Empty:
                continue
            if not ok:
                raise value
            return list(value)

    def _read_network_chunk(self, response: _PinnedResponse, size: int) -> bytes:
        self._check_running()
        response.set_timeout(self._network_timeout())
        try:
            chunk = response.read(size)
        except BaseException:
            self._check_running()
            raise
        self._check_running()
        return chunk

    def _read_limited(self, response: _PinnedResponse, limit: int) -> tuple[bytes, bool]:
        data = bytearray()
        while len(data) <= limit:
            chunk = self._read_network_chunk(response, min(65_536, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data[:limit]), len(data) > limit

    def _approve(self, name: str, arguments: Mapping[str, Any], risk: str, call_id: str) -> None:
        if self.approval is None:
            return
        decision = self.approval(name, arguments, risk, call_id)
        if decision is True or decision == "allow":
            return
        reason = decision[1] if isinstance(decision, (tuple, list)) and len(decision) > 1 else str(decision)
        raise PermissionError(f"approval denied for {name}: {reason}")

    def _record_failure(self, call: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        if result.get("ok"):
            return
        key = json.dumps(
            [call["name"], call["arguments"], result.get("error", {}).get("type")],
            sort_keys=True,
            default=str,
        )
        self._failure_counts[key] = self._failure_counts.get(key, 0) + 1
        if self._failure_counts[key] >= self.repeated_failure_limit:
            raise RuntimeError(f"repeated identical failing tool call: {call['name']}")

    def _append_message(self, message: dict[str, Any]) -> None:
        encoded = json.dumps(message, ensure_ascii=False, default=str).encode()
        if len(encoded) > self.max_message_bytes:
            raise ValueError("message exceeds configured limit")
        with self._lock:
            self.messages.append(message)
        self._persist_many([message])

    def _snapshot_messages(self) -> list[dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self.messages, default=str))

    def _persist_many(self, messages: Iterable[Mapping[str, Any]]) -> None:
        redacted = [self._redact_obj(message) for message in messages]
        if self.transcript_handler is not None:
            for message in redacted:
                self.transcript_handler(message)
        if self.persistence_file is not None:
            self.persistence_file.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, self.persistence_file.open("a", encoding="utf-8") as stream:
                for message in redacted:
                    stream.write(json.dumps(message, ensure_ascii=False, default=str) + "\n")

    def _emit(self, event: str, **data: Any) -> None:
        if self.event_handler is None:
            return
        payload = self._redact_obj({
            "event": event,
            "harness_id": self.id,
            "timestamp": time.time(),
            **data,
        })
        with contextlib.suppress(Exception):
            self.event_handler(payload)

    def _redact_obj(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in self.redactions:
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, Mapping):
            redacted: dict[str, Any] = {}
            for key, item in value.items():
                if any(token in str(key).casefold() for token in ("secret", "token", "password", "authorization", "api_key")):
                    redacted[str(key)] = "[REDACTED]"
                else:
                    redacted[str(key)] = self._redact_obj(item)
            return redacted
        if isinstance(value, list):
            return [self._redact_obj(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._redact_obj(item) for item in value)
        return value

    def _truncate(self, text: str) -> tuple[str, bool]:
        raw = text.encode()
        clipped = raw[: self.max_output_bytes]
        return clipped.decode(errors="replace"), len(raw) > len(clipped)

    def _atomic_write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
            raise

    @staticmethod
    def _normalize_message(message: Any) -> dict[str, Any]:
        if not isinstance(message, Mapping) or message.get("role") not in {"system", "user", "assistant", "tool"}:
            raise ValueError("invalid message")
        return dict(message)

    @staticmethod
    def _error(tool: str, kind: str, message: str) -> dict[str, Any]:
        return {"ok": False, "tool": tool, "error": {"type": kind, "message": message}}

    def _check_running(self) -> None:
        self._ensure_open()
        if self.cancelled:
            raise RuntimeError("agent run was cancelled")
        self._remaining_overall_timeout()

    def _remaining_overall_timeout(self) -> float | None:
        if self.overall_timeout is None or self._started_at is None:
            return None
        remaining = self.overall_timeout - (time.monotonic() - self._started_at)
        if remaining <= 0:
            raise TimeoutError("agent exceeded its overall timeout")
        return remaining

    def _ensure_open(self) -> None:
        if self.closed:
            raise RuntimeError("harness is closed")


class HarnessTaskManager:
    """Durable asynchronous task orchestration for :class:`LLMTaskHarness`."""

    TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})
    PERMISSION_RANK = {"read-only": 0, "workspace-write": 1, "full-access": 2}

    def __init__(
        self,
        repository_root: str | os.PathLike[str],
        settings: Mapping[str, Any],
        *,
        adapter_factory: Callable[[Mapping[str, Any]], Adapter] | None = None,
        state_directory: str | os.PathLike[str] = "runtime/coplex_stdpy",
    ) -> None:
        self.root = Path(repository_root).resolve(strict=True)
        self.settings = dict(settings)
        self.state_directory = (self.root / state_directory).resolve(strict=False)
        if self.state_directory != self.root and self.root not in self.state_directory.parents:
            raise ValueError("state directory escapes repository")
        self.tasks_directory = self.state_directory / "tasks"
        self.tasks_directory.mkdir(parents=True, exist_ok=True)
        self.adapter_factory = adapter_factory or self._default_adapter
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._records: dict[str, dict[str, Any]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._harnesses: dict[str, LLMTaskHarness] = {}
        self._futures: dict[str, concurrent.futures.Future[Any]] = {}
        self._closed = False
        workers = max(1, min(int(self.settings.get("maxWorkers", 2)), 16))
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="llm-task-harness",
        )
        self._load_records()

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._condition.notify_all()
        self.cancel_all()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def update_settings(self, settings: Mapping[str, Any]) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("task manager is closed")
            self.settings.update(dict(settings))

    def submit(self, body: Mapping[str, Any]) -> dict[str, Any]:
        if not bool(self.settings.get("executionEnabled", False)):
            raise PermissionError("task execution is disabled; enable it on the plugin configuration page")
        task = str(body.get("task") or "").strip()
        if not task:
            raise ValueError("task must be non-empty text")
        task_root = self._task_root(str(body.get("root") or "."))
        profile = str(body.get("permissionProfile") or self.settings.get("defaultPermissionProfile") or "workspace-write")
        if profile not in LLMTaskHarness.PROFILE_RISKS:
            raise ValueError(f"unknown permission profile: {profile}")
        maximum_profile = str(
            self.settings.get("maximumPermissionProfile")
            or self.settings.get("defaultPermissionProfile")
            or "workspace-write"
        )
        if maximum_profile not in self.PERMISSION_RANK:
            raise ValueError(f"unknown maximum permission profile: {maximum_profile}")
        if self.PERMISSION_RANK[profile] > self.PERMISSION_RANK[maximum_profile]:
            raise PermissionError(
                f"requested permission profile {profile} exceeds configured maximum {maximum_profile}"
            )
        approval_mode = str(body.get("approvalMode") or self.settings.get("defaultApprovalMode") or "on-request")
        if approval_mode not in {"never", "on-request", "deny"}:
            raise ValueError(f"unknown approval mode: {approval_mode}")
        allow_never = bool(self.settings.get("allowApprovalNever", False))
        if approval_mode == "never" and not allow_never:
            raise PermissionError("approval mode never is disabled by administrator policy")
        identifier = str(uuid.uuid4())
        now = time.time()
        record = {
            "id": identifier,
            "task": task,
            "root": str(task_root.relative_to(self.root) or "."),
            "model": str(body.get("model") or self.settings.get("defaultModel") or "default"),
            "permissionProfile": profile,
            "approvalMode": approval_mode,
            "status": "queued",
            "createdAt": now,
            "updatedAt": now,
            "startedAt": None,
            "finishedAt": None,
            "answer": "",
            "error": "",
            "cancelRequested": False,
            "approvals": {},
            "pendingInput": None,
            "inputResponse": None,
            "options": dict(body.get("options") or {}),
        }
        with self._lock:
            if self._closed:
                raise RuntimeError("task manager is closed")
            self._records[identifier] = record
            self._events[identifier] = []
            self._persist_record(record)
            self._append_event(identifier, "task.queued", status="queued")
            self._futures[identifier] = self._executor.submit(self._run_record, identifier)
        return self.get(identifier)

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            records = sorted(self._records.values(), key=lambda item: item["createdAt"], reverse=True)
            return [self._public(record) for record in records[: max(1, min(limit, 1000))]]

    def get(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._records.get(task_id)
            if record is None:
                raise KeyError(task_id)
            return self._public(record)

    def events(self, task_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            if task_id not in self._records:
                raise KeyError(task_id)
            return [dict(event) for event in self._events.get(task_id, []) if int(event["sequence"]) > after]

    def cancel(self, task_id: str) -> dict[str, Any]:
        harness: LLMTaskHarness | None = None
        with self._condition:
            record = self._records.get(task_id)
            if record is None:
                raise KeyError(task_id)
            if record["status"] in self.TERMINAL:
                return self._public(record)
            record["cancelRequested"] = True
            record["updatedAt"] = time.time()
            for approval in record.get("approvals", {}).values():
                if isinstance(approval, dict) and approval.get("status") == "pending":
                    approval["status"] = "cancelled"
                    approval["resolvedAt"] = time.time()
            record["pendingInput"] = None
            record["inputResponse"] = None
            future = self._futures.get(task_id)
            if future is not None and future.cancel():
                record["status"] = "cancelled"
                record["finishedAt"] = time.time()
            harness = self._harnesses.get(task_id)
            self._append_event(task_id, "task.cancel_requested", status=record["status"])
            self._persist_record(record)
            self._condition.notify_all()
        if harness is not None:
            harness.cancel()
        return self.get(task_id)

    def cancel_all(self) -> dict[str, Any]:
        cancelled: list[str] = []
        with self._lock:
            task_ids = [
                task_id
                for task_id, record in self._records.items()
                if record["status"] not in self.TERMINAL
            ]
        for task_id in task_ids:
            if self.get(task_id)["status"] not in self.TERMINAL:
                self.cancel(task_id)
                cancelled.append(task_id)
        return {"cancelled": cancelled, "count": len(cancelled)}

    def decide_approval(self, task_id: str, call_id: str, decision: str) -> dict[str, Any]:
        if decision not in {"allow", "deny"}:
            raise ValueError("decision must be allow or deny")
        with self._condition:
            record = self._records.get(task_id)
            if record is None:
                raise KeyError(task_id)
            if record["status"] in self.TERMINAL or record.get("cancelRequested"):
                raise ValueError("task is no longer accepting approval decisions")
            approval = record["approvals"].get(call_id)
            if not isinstance(approval, dict) or approval.get("status") != "pending":
                raise ValueError("approval is not pending")
            approval["status"] = decision
            approval["resolvedAt"] = time.time()
            record["status"] = "running"
            record["updatedAt"] = time.time()
            self._append_event(task_id, "approval.resolved", call_id=call_id, decision=decision)
            self._persist_record(record)
            self._condition.notify_all()
            return self._public(record)

    def provide_input(self, task_id: str, response: str) -> dict[str, Any]:
        with self._condition:
            record = self._records.get(task_id)
            if record is None:
                raise KeyError(task_id)
            if record["status"] in self.TERMINAL or record.get("cancelRequested"):
                raise ValueError("task is no longer accepting input")
            if record.get("pendingInput") is None:
                raise ValueError("task is not waiting for input")
            record["inputResponse"] = str(response)
            record["updatedAt"] = time.time()
            record["status"] = "running"
            self._append_event(task_id, "input.received")
            self._persist_record(record)
            self._condition.notify_all()
            return self._public(record)

    def capabilities(self) -> dict[str, Any]:
        harness = LLMTaskHarness(lambda _: {"content": "", "tool_calls": []}, self.root)
        try:
            return harness.capabilities() | {
                "taskStates": ["queued", "running", "waiting_approval", "waiting_input", "completed", "failed", "cancelled", "interrupted"],
                "executionEnabled": bool(self.settings.get("executionEnabled", False)),
            }
        finally:
            harness.close()

    def models(self) -> list[dict[str, Any]]:
        adapter = self._default_adapter({"model": self.settings.get("defaultModel") or "default"})
        if not isinstance(adapter, OpenAICompatibleAdapter):
            raise RuntimeError("configured adapter cannot enumerate models")
        return adapter.list_models()

    def _run_record(self, task_id: str) -> None:
        with self._condition:
            record = self._records[task_id]
            if record["cancelRequested"]:
                record["status"] = "cancelled"
                record["finishedAt"] = time.time()
                self._persist_record(record)
                return
            record["status"] = "running"
            record["startedAt"] = time.time()
            record["updatedAt"] = time.time()
            self._append_event(task_id, "task.started", status="running")
            self._persist_record(record)
        harness: LLMTaskHarness | None = None
        try:
            task_root = self._task_root(record["root"])
            redactions = [
                os.environ.get(str(self.settings.get("apiKeyEnv") or "OPENAI_API_KEY"), ""),
            ]
            harness = LLMTaskHarness(
                self.adapter_factory(record),
                task_root,
                model=record["model"],
                permission_profile=record["permissionProfile"],
                max_steps=max(1, min(int(self.settings.get("maxSteps", 50)), 500)),
                timeout=max(1.0, float(self.settings.get("modelTimeoutSeconds", 120))),
                overall_timeout=max(1.0, float(self.settings.get("taskTimeoutSeconds", 1800))),
                allow_shell=record["permissionProfile"] != "read-only",
                allow_network=bool(self.settings.get("allowToolNetwork", False)),
                allowed_hosts=tuple(self.settings.get("allowedHosts") or ()),
                denied_globs=self._task_denied_globs(task_root),
                approval=self._approval_handler(task_id),
                event_handler=lambda event: self._append_event(task_id, str(event.get("event") or "harness.event"), payload=dict(event)),
                input_handler=self._input_handler(task_id),
                transcript_handler=lambda message: self._append_transcript(task_id, message),
                redact=redactions,
            )
            with self._lock:
                self._harnesses[task_id] = harness
            if record["cancelRequested"]:
                harness.cancel()
            answer = harness.run(
                record["task"],
                options=record.get("options") or {},
                extra_context=self._ancestor_instruction_context(task_root),
            )
            with self._condition:
                cancelled = bool(record["cancelRequested"])
                record["answer"] = "" if cancelled else answer
                record["status"] = "cancelled" if cancelled else "completed"
                record["finishedAt"] = time.time()
                record["updatedAt"] = time.time()
                self._append_event(
                    task_id,
                    f"task.{record['status']}",
                    status=record["status"],
                )
                self._persist_record(record)
        except BaseException as error:
            with self._condition:
                record["error"] = str(error)
                record["status"] = "cancelled" if record["cancelRequested"] else "failed"
                record["finishedAt"] = time.time()
                record["updatedAt"] = time.time()
                self._append_event(task_id, f"task.{record['status']}", status=record["status"], error=str(error))
                self._persist_record(record)
        finally:
            if harness is not None:
                harness.close()
            with self._lock:
                self._harnesses.pop(task_id, None)

    def _approval_handler(self, task_id: str) -> Approval:
        def approve(name: str, arguments: Mapping[str, Any], risk: str, call_id: str) -> Any:
            with self._condition:
                record = self._records[task_id]
                mode = record["approvalMode"]
                if risk in {"read", "state", "model"} or mode == "never":
                    return True
                if mode == "deny":
                    return ("deny", "task approval mode denies risky tools")
                record["approvals"][call_id] = {
                    "callId": call_id,
                    "tool": name,
                    "risk": risk,
                    "arguments": self._sanitize(arguments),
                    "status": "pending",
                    "createdAt": time.time(),
                    "resolvedAt": None,
                }
                record["status"] = "waiting_approval"
                record["updatedAt"] = time.time()
                self._append_event(task_id, "approval.requested", call_id=call_id, tool=name, risk=risk)
                self._persist_record(record)
                while record["approvals"][call_id]["status"] == "pending" and not record["cancelRequested"]:
                    remaining = self._remaining_task_seconds(record)
                    if remaining <= 0:
                        record["approvals"][call_id]["status"] = "expired"
                        record["approvals"][call_id]["resolvedAt"] = time.time()
                        self._persist_record(record)
                        raise TimeoutError("task timed out while waiting for approval")
                    self._condition.wait(timeout=min(0.5, remaining))
                if record["cancelRequested"]:
                    return ("deny", "task was cancelled")
                decision = record["approvals"][call_id]["status"]
                record["status"] = "running"
                return True if decision == "allow" else ("deny", "user denied the tool call")
        return approve

    def _input_handler(self, task_id: str) -> InputHandler:
        def request(prompt: str, context: Mapping[str, Any]) -> str:
            with self._condition:
                record = self._records[task_id]
                record["pendingInput"] = {"prompt": prompt, "context": self._sanitize(context), "createdAt": time.time()}
                record["inputResponse"] = None
                record["status"] = "waiting_input"
                record["updatedAt"] = time.time()
                self._append_event(task_id, "input.requested", prompt=prompt)
                self._persist_record(record)
                while record["inputResponse"] is None and not record["cancelRequested"]:
                    remaining = self._remaining_task_seconds(record)
                    if remaining <= 0:
                        record["pendingInput"] = None
                        self._persist_record(record)
                        raise TimeoutError("task timed out while waiting for user input")
                    self._condition.wait(timeout=min(0.5, remaining))
                if record["cancelRequested"]:
                    raise RuntimeError("task was cancelled while waiting for input")
                response = str(record["inputResponse"])
                record["pendingInput"] = None
                record["inputResponse"] = None
                record["status"] = "running"
                self._persist_record(record)
                return response
        return request

    def _remaining_task_seconds(self, record: Mapping[str, Any]) -> float:
        timeout = max(1.0, float(self.settings.get("taskTimeoutSeconds", 1800)))
        started = float(record.get("startedAt") or time.time())
        return started + timeout - time.time()

    def _default_adapter(self, record: Mapping[str, Any]) -> Adapter:
        return OpenAICompatibleAdapter(
            str(self.settings.get("modelBaseUrl") or "http://127.0.0.1:8801/v1"),
            str(record.get("model") or self.settings.get("defaultModel") or "default"),
            api_key_env=str(self.settings.get("apiKeyEnv") or "OPENAI_API_KEY"),
            timeout=max(1.0, float(self.settings.get("modelTimeoutSeconds", 120))),
        )

    def _task_root(self, value: str) -> Path:
        raw = Path(value)
        path = (raw if raw.is_absolute() else self.root / raw).resolve(strict=True)
        if path != self.root and self.root not in path.parents:
            raise PermissionError("task root escapes the repository")
        if not path.is_dir():
            raise NotADirectoryError(path)
        if self._manager_path_is_denied(path):
            raise PermissionError("task root is denied by harness policy")
        if path == self.state_directory or self.state_directory in path.parents:
            raise PermissionError("task root cannot be inside the harness control-plane directory")
        return path

    def _manager_path_is_denied(self, path: Path) -> bool:
        patterns: list[str] = []
        for raw in LLMTaskHarness.DEFAULT_DENIED_GLOBS:
            pattern = raw.replace("\\", "/").casefold()
            patterns.append(pattern)
            if pattern.startswith("**/"):
                patterns.append(pattern[3:])
        current = path
        while current != self.root:
            relative = current.relative_to(self.root).as_posix().casefold()
            if any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns):
                return True
            current = current.parent
        return False

    def _ancestor_instruction_context(self, task_root: Path) -> str | None:
        if task_root == self.root:
            return None
        limit = max(1, int(self.settings.get("maxContextBytes", 200_000)))
        directories: list[Path] = []
        current = task_root.parent
        while current == self.root or self.root in current.parents:
            directories.append(current)
            if current == self.root:
                break
            current = current.parent
        sections: list[str] = []
        total = 0
        for directory in reversed(directories):
            candidate = directory / "AGENTS.md"
            try:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                resolved = candidate.resolve(strict=True)
                if resolved != self.root and self.root not in resolved.parents:
                    continue
                if resolved == self.state_directory or self.state_directory in resolved.parents:
                    continue
                relative = candidate.relative_to(self.root).as_posix()
                header = f"--- {relative} ---\n"
                prospective = total + len(header.encode()) + resolved.stat().st_size
                if prospective > limit:
                    raise ValueError("applicable ancestor instructions exceed the model context budget")
                sections.append(header + resolved.read_text(encoding="utf-8"))
                total = prospective
            except (OSError, UnicodeDecodeError):
                continue
        if not sections:
            return None
        return "Applicable ancestor repository instructions:\n" + "\n\n".join(sections)

    def _task_denied_globs(self, task_root: Path) -> tuple[str, ...]:
        patterns = list(LLMTaskHarness.DEFAULT_DENIED_GLOBS)
        with contextlib.suppress(ValueError):
            relative = self.state_directory.relative_to(task_root).as_posix()
            patterns.extend((relative, f"{relative}/**"))
        return tuple(patterns)

    @staticmethod
    def _validated_task_id(task_id: str) -> str:
        value = str(task_id)
        try:
            canonical = str(uuid.UUID(value))
        except ValueError as error:
            raise ValueError("task id must be a canonical UUID") from error
        if value != canonical:
            raise ValueError("task id must be a canonical UUID")
        return canonical

    def _record_path(self, task_id: str) -> Path:
        return self.tasks_directory / f"{self._validated_task_id(task_id)}.json"

    def _events_path(self, task_id: str) -> Path:
        return self.tasks_directory / f"{self._validated_task_id(task_id)}.events.jsonl"

    def _transcript_path(self, task_id: str) -> Path:
        return self.tasks_directory / f"{self._validated_task_id(task_id)}.transcript.jsonl"

    def _append_transcript(self, task_id: str, message: Mapping[str, Any]) -> None:
        with self._lock, self._transcript_path(task_id).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(self._sanitize(message), ensure_ascii=False, default=str) + "\n")

    def _persist_record(self, record: Mapping[str, Any]) -> None:
        target = self._record_path(str(record["id"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(self._sanitize(record), stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
            raise

    def _append_event(self, task_id: str, event: str, **data: Any) -> None:
        with self._lock:
            events = self._events.setdefault(task_id, [])
            payload = self._sanitize({
                "sequence": len(events) + 1,
                "taskId": task_id,
                "event": event,
                "timestamp": time.time(),
                **data,
            })
            events.append(payload)
            with self._events_path(task_id).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def _load_records(self) -> None:
        for path in sorted(self.tasks_directory.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict) or not record.get("id"):
                continue
            try:
                task_id = self._validated_task_id(str(record["id"]))
            except ValueError:
                continue
            if path.stem != task_id:
                continue
            if record.get("status") not in self.TERMINAL:
                record["status"] = "interrupted"
                record["error"] = "Workbench stopped before this task reached a terminal state"
                record["finishedAt"] = time.time()
                record["updatedAt"] = time.time()
                self._persist_record(record)
            self._records[task_id] = record
            events: list[dict[str, Any]] = []
            event_path = self._events_path(task_id)
            if event_path.is_file():
                for line in event_path.read_text(encoding="utf-8").splitlines():
                    with contextlib.suppress(json.JSONDecodeError):
                        value = json.loads(line)
                        if isinstance(value, dict):
                            events.append(value)
            self._events[task_id] = events

    def _public(self, record: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(record["id"])
        return self._sanitize({
            **dict(record),
            "eventCount": len(self._events.get(task_id, [])),
            "links": {
                "self": f"/coplex_stdpy/tasks/{task_id}",
                "events": f"/coplex_stdpy/tasks/{task_id}/events",
                "cancel": f"/coplex_stdpy/tasks/{task_id}/cancel",
            },
        })

    @staticmethod
    def _sanitize(value: Any) -> Any:
        if isinstance(value, Mapping):
            cleaned: dict[str, Any] = {}
            for key, item in value.items():
                if any(token in str(key).casefold() for token in ("secret", "token", "password", "authorization", "api_key")):
                    cleaned[str(key)] = "[REDACTED]"
                else:
                    cleaned[str(key)] = HarnessTaskManager._sanitize(item)
            return cleaned
        if isinstance(value, list):
            return [HarnessTaskManager._sanitize(item) for item in value]
        if isinstance(value, tuple):
            return [HarnessTaskManager._sanitize(item) for item in value]
        return value


# Compatibility with the class name used in Python_Codex_Harness.md.
CodexHarness = LLMTaskHarness


__all__ = [
    "CodexHarness",
    "HarnessTaskManager",
    "LLMTaskHarness",
    "OpenAICompatibleAdapter",
    "ToolDefinition",
]
