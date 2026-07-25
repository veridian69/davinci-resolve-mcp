"""Start the analysis dashboard inside DaVinci Resolve. Paste one line in the console.

    Workspace -> Console -> Py3 dropdown, then:

    INPROC_REPO="/path/to/davinci-resolve-mcp"; exec(open(INPROC_REPO+"/dashboard_console_boot.py").read())

Separate from resolve_console_boot.py on purpose. The dashboard
(src/analysis_dashboard.py) calls DaVinciResolveScript.scriptapp("Resolve")
itself rather than reusing the granular bridge's handle, so it needs the
same shim installed but does not need the MCP server running -- this boots
the dashboard alone, on its own port, so "Refresh Clips" and Overview can
connect the way tools/probe_compound_connect.py measured that they do.

Run resolve_console_boot.py first if you also want the 341-tool MCP bridge;
either order works, since this installs the shim itself when it is not
already there. Re-paste this same line to reload after editing
src/analysis_dashboard.py or any src/utils module it imports.

ASCII-only, like resolve_console_boot.py: the console's open() defaults to
US-ASCII, and this file is read before any encoding fix can exist.
"""

import os
import sys

REPO = globals().get("INPROC_REPO") or os.environ.get("INPROC_REPO")
_INPROC = os.path.join(REPO, ".inproc") if REPO else ""
DEPS = os.path.join(_INPROC, "deps")
STATE_PATH = os.path.join(
    _INPROC, os.environ.get("INPROC_DASHBOARD_STATE_NAME", "dashboard.json"))
LOG_PATH = os.path.join(
    _INPROC, os.environ.get("INPROC_DASHBOARD_LOG_NAME", "dashboard.log"))

HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
# Different from DAVINCI_MCP_PORT's 8765 default -- the two servers can run
# side by side inside the same console.
PORT = int(os.environ.get("DASHBOARD_PORT", "8766"))
PROJECT_NAME = os.environ.get("DASHBOARD_PROJECT_NAME", "Dashboard Analysis")
PROJECT_ID = os.environ.get("DASHBOARD_PROJECT_ID", "dashboard")
ANALYSIS_ROOT = os.environ.get(
    "DASHBOARD_ANALYSIS_ROOT",
    os.path.expanduser("~/Documents/davinci-resolve-mcp-analysis"))

_console_resolve = globals().get("resolve")


class BootFailed(Exception):
    """Aborts the boot. Never SystemExit -- that can take Resolve down with it."""


def _boot():
    if not REPO:
        raise BootFailed(
            "INPROC_REPO is not set. Paste this instead, with your own path:\n"
            '  INPROC_REPO="/path/to/davinci-resolve-mcp"; '
            'exec(open(INPROC_REPO+"/dashboard_console_boot.py").read())')

    if not os.path.isdir(os.path.join(REPO, "src", "inproc")):
        raise BootFailed(
            f"{REPO} does not look like the checkout: no src/inproc in it")

    if _console_resolve is None:
        raise BootFailed(
            "no `resolve` in the console namespace -- run this from Resolve's "
            "Workspace -> Console with the Py3 dropdown selected")

    if not os.path.isdir(DEPS):
        raise BootFailed(
            f"dependencies missing at {DEPS}\n"
            f"  build them first, from a normal shell:\n"
            f"  python3 {os.path.join(REPO, 'tools', 'setup_inproc.py')}")

    # Stop a dashboard from an earlier paste before anything else rebinds state.
    previous = globals().get("__inproc_dashboard_handle__")
    stopped = None
    if previous is not None:
        try:
            previous["server"].shutdown()
            previous["server"].server_close()
            stopped = not previous["thread"].is_alive()
        except Exception as exc:
            stopped = f"stop failed: {exc}"

    for path in (DEPS, REPO):
        while path in sys.path:
            sys.path.remove(path)
    sys.path.insert(0, DEPS)
    sys.path.insert(0, REPO)

    # Purge before importing so edits to src/analysis_dashboard.py or any
    # src/utils module it imports are picked up on reload.
    doomed = [name for name in list(sys.modules)
              if name == "src" or name.startswith("src.")]
    for name in doomed:
        del sys.modules[name]

    from src.inproc import shim

    # Idempotent: harmless to call even if resolve_console_boot.py already
    # installed the shim in this same console session.
    was_installed = shim.is_installed()
    shim.install(_console_resolve)

    print("DaVinci Resolve MCP -- dashboard in-process boot")
    print(f"  repo    : {REPO}")
    print(f"  log     : {LOG_PATH}")
    if stopped is not None:
        print(f"  previous dashboard stopped: {stopped}")
    print(f"  modules purged: {len(doomed)}")
    print(f"  shim: {'already installed' if was_installed else 'installed now'}")

    import logging
    handler = logging.FileHandler(LOG_PATH)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)

    from src.analysis_dashboard import DashboardState, Handler
    from http.server import ThreadingHTTPServer
    import threading
    import time

    state = DashboardState(PROJECT_NAME, PROJECT_ID, ANALYSIS_ROOT)
    Handler.state = state

    if not port_is_free(HOST, PORT):
        raise BootFailed(
            f"port {PORT} on {HOST} is already bound -- stop the previous "
            f"dashboard, or set DASHBOARD_PORT to a different port")

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    thread = threading.Thread(target=server.serve_forever,
                              name="inproc-dashboard", daemon=True)
    thread.start()
    time.sleep(0.2)
    if not thread.is_alive():
        raise BootFailed("dashboard server thread died during startup")

    globals()["__inproc_dashboard_handle__"] = {"server": server, "thread": thread}

    url = f"http://{HOST}:{PORT}/"
    write_dashboard_state(url, PORT, STATE_PATH)

    print()
    print(f"  url     : {url}")
    print(f"  project : {PROJECT_NAME} ({PROJECT_ID})")
    print(f"  root    : {state.project_root}")
    print(f"  state   : {STATE_PATH}")
    print()
    print("  Open the url above in a normal browser. Overview and Refresh")
    print("  Clips connect to Resolve because this process has the shim.")
    print()
    print("  re-paste the same exec(...) line to reload after edits")
    return url


def port_is_free(host, port):
    import socket
    probe = socket.socket()
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def write_dashboard_state(url, port, state_path):
    import json
    import os as _os
    import time as _time
    payload = {"url": url, "port": port, "pid": _os.getpid(),
               "started_at": _time.time(), "inproc": True}
    try:
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except OSError as exc:
        print(f"  could not write dashboard state: {exc}")


try:
    _boot()
except BootFailed as _exc:
    print()
    print(f"BOOT FAILED: {_exc}")
except Exception:
    import traceback
    print()
    print("BOOT FAILED with an unexpected error:")
    traceback.print_exc()
