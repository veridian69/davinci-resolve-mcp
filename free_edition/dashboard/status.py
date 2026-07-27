"""Read-only status of the in-process bridge, for the dashboard's diagnostics card.

Upstream's analysis dashboard runs as its own process, outside Resolve. The
free-edition bridge runs *inside* Resolve's console. That asymmetry is why
everything here is read-only: the dashboard can start and stop the networked
transport it launched itself, but a server living in a process it never spawned
and cannot signal is something it can only report on.

So the picture is assembled from two files the bridge leaves in the checkout --
`.inproc/transport.json` and `.inproc/inproc.log` -- plus one live probe. The
probe is the part that earns its keep: a live pid proves a Resolve process
exists, not that the HTTP server thread inside it is still serving. A stale
transport.json plus a Resolve that has since been restarted looks identical to a
healthy bridge until something actually knocks on the door.

Nothing upstream imports this module. `free_edition.dashboard.card.install()`
attaches it at runtime by wrapping `_mcp_status_payload`.
"""

import json
import os
import pathlib
from typing import Any, Dict, List

# Enough log to see the last boot and its traceback, short enough to embed in a
# JSON status payload that is polled.
LOG_TAIL_LINES = 20

# The probe runs inside a dashboard request. A bridge that needs longer than
# this to answer a loopback GET is not "up" in any sense the card should claim.
PROBE_TIMEOUT_SECONDS = 1.5


def _repo_root() -> str:
    """Absolute path of the checkout root.

    `parents[2]` because this file is `free_edition/dashboard/status.py`, two
    packages below the root. Upstream's `analysis_dashboard._repo_root()`
    computes the same directory from `src/`, but borrowing it would import
    upstream back into free_edition -- the coupling this layout exists to
    remove -- and would break the moment the dashboard moves. Tests patch this
    name to point at a temporary checkout.
    """
    return str(pathlib.Path(__file__).resolve().parents[2])


def inproc_status() -> Dict[str, Any]:
    """Describe the in-process bridge for the dashboard's MCP diagnostics panel.

    Returns `{"configured": False}` when there is no `.inproc/transport.json`,
    which is the normal state for a Studio install that never needed the free
    edition at all -- the card renders as "Not configured" rather than as an
    error, because nothing is wrong.
    """
    root = _repo_root()
    state_path = os.path.join(root, ".inproc", "transport.json")
    try:
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return {"configured": False}

    pid = state.get("pid")
    pid_alive = False
    if isinstance(pid, int):
        try:
            os.kill(pid, 0)
            pid_alive = True
        except OSError:
            # ProcessLookupError (no such pid) and PermissionError (someone
            # else's pid, therefore not our bridge) are both OSError.
            pid_alive = False

    reachable = False
    if pid_alive and state.get("url"):
        reachable = probe_reachable(state["url"])

    log_path = os.path.join(root, ".inproc", "inproc.log")
    log_tail: List[str] = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            log_tail = fh.readlines()[-LOG_TAIL_LINES:]
    except OSError:
        pass

    return {
        "configured": True,
        "pid_alive": pid_alive,
        "reachable": reachable,
        "url": state.get("url"),
        "token": state.get("token"),
        "pid": pid,
        "started_at": state.get("started_at"),
        "state_path": state_path,
        "log_path": log_path if log_tail else None,
        "log_tail": log_tail,
    }


def probe_reachable(url: str) -> bool:
    """A short, unauthenticated GET against `/mcp`.

    The bridge's bearer-auth middleware answers 401 to any request without a
    token, so a 401 is the proof we want: something is listening and it is our
    server. This is the same signal `free_edition/inproc/selftest.py` uses to
    show the server thread is serving rather than that a pid happens to exist.

    urllib is imported here rather than at module scope to keep importing this
    module cheap -- the boot imports it while Resolve's UI thread waits.
    """
    import urllib.error
    import urllib.request

    try:
        urllib.request.urlopen(f"{url.rstrip('/')}/mcp",
                               timeout=PROBE_TIMEOUT_SECONDS)
        return True  # unexpected, but still means something answered
    except urllib.error.HTTPError as exc:
        return exc.code == 401
    except Exception:
        return False
