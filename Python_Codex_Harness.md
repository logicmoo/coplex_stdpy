# Feature-Rich Python Codex/Copilot Agent Harness

## Documentation

# Python Codex Harness

`codex_harness.py` implements a provider-neutral coding agent as one public
`CodexHarness` class. It combines a model adapter, instructions, repository
context, normalized tool calling, execution, safety controls, persistence,
events, and bounded parallel analysis subagents.

## Adapter contract

Pass any callable accepting a request dictionary:

```python
def adapter(request):
    # Translate request to the provider SDK/API, then normalize its response.
    return {
        "content": "I need to inspect the file.",
        "tool_calls": [{
            "id": "call_1",
            "name": "read_file",
            "arguments": {"path": "README.md"},
        }],
    }
```

The next adapter request contains the resulting `role: tool` message. Return an
empty `tool_calls` list to finish.

```python
from codex_harness import CodexHarness

with CodexHarness(
    adapter,
    root=".",
    model="your-model",
    allow_shell=True,
    allow_network=False,
) as agent:
    answer = agent.run("Inspect the repository, fix the tests, and verify them.")
    print(answer)
```

## Included tools

- Files: read, atomic write, list, metadata, directory creation
- Repository search using `rg`, with a Python fallback
- Git-style patch application
- Direct process execution without an implicit shell
- Test-command execution and common project detection
- Read-only Git status, diff, log, and show
- HTTP retrieval and downloads with host/IP/redirect/size protections
- Injectable web search
- Ordered, bounded parallel analysis subagents

Network access is disabled by default. Repository paths are canonicalized and
restricted to configured readable/writable scopes. Common destructive commands,
shell interpreters, and mutating Git commands are rejected. An approval callback
can impose a stricter policy for every tool invocation.

Subagents are intentionally single-shot and read-only by default. They may
analyze independent questions concurrently, but the parent agent owns edits,
avoiding conflicting writes.

## Test

```bash
python -m unittest -v test_codex_harness.py
```

The tests are deterministic and require no live model or Internet connection.

## Security boundary

This is an application-level safety layer, not an operating-system sandbox.
For untrusted model output, also run the harness inside a container, VM, or
restricted account with least-privilege filesystem and network permissions.

## Complete implementation: `codex_harness.py`

```python
"""A feature-rich, provider-neutral Codex/Copilot-style agent harness.

The harness is intentionally one public class.  Model vendors are integrated
through a callable adapter rather than subclasses::

    reply = adapter(request_dict)

The adapter receives normalized messages/tool schemas and returns::

    {"content": "...", "tool_calls": [
        {"id": "call_1", "name": "read_file",
         "arguments": {"path": "README.md"}}
    ]}

An empty ``tool_calls`` list ends the turn.  The harness owns repository
context, execution, permissions, safety, persistence and iteration.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import fnmatch
import hashlib
import ipaddress
import json
import os
import platform
import queue
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


class CodexHarness:
    """One self-contained, provider-agnostic coding-agent harness.

    ``adapter`` is a callable accepting a request dictionary and returning a
    normalized reply dictionary.  ``approval`` is an optional callable with
    signature ``approval(tool_name, arguments, risk)`` returning True/False,
    ``"allow"``, or ``("deny", reason)``.  ``event_handler`` receives event
    dictionaries and must not raise.
    """

    DEFAULT_INSTRUCTIONS = """You are a persistent coding agent operating in a repository.
Inspect and search before making assumptions. Preserve unrelated user changes.
Make focused, reviewable edits and prefer patch-based modification. Run relevant
tests and diagnose failures. Never claim a command, edit, or test succeeded
without a successful tool result. Continue until the requested work is fully
implemented and verified. Ask only when a material decision or new authority is
required. Avoid destructive operations. Use subagents only for independent
analysis. At completion, concisely report changes, verification, and limitations."""

    RISK = {
        "read_file": "read", "list_files": "read", "search": "read",
        "file_info": "read", "git_status": "read", "git_diff": "read",
        "git_log": "read", "git_show": "read",
        "write_file": "write", "apply_patch": "write",
        "make_directory": "write", "download": "write_network",
        "shell": "execute", "run_tests": "execute", "web_get": "network",
        "web_search": "network", "subagents": "model",
    }

    def __init__(
            self,
            adapter: Callable[[dict[str, Any]], Mapping[str, Any]],
            root: str | os.PathLike[str] = ".",
            *,
            model: str = "default",
            instructions: str | None = None,
            max_steps: int = 50,
            timeout: float = 120.0,
            overall_timeout: float | None = None,
            allow_shell: bool = True,
            allow_network: bool = False,
            allowed_hosts: Sequence[str] = (),
            readable_paths: Sequence[str] = (".",),
            writable_paths: Sequence[str] = (".",),
            environment: Mapping[str, str] | None = None,
            max_output_bytes: int = 1_000_000,
            max_file_bytes: int = 4_000_000,
            max_download_bytes: int = 10_000_000,
            max_context_bytes: int = 100_000,
            subagent_limit: int = 4,
            subagent_tools: Sequence[str] = (
                    "read_file", "list_files", "search", "file_info",
                    "git_status", "git_diff", "git_log", "git_show", "web_get",
            ),
            approval: Callable[[str, Mapping[str, Any], str], Any] | None = None,
            event_handler: Callable[[Mapping[str, Any]], Any] | None = None,
            web_search_adapter: Callable[[str, int], Any] | None = None,
            persistence_file: str | os.PathLike[str] | None = None,
            redact: Sequence[str] = (),
            test_command: Sequence[str] | None = None,
            repeated_failure_limit: int = 3,
    ) -> None:
        if not callable(adapter):
            raise TypeError("adapter must be callable")
        if max_steps < 1 or timeout <= 0 or subagent_limit < 1:
            raise ValueError("max_steps, timeout, and subagent_limit must be positive")
        self.id = str(uuid.uuid4())
        self.adapter = adapter
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise NotADirectoryError(self.root)
        self.model = model
        self.instructions = instructions or self.DEFAULT_INSTRUCTIONS
        self.max_steps = max_steps
        self.timeout = timeout
        self.overall_timeout = overall_timeout
        self.allow_shell = allow_shell
        self.allow_network = allow_network
        self.allowed_hosts = tuple(h.lower().rstrip(".") for h in allowed_hosts)
        self.readable_roots = tuple(self._initial_scope(p) for p in readable_paths)
        self.writable_roots = tuple(self._initial_scope(p) for p in writable_paths)
        self.environment = {str(k): str(v) for k, v in (environment or {}).items()}
        self.max_output_bytes = max_output_bytes
        self.max_file_bytes = max_file_bytes
        self.max_download_bytes = max_download_bytes
        self.max_context_bytes = max_context_bytes
        self.subagent_limit = subagent_limit
        self.subagent_tools = frozenset(subagent_tools)
        self.approval = approval
        self.event_handler = event_handler
        self.web_search_adapter = web_search_adapter
        self.persistence_file = Path(persistence_file).resolve() if persistence_file else None
        self.redactions = tuple(x for x in redact if x)
        self.test_command = tuple(test_command) if test_command else None
        self.repeated_failure_limit = repeated_failure_limit
        self.messages: list[dict[str, Any]] = []
        self.step = 0
        self.cancelled = False
        self.closed = False
        self.current_task: str | None = None
        self._lock = threading.RLock()
        self._processes: set[subprocess.Popen[bytes]] = set()
        self._failure_counts: dict[str, int] = {}
        self._started_at: float | None = None
        self._emit("harness.created", root=str(self.root), model=self.model)

    def __enter__(self) -> "CodexHarness":
        self._ensure_open()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
            self.cancel()
            self.closed = True
            self._emit("harness.closed")

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            for process in tuple(self._processes):
                with contextlib.suppress(Exception):
                    process.terminate()
            self._emit("run.cancelled")

    def reset(self) -> None:
        self._ensure_open()
        with self._lock:
            self.messages.clear()
            self.step = 0
            self.cancelled = False
            self.current_task = None
            self._failure_counts.clear()
            self._emit("conversation.reset")

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id, "root": str(self.root), "model": self.model,
                "step": self.step, "cancelled": self.cancelled,
                "closed": self.closed, "current_task": self.current_task,
                "message_count": len(self.messages),
            }

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
        with self._lock:
            self.cancelled = False
            self.step = 0
            self.current_task = task
            self._failure_counts.clear()
            self._started_at = time.monotonic()
            context = self.repository_context()
            if extra_context:
                context += "\n\nAdditional context:\n" + extra_context[: self.max_context_bytes]
            self.messages.extend([
                {"role": "system", "content": context},
                {"role": "user", "content": task},
            ])
            self._persist_many(self.messages[-2:])
        self._emit("run.started", task=task)
        try:
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
                    "messages": self._snapshot_messages(),
                    "tools": self.tool_specs(),
                    "options": dict(options or {}),
                    "metadata": {"harness_id": self.id, "step": step},
                }
                self._emit("model.request", step=step)
                started = time.monotonic()
                try:
                    raw_reply = self.adapter(request)
                    reply = self._normalize_reply(raw_reply)
                except Exception as exc:
                    self._emit("model.error", step=step, error=str(exc))
                    raise RuntimeError(f"model adapter failed at step {step}: {exc}") from exc
                self._emit("model.response", step=step,
                           duration_ms=round((time.monotonic() - started) * 1000),
                           tool_calls=len(reply["tool_calls"]))
                assistant = {"role": "assistant", **reply}
                self._append_message(assistant)
                if not reply["tool_calls"]:
                    answer = reply["content"]
                    self._emit("run.finished", answer=answer, steps=step)
                    return answer
                for call in reply["tool_calls"]:
                    self._check_running()
                    result = self.execute_tool(call["name"], call["arguments"])
                    self._record_failure(call, result)
                    self._append_message({
                        "role": "tool", "tool_call_id": call["id"],
                        "name": call["name"], "content": result,
                    })
        finally:
            with self._lock:
                self.current_task = None

    def execute_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self._ensure_open()
        args = dict(arguments or {})
        if name not in self.RISK:
            return self._error(name, "unknown_tool", f"Unknown tool: {name}")
        risk = self.RISK[name]
        try:
            self._approve(name, args, risk)
        except Exception as exc:
            return self._error(name, "permission_error", str(exc))
        started = time.monotonic()
        self._emit("tool.started", tool=name, arguments=args, risk=risk)
        try:
            method = getattr(self, f"_tool_{name}")
            data = method(args)
            result = {"ok": True, "tool": name, **(data or {})}
        except Exception as exc:
            result = self._error(name, exc.__class__.__name__, str(exc))
            if os.environ.get("CODEX_HARNESS_TRACEBACK"):
                result["error"]["traceback"] = traceback.format_exc()
        result["duration_ms"] = round((time.monotonic() - started) * 1000)
        result = self._redact_obj(result)
        self._emit("tool.finished", tool=name, result=result)
        return result

    @classmethod
    def tool_specs(cls) -> list[dict[str, Any]]:
        def spec(name: str, description: str, properties: dict[str, Any], required: Sequence[str] = ()) -> dict[
            str, Any]:
            return {"type": "function", "name": name, "description": description,
                    "parameters": {"type": "object", "properties": properties,
                                   "required": list(required), "additionalProperties": False}}

        string = {"type": "string"};
        boolean = {"type": "boolean"};
        integer = {"type": "integer"}
        strings = {"type": "array", "items": string}
        return [
            spec("read_file", "Read a UTF-8 repository file",
                 {"path": string, "start_line": integer, "end_line": integer}, ["path"]),
            spec("write_file", "Atomically create or replace a UTF-8 repository file",
                 {"path": string, "content": string}, ["path", "content"]),
            spec("list_files", "List repository files", {"path": string, "glob": string, "limit": integer}),
            spec("search", "Search repository text",
                 {"query": string, "path": string, "glob": string, "case_sensitive": boolean, "limit": integer},
                 ["query"]),
            spec("apply_patch", "Apply a unified Git-style patch", {"patch": string}, ["patch"]),
            spec("file_info", "Inspect a repository path", {"path": string}, ["path"]),
            spec("make_directory", "Create a repository directory", {"path": string}, ["path"]),
            spec("shell", "Run a program directly without an implicit shell",
                 {"command": string, "args": strings, "timeout": integer}, ["command"]),
            spec("run_tests", "Run an explicit or automatically detected test command",
                 {"command": string, "args": strings, "timeout": integer}),
            spec("git_status", "Read Git status", {}),
            spec("git_diff", "Read Git diff", {"cached": boolean, "path": string}),
            spec("git_log", "Read recent Git history", {"limit": integer}),
            spec("git_show", "Show a Git object", {"object": string}),
            spec("web_get", "Retrieve a permitted HTTP(S) URL", {"url": string}, ["url"]),
            spec("web_search", "Search the web through the injected search adapter",
                 {"query": string, "limit": integer}, ["query"]),
            spec("download", "Download a permitted URL into the repository", {"url": string, "path": string},
                 ["url", "path"]),
            spec("subagents", "Run independent analysis tasks concurrently", {"tasks": strings, "instructions": string},
                 ["tasks"]),
        ]

    # File and repository tools -------------------------------------------------

    def _tool_read_file(self, a: dict[str, Any]) -> dict[str, Any]:
        path = self._path(a["path"], "read")
        size = path.stat().st_size
        if size > self.max_file_bytes:
            raise ValueError(f"file is {size} bytes; limit is {self.max_file_bytes}")
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        start = max(1, int(a.get("start_line", 1)))
        end = min(len(lines), int(a.get("end_line", len(lines))))
        content, truncated = self._truncate("".join(lines[start - 1:end]))
        return {"path": self._relative(path), "content": content,
                "start_line": start, "end_line": end, "total_lines": len(lines),
                "truncated": truncated}

    def _tool_write_file(self, a: dict[str, Any]) -> dict[str, Any]:
        path = self._path(a["path"], "write", may_not_exist=True)
        content = a["content"]
        if not isinstance(content, str): raise TypeError("content must be text")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(content);
                stream.flush();
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            raise
        return {"path": self._relative(path), "bytes": len(content.encode()),
                "sha256": hashlib.sha256(content.encode()).hexdigest()}

    def _tool_list_files(self, a: dict[str, Any]) -> dict[str, Any]:
        base = self._path(a.get("path", ".."), "read")
        pattern, limit = a.get("glob", "*"), min(int(a.get("limit", 1000)), 10000)
        iterator = base.rglob("*") if base.is_dir() else iter([base])
        files = []
        for p in iterator:
            if ".git" in p.parts or not p.is_file() or not fnmatch.fnmatch(p.name, pattern): continue
            files.append(self._relative(p))
            if len(files) >= limit: break
        return {"files": sorted(files), "truncated": len(files) >= limit}

    def _tool_search(self, a: dict[str, Any]) -> dict[str, Any]:
        query, base = str(a["query"]), self._path(a.get("path", ".."), "read")
        limit = min(int(a.get("limit", 200)), 5000)
        if shutil.which("rg"):
            args = ["rg", "--line-number", "--no-heading", "--color=never", "--max-count", str(limit)]
            if not a.get("case_sensitive", False): args.append("--ignore-case")
            if a.get("glob"): args += ["--glob", str(a["glob"])]
            args += [query, str(base)]
            result = self._run_process(args, allowed_nonzero={1})
            return {**result, "matches": result["stdout"].splitlines()[:limit]}
        flags = 0 if a.get("case_sensitive") else re.IGNORECASE;
        rx = re.compile(query, flags);
        matches = []
        for p in (base.rglob("*") if base.is_dir() else [base]):
            if not p.is_file() or ".git" in p.parts or (
                    a.get("glob") and not fnmatch.fnmatch(p.name, a["glob"])): continue
            with contextlib.suppress(UnicodeDecodeError, OSError):
                for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                    if rx.search(line):
                        matches.append(f"{self._relative(p)}:{n}:{line}")
                        if len(matches) >= limit: return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    def _tool_apply_patch(self, a: dict[str, Any]) -> dict[str, Any]:
        patch = str(a["patch"]);
        self._validate_patch(patch)
        return self._run_process(["git", "apply", "--whitespace=nowarn", "-"], input_bytes=patch.encode())

    def _tool_file_info(self, a: dict[str, Any]) -> dict[str, Any]:
        p = self._path(a["path"], "read");
        s = p.stat()
        return {"path": self._relative(p), "type": "directory" if p.is_dir() else "file",
                "bytes": s.st_size, "modified": s.st_mtime, "mode": oct(s.st_mode & 0o777)}

    def _tool_make_directory(self, a: dict[str, Any]) -> dict[str, Any]:
        p = self._path(a["path"], "write", may_not_exist=True);
        p.mkdir(parents=True, exist_ok=True)
        return {"path": self._relative(p)}

    # Process, tests and Git tools ---------------------------------------------

    def _tool_shell(self, a: dict[str, Any]) -> dict[str, Any]:
        if not self.allow_shell: raise PermissionError("shell execution is disabled")
        command = str(a["command"]);
        args = a.get("args", [])
        if not isinstance(args, list) or not all(isinstance(x, str) for x in args): raise TypeError(
            "args must be strings")
        self._reject_dangerous_command(command, args)
        return self._run_process([command, *args], timeout=float(a.get("timeout", self.timeout)))

    def _tool_run_tests(self, a: dict[str, Any]) -> dict[str, Any]:
        if not self.allow_shell: raise PermissionError("process execution is disabled")
        if "command" in a:
            command = [str(a["command"]), *map(str, a.get("args", []))]
        elif self.test_command:
            command = list(self.test_command)
        else:
            command = self._detect_test_command()
        result = self._run_process(command, timeout=float(a.get("timeout", self.timeout)),
                                   allowed_nonzero=set(range(1, 256)))
        result["passed"] = result["exit_code"] == 0;
        result["command"] = command
        return result

    def _tool_git_status(self, _: dict[str, Any]) -> dict[str, Any]:
        return self._run_process(["git", "status", "--porcelain=v1", "--branch"])

    def _tool_git_diff(self, a: dict[str, Any]) -> dict[str, Any]:
        command = ["git", "diff"] + (["--cached"] if a.get("cached") else [])
        if a.get("path"): command += ["--", str(a["path"])]
        return self._run_process(command)

    def _tool_git_log(self, a: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(int(a.get("limit", 20)), 200))
        return self._run_process(["git", "log", f"-{limit}", "--oneline", "--decorate"])

    def _tool_git_show(self, a: dict[str, Any]) -> dict[str, Any]:
        obj = str(a.get("object", "HEAD"));
        if obj.startswith("-"): raise ValueError("invalid Git object")
        return self._run_process(["git", "show", "--stat", "--oneline", obj])

    # Network tools ------------------------------------------------------------

    def _tool_web_get(self, a: dict[str, Any]) -> dict[str, Any]:
        response, final = self._open_url(str(a["url"]))
        with response:
            data = self._read_limited(response, self.max_output_bytes)
            content_type = response.headers.get("Content-Type", "")
            encoding = response.headers.get_content_charset() or "utf-8"
        return {"url": final, "status": getattr(response, "status", 200), "content_type": content_type,
                "content": data.decode(encoding, errors="replace"), "truncated": len(data) >= self.max_output_bytes}

    def _tool_web_search(self, a: dict[str, Any]) -> dict[str, Any]:
        if not self.allow_network: raise PermissionError("network access is disabled")
        if not self.web_search_adapter: raise RuntimeError("no web_search_adapter configured")
        limit = max(1, min(int(a.get("limit", 10)), 50));
        return {"results": self.web_search_adapter(str(a["query"]), limit)}

    def _tool_download(self, a: dict[str, Any]) -> dict[str, Any]:
        target = self._path(a["path"], "write", may_not_exist=True);
        response, final = self._open_url(str(a["url"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        with response, tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as out:
            temporary = Path(out.name);
            total = 0
            try:
                while chunk := response.read(65536):
                    total += len(chunk)
                    if total > self.max_download_bytes: raise ValueError("download exceeds configured size limit")
                    out.write(chunk)
                out.flush();
                os.fsync(out.fileno());
                os.replace(temporary, target)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    temporary.unlink()
                raise
        return {"url": final, "path": self._relative(target), "bytes": total}

    # Parallel subagents -------------------------------------------------------

    def _tool_subagents(self, a: dict[str, Any]) -> dict[str, Any]:
        tasks = a["tasks"]
        if not isinstance(tasks, list) or not all(isinstance(x, str) and x.strip() for x in tasks): raise TypeError(
            "tasks must be non-empty strings")
        if len(tasks) > 32: raise ValueError("at most 32 subagent tasks are allowed")
        extra = str(a.get("instructions", "Independent analysis only; do not edit files."))

        def one(index_task: tuple[int, str]) -> tuple[int, dict[str, Any]]:
            index, task = index_task
            try:
                request = {"model": self.model, "instructions": self.instructions + "\n" + extra,
                           "messages": [{"role": "system", "content": self.repository_context()},
                                        {"role": "user", "content": task}],
                           "tools": [s for s in self.tool_specs() if s["name"] in self.subagent_tools],
                           "options": {"subagent": True, "read_only": True},
                           "metadata": {"parent": self.id, "index": index}}
                reply = self._normalize_reply(self.adapter(request))
                # Safe default: subagents are single-shot analysts; the parent can act on results.
                return index, {"ok": True, "content": reply["content"], "unexecuted_tool_calls": reply["tool_calls"]}
            except Exception as exc:
                return index, {"ok": False, "error": str(exc)}

        results = [None] * len(tasks)
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.subagent_limit,
                                                   thread_name_prefix="codex-subagent") as pool:
            futures = [pool.submit(one, pair) for pair in enumerate(tasks)]
            for future in concurrent.futures.as_completed(futures):
                index, result = future.result();
                results[index] = result
        return {"results": results}

    # Context, process, path, URL and state helpers ----------------------------

    def repository_context(self) -> str:
        parts = [f"Repository root: {self.root}", f"Platform: {platform.platform()}",
                 f"Python: {platform.python_version()}"]
        for command, label in [(["git", "branch", "--show-current"], "Git branch"),
                               (["git", "status", "--short"], "Git status")]:
            try:
                parts.append(f"{label}:\n{self._run_process(command, timeout=10)['stdout']}")
            except Exception as exc:
                parts.append(f"{label}: unavailable ({exc})")
        agents = []
        for p in [self.root / "AGENTS.md", *self.root.glob("*/AGENTS.md")]:
            if p.is_file():
                with contextlib.suppress(Exception): agents.append(
                    f"--- {self._relative(p)} ---\n{p.read_text(encoding='utf-8')}")
        if agents: parts.append("Repository instructions:\n" + "\n".join(agents))
        try:
            listing = self._tool_list_files({"path": ".", "limit": 300})
            parts.append("Bounded file tree:\n" + "\n".join(listing["files"]))
        except Exception:
            pass
        context = "\n\n".join(parts)
        return context.encode()[:self.max_context_bytes].decode(errors="ignore")

    def save(self, path: str | os.PathLike[str]) -> None:
        target = Path(path);
        payload = {"version": 1, "id": self.id, "messages": self._snapshot_messages(), "model": self.model}
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def restore(self, path: str | os.PathLike[str]) -> None:
        data = json.loads(Path(path).read_text(encoding="utf-8"));
        messages = data.get("messages")
        if data.get("version") != 1 or not isinstance(messages, list): raise ValueError("invalid harness state file")
        with self._lock: self.messages = [self._normalize_message(x) for x in messages]

    def _run_process(self, command: Sequence[str], *, timeout: float | None = None,
                     input_bytes: bytes | None = None, allowed_nonzero: set[int] = frozenset()) -> dict[str, Any]:
        self._check_running();
        env = os.environ.copy();
        env.update(self.environment);
        started = time.monotonic()
        process = subprocess.Popen(list(command), cwd=self.root, env=env,
                                   stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with self._lock:
            self._processes.add(process)
        try:
            out, err = process.communicate(input=input_bytes, timeout=timeout or self.timeout)
        except subprocess.TimeoutExpired:
            process.kill();
            out, err = process.communicate();
            raise TimeoutError(f"command timed out: {command[0]}")
        finally:
            with self._lock:
                self._processes.discard(process)
        stdout, ot = self._truncate_bytes(out);
        stderr, et = self._truncate_bytes(err)
        result = {"exit_code": process.returncode, "stdout": stdout, "stderr": stderr, "truncated": ot or et,
                  "duration_ms": round((time.monotonic() - started) * 1000)}
        if process.returncode and process.returncode not in allowed_nonzero: raise RuntimeError(
            f"command exited {process.returncode}: {stderr or stdout}")
        return result

    def _path(self, value: Any, mode: str, may_not_exist: bool = False) -> Path:
        if not isinstance(value, (str, os.PathLike)): raise TypeError("path must be text")
        raw = Path(value)
        candidate = (self.root / raw).resolve(strict=not may_not_exist)
        scopes = self.readable_roots if mode == "read" else self.writable_roots
        if not any(candidate == scope or scope in candidate.parents for scope in scopes):
            raise PermissionError(f"path is outside permitted {mode} scope: {value}")
        return candidate

    def _initial_scope(self, value: str) -> Path:
        p = (self.root / value).resolve(strict=False)
        if p != self.root and self.root not in p.parents: raise ValueError(f"scope escapes repository: {value}")
        return p

    def _relative(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix() or "."

    def _validate_patch(self, patch: str) -> None:
        if not patch.strip(): raise ValueError("patch is empty")
        for match in re.finditer(r"^(?:---|\+\+\+)\s+([^\t\n]+)", patch, re.MULTILINE):
            name = match.group(1).strip()
            if name == "/dev/null": continue
            if name.startswith(("a/", "b/")): name = name[2:]
            self._path(name, "write", may_not_exist=True)

    def _reject_dangerous_command(self, command: str, args: Sequence[str]) -> None:
        base = Path(command).name.lower();
        joined = " ".join(args)
        if base in {"rm", "rmdir", "mkfs", "shutdown", "reboot", "poweroff"}: raise PermissionError(
            f"destructive command is prohibited: {base}")
        if base == "git" and args and args[0] in {"reset", "clean", "checkout", "switch", "rebase", "push",
                                                  "commit"}: raise PermissionError(
            f"mutating Git command is prohibited: git {args[0]}")
        if base in {"sh", "bash", "zsh", "cmd", "powershell", "pwsh"} and joined: raise PermissionError(
            "implicit shell execution is prohibited")

    def _detect_test_command(self) -> list[str]:
        if (self.root / "pyproject.toml").exists() or (self.root / "pytest.ini").exists(): return ["python", "-m",
                                                                                                   "pytest"]
        if (self.root / "package.json").exists(): return ["npm", "test", "--"]
        if (self.root / "Cargo.toml").exists(): return ["cargo", "test"]
        if (self.root / "go.mod").exists(): return ["go", "test", "./..."]
        if (self.root / "Makefile").exists(): return ["make", "test"]
        raise RuntimeError("could not detect a test command")

    def _open_url(self, url: str) -> tuple[Any, str]:
        if not self.allow_network: raise PermissionError("network access is disabled")
        current = url

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args: Any, **kwargs: Any) -> None: return None

        opener = urllib.request.build_opener(NoRedirect)
        for _ in range(6):
            self._validate_url(current)
            try:
                response = opener.open(urllib.request.Request(current, headers={"User-Agent": "CodexHarness/1"}),
                                       timeout=self.timeout); return response, current
            except urllib.error.HTTPError as exc:
                if exc.code not in {301, 302, 303, 307, 308}: raise
                location = exc.headers.get("Location")
                if not location: raise ValueError("redirect has no Location")
                current = urllib.parse.urljoin(current, location)
        raise ValueError("too many redirects")

    def _validate_url(self, url: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname: raise ValueError(
            "only HTTP(S) URLs with hosts are allowed")
        host = parsed.hostname.lower().rstrip(".")
        if self.allowed_hosts and not any(
            host == h or host.endswith("." + h) for h in self.allowed_hosts): raise PermissionError(
            f"host is not allowed: {host}")
        for info in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                       type=socket.SOCK_STREAM):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
                raise PermissionError(f"unsafe network address for {host}: {ip}")

    def _read_limited(self, response: Any, limit: int) -> bytes:
        data = response.read(limit + 1)
        if len(data) > limit: return data[:limit]
        return data

    def _normalize_reply(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, Mapping): raise TypeError("adapter reply must be a mapping")
        content = raw.get("content", "")
        if content is None: content = ""
        if not isinstance(content, str): raise TypeError("reply.content must be text")
        calls = raw.get("tool_calls", []) or []
        if not isinstance(calls, list): raise TypeError("reply.tool_calls must be a list")
        normalized = []
        for item in calls:
            if not isinstance(item, Mapping) or not isinstance(item.get("name"), str): raise TypeError(
                "invalid tool call")
            arguments = item.get("arguments", {})
            if isinstance(arguments, str): arguments = json.loads(arguments)
            if not isinstance(arguments, Mapping): raise TypeError("tool arguments must be an object")
            normalized.append(
                {"id": str(item.get("id") or uuid.uuid4()), "name": item["name"], "arguments": dict(arguments)})
        return {"content": content, "tool_calls": normalized}

    def _normalize_message(self, m: Any) -> dict[str, Any]:
        if not isinstance(m, Mapping) or m.get("role") not in {"system", "user", "assistant", "tool"}: raise ValueError(
            "invalid message")
        return dict(m)

    def _approve(self, name: str, args: Mapping[str, Any], risk: str) -> None:
        if self.approval is None: return
        decision = self.approval(name, args, risk)
        if decision is True or decision == "allow": return
        reason = decision[1] if isinstance(decision, (tuple, list)) and len(decision) > 1 else str(decision)
        raise PermissionError(f"approval denied for {name}: {reason}")

    def _record_failure(self, call: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        if result.get("ok"): return
        key = json.dumps([call["name"], call["arguments"], result.get("error", {}).get("type")], sort_keys=True,
                         default=str)
        self._failure_counts[key] = self._failure_counts.get(key, 0) + 1
        if self._failure_counts[key] >= self.repeated_failure_limit: raise RuntimeError(
            f"repeated identical failing tool call: {call['name']}")

    def _append_message(self, message: dict[str, Any]) -> None:
        with self._lock: self.messages.append(message)
        self._persist_many([message])

    def _snapshot_messages(self) -> list[dict[str, Any]]:
        with self._lock: return json.loads(json.dumps(self.messages, default=str))

    def _persist_many(self, messages: Iterable[Mapping[str, Any]]) -> None:
        if not self.persistence_file: return
        self.persistence_file.parent.mkdir(parents=True, exist_ok=True)
        with self.persistence_file.open("a", encoding="utf-8") as stream:
            for message in messages: stream.write(
                json.dumps(self._redact_obj(message), ensure_ascii=False, default=str) + "\n")

    def _emit(self, event: str, **data: Any) -> None:
        if not self.event_handler: return
        payload = self._redact_obj({"event": event, "harness_id": self.id, "timestamp": time.time(), **data})
        with contextlib.suppress(Exception): self.event_handler(payload)

    def _redact_obj(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in self.redactions: value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict): return {k: self._redact_obj(v) for k, v in value.items()}
        if isinstance(value, list): return [self._redact_obj(v) for v in value]
        if isinstance(value, tuple): return tuple(self._redact_obj(v) for v in value)
        return value

    def _truncate(self, text: str) -> tuple[str, bool]:
        raw = text.encode();
        clipped = raw[:self.max_output_bytes]
        return clipped.decode(errors="replace"), len(raw) > len(clipped)

    def _truncate_bytes(self, data: bytes) -> tuple[str, bool]:
        clipped = data[:self.max_output_bytes]
        return clipped.decode(errors="replace"), len(data) > len(clipped)

    @staticmethod
    def _error(tool: str, kind: str, message: str) -> dict[str, Any]:
        return {"ok": False, "tool": tool, "error": {"type": kind, "message": message}}

    def _check_running(self) -> None:
        self._ensure_open()
        if self.cancelled: raise RuntimeError("agent run was cancelled")
        if self.overall_timeout and self._started_at and time.monotonic() - self._started_at > self.overall_timeout:
            raise TimeoutError("agent exceeded its overall timeout")

    def _ensure_open(self) -> None:
        if self.closed: raise RuntimeError("harness is closed")


if __name__ == "__main__":
    # Deterministic demonstration; replace demo_adapter with any provider SDK.
    replies = iter([
        {"content": "I will inspect the README.",
         "tool_calls": [{"id": "1", "name": "read_file", "arguments": {"path": "README.md"}}]},
        {"content": "Inspection complete.", "tool_calls": []},
    ])


    def demo_adapter(_: dict[str, Any]) -> dict[str, Any]: return next(replies)


    with CodexHarness(demo_adapter) as harness:
        print(harness.run("Inspect the repository README."))
```

## Complete tests: `test_codex_harness.py`

```python
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from codex_harness import CodexHarness


def scripted(*replies):
    lock = threading.Lock()
    items = iter(replies)
    requests = []
    def adapter(request):
        with lock:
            requests.append(request)
            return next(items)
    adapter.requests = requests
    return adapter


def final(text="done"):
    return {"content": text, "tool_calls": []}


def call(name, args=None, call_id="1"):
    return {"content": "working", "tool_calls": [
        {"id": call_id, "name": name, "arguments": args or {}}
    ]}


class CodexHarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / "README.md").write_text("hello world\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_final_response_and_context(self):
        adapter = scripted(final("answer"))
        with CodexHarness(adapter, self.repo) as harness:
            self.assertEqual(harness.run("do it"), "answer")
            self.assertEqual(adapter.requests[0]["messages"][-1]["content"], "do it")

    def test_tool_round_trip(self):
        adapter = scripted(call("read_file", {"path": "README.md"}), final())
        with CodexHarness(adapter, self.repo) as harness:
            harness.run("read")
        tool = adapter.requests[1]["messages"][-1]
        self.assertEqual(tool["role"], "tool")
        self.assertTrue(tool["content"]["ok"])
        self.assertIn("hello world", tool["content"]["content"])

    def test_write_atomic_and_path_escape(self):
        with CodexHarness(scripted(final()), self.repo) as harness:
            result = harness.execute_tool("write_file", {"path": "x/a.txt", "content": "yes"})
            self.assertTrue(result["ok"])
            self.assertEqual((self.repo / "x/a.txt").read_text(), "yes")
            result = harness.execute_tool("write_file", {"path": "../escape.txt", "content": "no"})
            self.assertFalse(result["ok"])

    def test_search_and_output_limit(self):
        with CodexHarness(scripted(final()), self.repo, max_output_bytes=5) as harness:
            result = harness.execute_tool("read_file", {"path": "README.md"})
            self.assertTrue(result["truncated"])
            self.assertEqual(result["content"], "hello")

    def test_approval_and_network_disabled(self):
        deny = lambda name, args, risk: ("deny", "policy")
        with CodexHarness(scripted(final()), self.repo, approval=deny) as harness:
            result = harness.execute_tool("read_file", {"path": "README.md"})
            self.assertFalse(result["ok"])
            self.assertIn("policy", result["error"]["message"])
        with CodexHarness(scripted(final()), self.repo) as harness:
            self.assertFalse(harness.execute_tool("web_get", {"url": "https://example.com"})["ok"])

    def test_shell_and_nonzero_tests(self):
        with CodexHarness(scripted(final()), self.repo) as harness:
            result = harness.execute_tool("shell", {"command": sys.executable, "args": ["-c", "print('ok')"]})
            self.assertTrue(result["ok"])
            self.assertEqual(result["stdout"].strip(), "ok")
            result = harness.execute_tool("run_tests", {"command": sys.executable, "args": ["-c", "raise SystemExit(3)"]})
            self.assertTrue(result["ok"])
            self.assertFalse(result["passed"])
            self.assertEqual(result["exit_code"], 3)

    def test_destructive_shell_rejected(self):
        with CodexHarness(scripted(final()), self.repo) as harness:
            self.assertFalse(harness.execute_tool("shell", {"command": "rm", "args": ["README.md"]})["ok"])

    def test_max_steps(self):
        adapter = lambda request: call("file_info", {"path": "README.md"})
        with CodexHarness(adapter, self.repo, max_steps=2, repeated_failure_limit=9) as harness:
            with self.assertRaisesRegex(RuntimeError, "max_steps"):
                harness.run("loop")

    def test_reset_save_restore(self):
        state = self.repo / "state.json"
        with CodexHarness(scripted(final("one")), self.repo) as harness:
            harness.run("task")
            harness.save(state)
            harness.reset()
            self.assertFalse(harness.messages)
            harness.restore(state)
            self.assertTrue(harness.messages)

    def test_subagents_order_and_partial_failure(self):
        def adapter(request):
            text = request["messages"][-1]["content"]
            if text == "bad": raise ValueError("boom")
            time.sleep(0.02 if text == "first" else 0)
            return final(text.upper())
        with CodexHarness(adapter, self.repo, subagent_limit=2) as harness:
            result = harness.execute_tool("subagents", {"tasks": ["first", "bad", "third"]})
            self.assertEqual([x.get("content") for x in result["results"]], ["FIRST", None, "THIRD"])
            self.assertFalse(result["results"][1]["ok"])

    def test_two_instances(self):
        first = CodexHarness(scripted(final("a")), self.repo)
        second = CodexHarness(scripted(final("b")), self.repo)
        try:
            self.assertNotEqual(first.id, second.id)
            self.assertEqual(first.run("x"), "a")
            self.assertEqual(second.run("y"), "b")
            self.assertEqual(len(first.messages), 3)
            self.assertEqual(len(second.messages), 3)
        finally:
            first.close(); second.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
```
