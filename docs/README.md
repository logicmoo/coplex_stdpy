# coplex_stdpy — Design Documentation

This folder is the deep-dive design reference for `coplex_stdpy`: the
**Python** repository task harness plugin (as opposed to its Prolog sibling,
`coplex`). For a quickstart, the HTTP/Python API surface, and the
safety policy summary, see [`../README.md`](../README.md). This folder
explains *how the whole system fits together and why it's built this way*,
with diagrams generated from the actual code paths (see
[`images/generate_diagrams.py`](images/generate_diagrams.py)).

Repository layout note: the installable package lives under
`src/coplex_stdpy/` (`runtime.py`, `server.py`, `standalone.py`,
`static/console.html`); the repository-root `plugin.py` + `plugin.json` are
the Workbench plugin entrypoint/manifest, and `plugin.py` is a thin shim that
adds `src/` to `sys.path` and delegates to `coplex_stdpy.server`. The docs
below mostly say "`plugin.py`" for the HTTP/admin layer as a mental-model
shorthand for that pair; where the exact module matters they name
`coplex_stdpy.server` directly.

## Reading order

| # | Doc | Covers |
|---|-----|--------|
| 1 | [01-architecture.md](01-architecture.md) | The three layers (`runtime.py` core, `server.py` HTTP/admin, `static/console.html` UI), how they're wired together, and the process/thread model. |
| 2 | [02-harness-core.md](02-harness-core.md) | `LLMTaskHarness` internals: construction/configuration surface, the `run()` model/tool loop, repository context assembly, deterministic context compaction, and cancellation/timeouts. |
| 3 | [03-tools-and-permissions.md](03-tools-and-permissions.md) | The built-in tool catalog, the `read`/`write`/`execute`/`network`/`state`/`model` risk taxonomy, permission profiles, the approval gate, and bounded read-only subagents. |
| 4 | [04-security-and-sandboxing.md](04-security-and-sandboxing.md) | The concrete guardrails: path scoping and denied globs, patch/command validation, the SSRF-guarded HTTP stack, redaction, and — explicitly — what these controls are *not*. |
| 5 | [05-task-manager-and-http-api.md](05-task-manager-and-http-api.md) | `HarnessTaskManager`: durable JSON/JSONL task state, the task lifecycle, approvals and human-input pauses, restart recovery, and the full `plugin.py`/`server.py` HTTP/admin surface. |
| 6 | [06-testing-and-validation.md](06-testing-and-validation.md) | What the two pytest suites actually protect against, and the exact commands used to validate a change. |

## One-paragraph mental model

A **harness** (`LLMTaskHarness`) is one repository-scoped, single-active-run
object: it owns a message list, a registry of JSON-schema tools tagged with a
risk category, and a pluggable **adapter** callable that turns a normalized
request into a model reply. Calling `run(task)` seeds the conversation with a
size-budgeted repository context, then loops — ask the adapter for the next
reply, execute any requested tool calls through a permission/approval gate,
append the results, repeat — until the model stops calling tools, an error
occurs, or a limit is hit. Everything above is usable synchronously and
in-process from plain Python (see the README's Python API example). The
sibling `HarnessTaskManager` wraps that synchronous object with durable,
asynchronous task orchestration — JSON records, JSONL events/transcripts,
approvals, human-input pauses, cancellation, and restart recovery — and
`coplex_stdpy.server` exposes *that* manager as a Workbench FastAPI router
(loaded via the repository-root `plugin.py` shim) plus a same-origin task
console (`static/console.html`) and a native admin settings page. None of
this is an OS sandbox; see doc 04 for the actual boundary.

## System at a glance

![System architecture](images/01-system-architecture.png)

Three layers, each documented separately, each replaceable without touching
the others:

1. **Core loop** (`LLMTaskHarness` in `runtime.py`) — a plain Python class
   with no FastAPI/HTTP dependency. Usable standalone, as shown in the
   README's `with LLMTaskHarness(adapter, ...) as agent: agent.run(...)`
   example.
2. **Durable orchestration** (`HarnessTaskManager`, also in `runtime.py`) —
   wraps the core loop with a `ThreadPoolExecutor`, JSON/JSONL persistence
   under `runtime/coplex_stdpy/tasks/<id>/`, and blocking approval/input
   handshakes driven by a `threading.Condition`.
3. **HTTP/admin layer** (`server.py` + `static/console.html`, loaded via the
   repository-root `plugin.py` shim) — the only layer that knows about
   FastAPI, the Workbench plugin manifest, or the browser-facing console. It
   is a thin translation layer over `HarnessTaskManager`'s public methods
   plus a native admin settings descriptor.
