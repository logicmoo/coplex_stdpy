# 06 — Testing and validation

## Test suites

Two pytest modules at the repository's `tests/` root cover this plugin —
notice they live outside `workbench/plugins/coplex_stdpy/`, alongside
the rest of the Workbench's test suite, not inside the plugin directory
itself:

| Suite | Focus | Approx. size |
|---|---|---|
| `tests/test_coplex_stdpy.py` | `LLMTaskHarness` and `HarnessTaskManager` in isolation, driven directly in Python with scripted/mock adapters — no FastAPI, no real network. | ~54 tests |
| `tests/test_coplex_stdpy_plugin.py` | The plugin wired into a real Workbench app instance — manifest contract, HTTP task API, admin descriptor, app-lifespan persistence. | ~12 tests |

### What `test_coplex_stdpy.py` actually protects against

Grouped by the doc each area is described in:

- **Core loop correctness** (doc 02): tool round-trips and repository
  context assembly; context-budget rejection *before* an oversized
  mandatory task/instructions set ever reaches the model; nested
  `AGENTS.md` scoping (ancestor instructions included, sibling/out-of-scope
  instructions excluded); deterministic compaction keeping the mandatory
  pair plus the complete latest turn; cancellation that doesn't block on
  slow startup context building.
- **Path and patch safety** (doc 04): atomic writes preserving file mode
  and rejecting scope escapes; root-level and case-variant secret files
  (`.env`, `.ENV`, etc.) denied; `apply_patch` rejecting a quoted denied
  path; search treating a leading-dash query as literal data rather than
  an `rg`/shell flag; the pure-Python search fallback being fixed-text-only
  and skipping oversized files.
- **Command execution safety** (doc 04): `run_tests` unable to bypass the
  dangerous-command policy just by going through the test-detection path
  instead of `shell`; child process environments excluding secrets and
  respecting output bounds.
- **Git scoping** (doc 03/04): Git inspection tools never exposing denied
  file *content*; the configured read scope always compiled to a literal
  (non-glob-injectable) pathspec.
- **Cancellation semantics** (doc 02): a cancelled harness rejecting late
  mutating tool calls; cancellation arriving *during* a pending approval
  correctly fencing off the tool dispatch that was waiting on it.
- **The guarded HTTP stack** (doc 04): the pinned connection actually using
  the validated address; cancellation closing a blocking response
  in-flight; cancellation *before* dispatch aborting without ever sending
  a request; an abort during connect never sending data; DNS resolution
  itself being cancellable and bounded to a fixed worker count even under
  repeated cancellation; every non-global resolved address being rejected,
  not just the first one.
- **Subagents** (doc 03): a real nested read-only tool loop; sequential
  subagents correctly sharing the parent's absolute deadline (not each
  getting a fresh timeout); parent cancellation reaching and quiescing
  every live subagent.
- **The adapter contract** (doc 02): `OpenAICompatibleAdapter` normalizing
  tool calls and usage correctly; TLS being required outside loopback;
  cancellation closing only the specific request it targets rather than a
  sibling's shared connection, including cancellation that arrives while
  the client is still being constructed.
- **`HarnessTaskManager` orchestration** (doc 05): pausing for approval
  then completing; pausing for human input; a task not being reported
  terminal until a non-cooperative adapter actually finishes; a cooperative
  adapter quiescing before the task is marked terminal; a restarted
  manager marking a previously non-terminal record `interrupted`;
  execution being rejected until explicitly enabled; a closed manager
  rejecting `submit()` without leaving a stray persisted queued record;
  the permission/approval ceiling being enforced; transcripts persisting
  correctly for a nested task root; denied directories being rejected as
  task roots; symlinked ancestor-instruction targets being rejected;
  ancestor-instruction reads being bounded; a task never being able to
  read/write the manager's own control-plane directory; forged record IDs
  being ignored; a task timing out correctly while waiting on a pending
  approval; a harness construction failure becoming a clean terminal
  state; a cancelled task rejecting a late approval decision; nested tasks
  correctly receiving ancestor `AGENTS.md` context; an overall timeout
  actually terminating a still-running child process.

### What `test_coplex_stdpy_plugin.py` actually protects against

- The manifest and packaging contract (`plugin.json` fields, declared
  files) stay internally consistent.
- The plugin is discoverable, loads, and serves real capabilities (not a
  stub) once mounted in a Workbench app.
- The task console route serves the real `static/console.html`, not a
  placeholder.
- The admin descriptor is native (not an iframe/external redirect) and
  documents every setting.
- The task HTTP API is gated (403) until `executionEnabled` is explicitly
  turned on.
- Invalid admin settings are rejected without ever mutating the on-disk
  manifest, including ambiguous boolean/number encodings and an
  inconsistent "network enabled with no allowed hosts" combination.
- `HarnessTaskManager` correctly reopens its durable state across
  simulated app lifespans (start → stop → start).
- A fully enabled task goes through a real, isolated end-to-end HTTP
  lifecycle (submit → poll → complete), not a mocked shortcut.
- Valid admin settings persist to the manifest and hot-update the running
  manager without a restart.
- The plugin's identity persists and restores correctly across a page
  route reload.
- `initialize()` rejects a structurally malformed policy before the
  plugin ever starts serving.

## Running the suites

From the repository root (see also `../README.md`):

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_coplex_stdpy.py tests/test_coplex_stdpy_plugin.py
```

`git diff --check` is run alongside these to catch stray whitespace/
conflict markers, and — because this plugin ships a frontend-adjacent
admin/console surface inside the wider Workbench — a full frontend build
is also part of the validation loop:

```powershell
git diff --check
Push-Location workbench\frontend
npm run build
Pop-Location
```

Plugin **entrypoint** changes (`plugin.py`, `runtime.py`, `__init__.py`)
require a Workbench API process restart to take effect: the Workbench can
discover a newly *enabled* plugin at runtime, but it cannot unload or
replace a router that is already mounted in the current Python process —
re-running the test suites above is what actually exercises a fresh
in-process load of any such change.

## Regenerating the diagrams in this folder

Every PNG under `images/` is generated by a small, dependency-light
(`matplotlib` only) script rather than hand-drawn, so it can be kept in
sync with the code instead of drifting from it:

```powershell
python docs\images\generate_diagrams.py
```

Re-run it whenever the architecture, the `run()` loop, the task lifecycle,
or the permission/approval flow changes in a way that would make an
existing diagram inaccurate.
