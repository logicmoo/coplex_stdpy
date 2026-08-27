"""coplex_stdpy Workbench plugin entrypoint.

The workbench plugin loader imports this file directly (not as part of an
installed package) and calls ``create_router(manifest)`` /
``initialize(manifest)`` per ``plugin.json``. We add this project's ``src``
directory to ``sys.path`` so the ``coplex_stdpy`` package is importable even
without a prior ``pip install`` -- matching the convention used by the other
Workbench plugins (for example ``emullm``, ``mailbox_chat``) -- and then
delegate to the packaged implementation in :mod:`coplex_stdpy.server`.

Running ``pip install -e .`` from this directory (see ``plugin.json``'s
``plugin-install.install``) is still recommended: it registers proper
distribution metadata and pulls in fastapi/httpx/psutil, but is not required
just to load this file inside the Workbench.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_SOURCE_ROOT = _HERE / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from coplex_stdpy.server import (  # noqa: E402
    PLUGIN_ID,
    create_admin_router,
    create_router,
    initialize,
    resolve_ui_pages,
)

__all__ = [
    "PLUGIN_ID",
    "create_admin_router",
    "create_router",
    "initialize",
    "resolve_ui_pages",
]


def __getattr__(name: str) -> Any:
    # Rarely-used internals (tests, debugging) still reachable as
    # plugin.<name> without re-exporting everything above.
    from coplex_stdpy import server

    return getattr(server, name)
