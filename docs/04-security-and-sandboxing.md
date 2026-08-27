# 04 — Security model and sandboxing

This doc collects the concrete guardrails implemented in `runtime.py`, and
is explicit about what they are **not**: application-level input validation
and scoping, not an operating-system sandbox. The README states this
directly, and every mechanism below is designed to fail closed (raise) on
anything ambiguous rather than guess permissively.

## Path scoping and denied globs

Every filesystem-touching tool resolves its path through `_path(value,
mode)`:

1. Relative paths are joined against `self.root`; the result is
   `.resolve(strict=False)` (so a not-yet-created write target can still be
   validated) then compared against `readable_roots`/`writable_roots`
   (whichever the requested `mode` needs). A path must be exactly one of
   the configured roots, or have one as an ancestor — anything else raises
   `PermissionError`, including any path that escapes the repository via
   `..` segments, since `resolve()` normalizes those before the scope
   check runs.
2. `_is_denied(path)` case-folds the path relative to `root` and matches it
   against `_expanded_denied_globs()` — the configured `denied_globs`
   (default: `.git`, `**/.git`, `.env`/`.env.*`, `**/.credentials`,
   `*.pem`/`*.key`, `id_rsa`, `id_ed25519`), each pattern additionally
   expanded so a leading `**/` also matches at the repository root, and any
   path that fails to resolve relative to `root` at all is treated as
   denied rather than allowed.
3. For reads, the target must already exist (`FileNotFoundError`
   otherwise); writes accept `may_not_exist=True` so `write_file`/
   `make_directory`/`download` can create new paths.

`readable_paths`/`writable_paths` themselves are validated once at
construction (`_initial_scope`) to reject any configured scope that
escapes the repository root — a misconfigured harness fails at
construction time, not on the first tool call.

## Patch and command validation

- **`apply_patch`** parses every `---`/`+++`, `rename from`/`rename to`,
  and `diff --git a/... b/...` path out of the unified diff text with
  regexes *before* ever invoking `git apply`, rejects quoted or
  backslash-escaped path segments (which `git apply` could otherwise
  interpret differently than the plain string the harness validated), and
  runs every extracted path through the same `_path(..., "write",
  may_not_exist=True)` scope/deny check as any other write. Only after
  every path passes does it run `git apply --check -` (dry run) and then
  the real `git apply --whitespace=nowarn -`, both fed the patch on stdin
  rather than as a shell argument.
- **`shell`/`run_tests`** never invoke a shell interpreter — commands run
  as `Popen([command, *args], ...)` with no `shell=True`, so there is no
  implicit `sh -c`/`cmd /c` parsing of the string at all. On top of that,
  `_reject_dangerous_command()` still blocks:
  - destructive binaries by basename (`rm`, `rmdir`, `del`, `erase`,
    `mkfs`, `format`, `shutdown`, `reboot`, `poweroff`), matched after
    stripping `.exe`/`.cmd`/`.bat`/`.com` extensions so `rm.exe` is caught
    the same as `rm`;
  - shell interpreters themselves (`sh`, `bash`, `zsh`, `fish`, `cmd`,
    `powershell`, `pwsh`) — a model cannot use the `shell` tool to reach an
    actual shell;
  - inline-interpreter escapes (`python -c`, `node -e`, etc.) for common
    language runtimes, which would otherwise bypass every other check
    above by executing arbitrary code inside an "approved" interpreter;
  - mutating Git subcommands (`reset`, `clean`, `checkout`, `switch`,
    `rebase`, `push`, `commit`, `restore`, `stash`, `rm`, `mv`, `merge`)
    when the command is `git` — the harness's own `git_*` tools are the
    only sanctioned way to touch Git state, and even `apply_patch` goes
    through `git apply` directly rather than the `shell` tool.
- **Process execution itself** runs with a minimal, allowlisted
  environment (`SAFE_ENVIRONMENT_KEYS`: `PATH`, `PATHEXT`, `SystemRoot`,
  `WINDIR`, `TEMP`/`TMP`/`TMPDIR`, `LANG`, `LC_ALL`, `TERM`, `ComSpec`,
  `HOME`, `USERPROFILE`) merged with an explicit `environment` mapping —
  child tools never inherit the full host environment (and therefore never
  inherit ambient secrets) unless a value is explicitly listed. Output is
  drained on background threads and truncated at `max_output_bytes` per
  stream while the process is still running (not just after it exits), a
  timeout terminates the whole process tree via `psutil` (with a plain
  `Popen.terminate()`/`.kill()` fallback if `psutil` is unavailable), and
  `cancel()` terminates every tracked process the same way.
- **Git read tools** always pass an explicit pathspec built from
  `readable_roots` (`_git_readable_pathspecs`, `:(top,literal)...`) plus
  the case-insensitive exclusion of every denied glob
  (`_git_exclusion_pathspecs`, `:(exclude,icase,glob)...`) — so `git
  status`/`git diff` can never surface the contents of a denied file even
  though the underlying repository has no concept of the harness's
  read/write scoping.

## The guarded HTTP stack (SSRF protections)

`web_get`, `download`, and (indirectly) redirect-following all go through
`_open_url()` → `_validate_url()`, which is deliberately more conservative
than "check the hostname once":

1. Only `http`/`https` URLs with a hostname are accepted; embedded
   credentials (`user:pass@host`) are rejected outright.
2. If `allowed_hosts` is non-empty, the hostname must exactly match an
   entry or be a subdomain of one.
3. **DNS resolution happens on the bounded daemon resolver pool**
   (`_DaemonResolverPool`, doc 01) rather than inline, specifically so a
   cancelled or timed-out request is never stuck waiting on
   `socket.getaddrinfo`, which Python cannot interrupt.
4. **Every resolved address is checked, not just the first one**: each
   must be `is_global` and not multicast/site-local, or the whole request
   is rejected (`PermissionError`) — this blocks the classic
   DNS-rebinding-to-loopback/private-network SSRF pattern even when the
   hostname itself looked public.
5. The connection is then made to the **already-validated IP address
   directly** (`_PinnedHTTPConnection`/`_PinnedHTTPSConnection`), while
   still sending the original `Host` header (and SNI, for HTTPS) — this
   closes the TOCTOU gap where a second DNS lookup at connect time could
   return a different, unvalidated address than the one just checked.
6. Redirects are followed manually (`_open_url`'s loop, max 6 hops) and
   **every hop re-runs the full validation** — a redirect to a private
   address is rejected exactly like a direct request would be.
7. Every network resource (connection + response) is tracked
   (`_track_network_resource`) so `cancel()` can abort it immediately, and
   a deadline timer tied to `overall_timeout` closes the connection even
   if nothing else is polling it.
8. Reads are bounded: `web_get` truncates at `max_output_bytes`,
   `download` raises once `max_download_bytes` is exceeded mid-stream
   (not just after the fact) and cleans up the partial temp file.
9. `OpenAICompatibleAdapter.validate_base_url()` applies the same spirit
   to the *model* endpoint itself: any non-loopback base URL must use
   HTTPS; `http://` is only accepted for `localhost`/loopback addresses,
   so a misconfigured remote endpoint can't silently send API keys over
   plaintext.

## Redaction

`_redact_obj()` is applied to every tool result, every emitted event, and
everything written to a transcript or persisted event log — not just to
whatever the model eventually sees. It recursively walks dicts/lists/tuples,
replaces the *value* of any key whose name contains `secret`, `token`,
`password`, `authorization`, or `api_key` (case-insensitive) with
`"[REDACTED]"`, and separately substring-replaces every string in
`redact` (the harness's configured secret values, e.g. a live API key)
wherever it appears in text output. `HarnessTaskManager` always includes
the configured provider API key's live value in `redact` when building a
task's harness (doc 05), and applies the same key-name-based sanitization
(`_sanitize`) to every persisted task record and transcript line
independently, so a secret can never reach disk through either path.

## What this is and is not

These are **application guardrails**, not an operating-system sandbox. An
approved `shell`/`run_tests` call is full host-code-execution authority: it
can invoke its own scripts, use language features to escape the specific
checks above, or otherwise act with the same privileges as the Workbench
process itself once it has been approved to run at all. The permission
profile, approval mode, and denied-glob list bound *which categories of
tool calls the model can request and how they're scoped*; they do not
bound what an approved process, once running, chooses to do. Treat any
deployment that runs untrusted model output as needing a container, VM,
restricted account, or equivalent least-privilege boundary around the
whole Workbench process — exactly as the README states. This is also why
the first version deliberately excludes higher-risk primitives entirely
(commit/push/PR-creation, destructive-file tools, IDE control, general
browser/computer-use) rather than trying to safely gate them with the same
mechanisms: a Python-level allowlist is not a substitute for not exposing
the capability at all.
