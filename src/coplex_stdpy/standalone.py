"""Standalone runner: serve the LLM Task Harness as its own OS process.

Two entry points:

* ``python -m coplex_stdpy.standalone [host] [port]`` runs the server in the
  foreground.
* :func:`launch` spawns it as a *detached* background process. Idempotent: if
  something is already serving the target port it does nothing.

This process serves the same routes built by :func:`coplex_stdpy.plugin.create_router`.
When embedded in a compatible plugin host (for example the LogicMOO
Workbench), the host's own mount can reach it on its own port so the harness
survives host restarts; run standalone otherwise to drive it directly.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent

DEFAULT_HOST = os.environ.get("COPLEX_STDPY_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("COPLEX_STDPY_HTTP_PORT", "8850"))

# Windows process-creation flags: detach from the parent console and start a
# new process group so the child survives the host exiting.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def _probe_host(host: str) -> str:
    """A connectable address for a bind host (wildcards map to loopback)."""

    if host in ("0.0.0.0", "::", ""):
        return "127.0.0.1"
    return host


def is_listening(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """Return True when a TCP connection to ``host:port`` succeeds."""

    try:
        with socket.create_connection((_probe_host(host), port), timeout=timeout):
            return True
    except OSError:
        return False


def build_app():
    """Build a FastAPI app hosting the plugin's full router."""

    from . import server as _server

    manifest: dict[str, Any] = {}
    # plugin.json is repo-root-only (not shipped inside the installed
    # package): pick it up when running from a source checkout, but a
    # plain "pip install coplex_stdpy" works fine with the {} default.
    for manifest_path in (_HERE / "plugin.json", _HERE.parents[1] / "plugin.json"):
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            break
    manifest["path"] = str(_HERE)

    from fastapi import FastAPI

    app = FastAPI(title="LLM Task Harness (standalone)")
    app.include_router(_server.create_router(manifest))

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "plugin": "coplex_stdpy", "standalone": True}

    return app


def main(argv: list[str] | None = None) -> None:
    """Run the standalone server in the foreground.

    Registers real ``/admin/shutdown`` and ``/admin/restart`` process-control
    hooks (see :func:`coplex_stdpy.server.register_process_control`): this
    process is the one that actually owns the running ``uvicorn.Server``, so
    it is the right (and only automatically wired) place those two routes
    can take real effect.
    """

    import argparse

    import uvicorn

    from . import server as _server

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", nargs="?", default=DEFAULT_HOST)
    parser.add_argument("port", nargs="?", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)

    config = uvicorn.Config(build_app(), host=args.host, port=args.port, log_level="warning")
    uvicorn_server = uvicorn.Server(config)

    def _shutdown() -> None:
        # uvicorn's serve loop polls this flag and then runs the app's
        # lifespan "shutdown" handlers (which closes the HarnessTaskManager)
        # before the process exits -- a graceful stop, not a hard kill.
        uvicorn_server.should_exit = True

    def _restart() -> None:
        def _do_restart() -> None:
            uvicorn_server.should_exit = True
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline and is_listening(args.host, args.port):
                time.sleep(0.1)
            launch(args.host, args.port)

        # Respond to the /admin/restart request immediately; do the actual
        # stop-then-respawn in the background so the HTTP client gets an
        # acknowledgement instead of a connection reset mid-restart.
        #
        # This thread must NOT be a daemon thread. uvicorn_server.run() (in
        # the main thread, below) returns as soon as it observes
        # should_exit=True -- almost immediately after this thread sets it --
        # and once main() returns there are no more statements left to run.
        # A daemon thread gets killed outright at that point, with no
        # guarantee it ever reached the is_listening()/launch() calls above;
        # a plain (non-daemon) thread keeps the whole process alive until it
        # actually finishes spawning the replacement.
        threading.Thread(target=_do_restart, name="coplex_stdpy-restart").start()

    _server.register_process_control(shutdown=_shutdown, restart=_restart)
    uvicorn_server.run()


def launch(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    wait: bool = False,
    timeout: float = 20.0,
) -> subprocess.Popen | None:
    """Spawn the standalone server as a detached background process.

    Idempotent: returns ``None`` immediately if ``host:port`` is already
    serving. Output goes to ``.standalone.log`` beside this file. The child is
    marked with ``COPLEX_STDPY_STANDALONE_CHILD=1`` so its own
    ``create_router`` never tries to launch another copy of itself.
    """

    if is_listening(host, port):
        return None

    env = os.environ.copy()
    env["COPLEX_STDPY_STANDALONE_CHILD"] = "1"

    log_path = _HERE / ".standalone.log"
    log = open(log_path, "ab", buffering=0)  # noqa: SIM115 - handed to the child

    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    # Run as "-m coplex_stdpy.standalone" (not a bare script path) so the
    # child process has proper package context for the relative import in
    # build_app().
    proc = subprocess.Popen(
        [sys.executable, "-m", "coplex_stdpy.standalone", host, str(port)],
        cwd=str(_HERE),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        close_fds=True,
        **kwargs,
    )

    if not wait:
        return proc

    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_listening(host, port):
            return proc
        if proc.poll() is not None:
            raise RuntimeError(
                f"coplex_stdpy standalone exited (code {proc.returncode}) during startup; see {log_path}"
            )
        time.sleep(0.25)
    raise TimeoutError(
        f"coplex_stdpy standalone did not start listening on {host}:{port} within {timeout:.0f}s; see {log_path}"
    )


if __name__ == "__main__":
    main()
