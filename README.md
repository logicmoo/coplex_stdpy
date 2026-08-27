# coplex_stdpy — LLM Task Harness

`coplex_stdpy` is a provider-neutral repository task runtime built for the
same class of work performed by coding agents: inspect a repository, maintain a
plan, edit files, apply patches, run focused commands and tests, inspect Git,
ask for human input, delegate bounded read-only analysis, and report verified
results.

It is split into two internal layers:

- `runtime.py`, exported through `__init__.py`, is the reusable Python API. It
  provides `LLMTaskHarness`, the compatible `CodexHarness` alias,
  `OpenAICompatibleAdapter`, and `HarnessTaskManager`. This layer has no
  dependency on any particular host application.
- `plugin.py`, `plugin.json`, and `static/console.html` publish a FastAPI HTTP
  task API, a same-origin task console, and durable task/event integration.
  `create_router()` works standalone; the native administration page
  additionally needs a `plugin_admin` module supplied by a compatible plugin
  host (for example the LogicMOO Workbench) and is optional otherwise.

## Installation

```powershell
pip install coplex_stdpy
# or, for the standalone HTTP server:
pip install "coplex_stdpy[server]"
```

To install from a source checkout instead:

```powershell
pip install -e ".[server,test]"
```

## Running standalone

```powershell
python -m coplex_stdpy.standalone [host] [port]
```

This serves the same FastAPI router as `coplex_stdpy.plugin.create_router()`
on its own port (default `127.0.0.1:8850`), so it can be driven directly or
mounted behind a reverse proxy.

For the full design deep-dive (architecture, the model/tool loop, the tool
catalog and permission model, the security boundary, the task manager and
HTTP API, and the test suites) — with diagrams — see [`docs/`](docs/README.md).

The architecture is extensible rather than vendor-locked. New tools register a
JSON schema, handler, and permission risk; provider adapters normalize model
messages and tool calls; the manager isolates concurrent tasks from one
another. The built-in OpenAI-compatible adapter works with the local EMU_LLM
relay at `http://127.0.0.1:8801/v1` by default.

## Safety boundary

Task execution is **disabled by default**. Enable it only after reviewing the
effective policy (`executionEnabled` in the manifest/settings passed to
`create_router()` / `HarnessTaskManager`).

The task console (`/coplex_stdpy/ui`) is the live task console. It submits real
tasks, lists durable state, shows ordered events, exposes pending approvals and
human-input requests, and can cancel active work. It contains no sample task
records or mock model output.

Three permission profiles are available:

| Profile | Built-in access |
| --- | --- |
| `read-only` | repository reads, search, Git inspection, plans, and model/subagent calls |
| `workspace-write` | read-only access plus scoped writes, patches, and approved processes/tests |
| `full-access` | workspace-write plus explicitly enabled, host-filtered network tools |

The default approval mode is `on-request`: write, execution, and network tools
pause the task until the matching call ID is allowed or denied. `deny` rejects
risky tools. `never` means auto-allow within the selected profile, but it is
disabled in the shipped policy until an administrator explicitly enables
`allowApprovalNever`.

The shipped `maximumPermissionProfile` is `workspace-write`, so API callers
cannot select `full-access` until an administrator raises that ceiling. The
default profile must remain at or below the ceiling. Network tools additionally
require both `allowToolNetwork` and a non-empty public-host allowlist.

These controls are application guardrails, **not an operating-system sandbox**.
An approved process or test is full host-code execution authority: it can use
wrappers, custom scripts, Git aliases, or its own libraries to bypass command,
file, and network tool filters. Run untrusted model output inside a container,
VM, restricted account, or equivalent least-privilege
boundary. Provider credentials are used only by the adapter; child tools receive
an allowlisted environment rather than a copy of every host secret.
`allowToolNetwork` governs the harness's HTTP tools; an approved test or process
can still contain its own networking or destructive behavior, which is another
reason execution is opt-in and approval-gated by default.

## Built-in capabilities

- normalized provider loop with model-call and overall task timeouts;
- deterministic context compaction that always retains the complete applicable
  `AGENTS.md` instructions, original task, and whole assistant/tool turns; the
  request fails explicitly when those indivisible inputs cannot fit;
- scoped UTF-8 reads, atomic writes, directory creation, and file metadata;
- literal-by-default repository search using safe `rg -e ... -- ...` argument
  separation, with a Python fallback;
- repository-scoped unified patch validation and `git apply --check`;
- bounded-output direct process execution and test discovery;
- scoped read-only Git status/diff plus log and commit-summary inspection;
- optional guarded HTTP retrieval and downloads using a public-host allowlist,
  one validated DNS resolution, and an IP-pinned connection; remote model
  endpoints require HTTPS while loopback development endpoints may use HTTP;
- dynamic tool registration for MCP, browser, diagnostics, media, or project
  tools supplied by later extensions;
- genuine iterative read-only subagents with isolated messages and bounded
  concurrency;
- plans, human-input pauses, ordered events, cancellation, and process-tree
  termination;
- durable task metadata, transcripts, and restart recovery. A task that was
  running when the Workbench stopped is marked `interrupted` instead of being
  falsely reported as complete.

Cancellation is cooperative at the provider boundary. The bundled
`OpenAICompatibleAdapter` closes its active HTTP client, process tools terminate
their process trees, and child harnesses receive cancellation before a task can
become terminal. A custom adapter receives a request-scoped
`cancellation_event` and should expose `cancel_request(cancellation_event)` when
it owns a cancellable transport. A whole-adapter `cancel()` method remains a
compatibility fallback; adapters with only that global cancellation method run
subagent batches serially so cancelling one request cannot cancel a sibling.
If an adapter ignores cancellation, the task remains nonterminal with
`cancelRequested=true` until that callable actually returns; a non-cooperative
in-process Python callable cannot be safely killed. Use a killable process
boundary for adapters that do not honor this contract.

The official Codex documentation describes the baseline workflow as inspecting
files, making changes, running local tools, choosing permissions, and composing
the agent with scripts or CI. This package implements those primitives as a
provider-neutral service; it does not copy proprietary cloud or IDE
internals. See [Codex CLI](https://learn.chatgpt.com/docs/codex/cli).

## HTTP API

`create_router()` (in `coplex_stdpy.plugin`) builds these routes. Mount it in
your own FastAPI app, or run `python -m coplex_stdpy.standalone` to serve them
directly:

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/coplex_stdpy` | Runtime summary and links |
| `GET` | `/coplex_stdpy/ui` | Same-origin interactive task console |
| `GET` | `/coplex_stdpy/health` | Local readiness and task counts |
| `GET` | `/coplex_stdpy/capabilities` | Tool schemas, risks, profiles, and features |
| `GET` | `/coplex_stdpy/models` | Models from the configured provider |
| `POST` | `/coplex_stdpy/tasks` | Queue an enabled task |
| `GET` | `/coplex_stdpy/tasks` | List durable tasks |
| `GET` | `/coplex_stdpy/tasks/{id}` | Read task state and pending controls |
| `GET` | `/coplex_stdpy/tasks/{id}/events?after=N` | Read ordered events after a cursor |
| `POST` | `/coplex_stdpy/tasks/{id}/cancel` | Request cancellation |
| `POST` | `/coplex_stdpy/tasks/{id}/approvals/{call_id}` | Send `{"decision":"allow"}` or `deny` |
| `POST` | `/coplex_stdpy/tasks/{id}/input` | Send `{"response":"..."}` to a waiting task |

Create a task after enabling execution:

```json
{
  "task": "Inspect the failing tests, make a focused repair, and verify it.",
  "root": ".",
  "model": "yourself/same",
  "permissionProfile": "workspace-write",
  "approvalMode": "on-request",
  "options": {}
}
```

Task states are `queued`, `running`, `waiting_approval`, `waiting_input`,
`completed`, `failed`, `cancelled`, and `interrupted`.

## Python API

Any callable can be a provider adapter when it accepts the normalized request
dictionary and returns content plus normalized tool calls:

```python
from coplex_stdpy import LLMTaskHarness


def adapter(request):
    if request["cancellation_event"].is_set():
        raise RuntimeError("cancelled")
    return {"content": "Inspection complete.", "tool_calls": []}


with LLMTaskHarness(adapter, root=".", permission_profile="read-only") as agent:
    print(agent.run("Inspect this repository."))
```

Register a project-specific tool without changing the core loop:

```python
agent.register_tool(
    "query_symbolic_store",
    "Read a bounded symbolic query result",
    {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
    query_symbolic_store,
    risk="read",
)
```

Durable task state defaults to `runtime/coplex_stdpy`. Deployments and test
runners may set `COPLEX_STDPY_STATE_DIRECTORY` to another path inside the
repository; paths outside the repository are rejected.

## Deliberate exclusions

The first version has no dedicated commit, push, PR-creation, destructive-file,
IDE-control, or browser/computer-use tool. Networking through harness tools is
allowlisted. Approved process execution is intentionally broad, however, and
must be treated as full host authority rather than as an enforceable command
allowlist. Higher-level operations should be added as separately reviewed tools
or integrations with explicit permissions and audit events. This keeps the
capability registry broad enough to grow toward Codex/Copilot-style coverage
without pretending that a Python blocklist is a security sandbox.

## Validation

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[server,dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m build
```

`plugin.py`'s `create_router()` works in this environment without any
extra setup. `initialize()` and `create_admin_router()` additionally need a
`plugin_admin` module supplied by a compatible plugin host and will raise a
clear `RuntimeError` if that host is not present.
