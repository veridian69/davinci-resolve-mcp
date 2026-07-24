"""Does the compound server's Resolve connection actually work in-process?

probe_compound_inproc.py proved src.server and its five workflow pieces
IMPORT cleanly inside Resolve's console. Importing is not connecting: the
compound path (analysis_dashboard.py, media_analysis.py) calls
DaVinciResolveScript.scriptapp("Resolve") itself rather than reusing the
granular bridge's already-live handle, so it is a second, independent call
into the shim. This probe makes that call and reports what comes back --
the same thing "Refresh Clips" in the dashboard would get if the dashboard
ran inside this console instead of as an external process.

Run AFTER the normal boot (the shim has to be installed first):

    INPROC_REPO="/path/to/davinci-resolve-mcp"; exec(open(INPROC_REPO+"/resolve_console_boot.py").read())
    exec(open("/path/to/davinci-resolve-mcp/tools/probe_compound_connect.py").read())

Read-only. Calls GetProductName / GetProjectManager / GetCurrentProject /
GetMediaPool and stops there -- nothing is created, changed, or deleted.
"""

import sys
import time


def _line(label, value):
    print("  {:<32} {}".format(label, value))


def main():
    bridge = sys.modules.get("DaVinciResolveScript")
    shim_active = getattr(bridge, "__file__", None) == "<inproc-shim>"
    _line("shim installed", shim_active)
    if not shim_active:
        print()
        print("  Run resolve_console_boot.py first.")
        return

    print("=" * 68)
    print("COMPOUND CONNECTION PROBE")
    print("=" * 68)

    try:
        from src.analysis_dashboard import _connect_resolve_read_only
    except ImportError as exc:
        print()
        print("  cannot import _connect_resolve_read_only:", exc)
        print("  (run probe_compound_inproc.py first if this is unexpected)")
        return

    print()
    print("-- calling _connect_resolve_read_only()")
    started = time.time()
    resolve, error = _connect_resolve_read_only()
    elapsed = time.time() - started

    _line("elapsed", "{:.3f}s".format(elapsed))
    if error:
        _line("error", error)
        print()
        print("  This is the exact failure 'Refresh Clips' would hit if the")
        print("  dashboard ran inside this console. If it names a missing")
        print("  RESOLVE_SCRIPT_API path, that env var is not set inside")
        print("  Resolve's console the way a normal shell sets it for the")
        print("  external bridge -- setup_environment() may need the same")
        print("  in-process treatment the granular shim already got.")
        return

    _line("connected", "yes -- {}".format(resolve))

    pm, pm_error = None, None
    try:
        pm = resolve.GetProjectManager()
    except Exception as exc:
        pm_error = "{}: {}".format(type(exc).__name__, exc)
    if pm_error:
        _line("GetProjectManager", "FAILED -- " + pm_error)
        return
    _line("GetProjectManager", "ok")

    project = None
    try:
        project = pm.GetCurrentProject()
    except Exception as exc:
        _line("GetCurrentProject", "FAILED -- {}: {}".format(type(exc).__name__, exc))
        return
    if project is None:
        _line("GetCurrentProject", "None -- no project open")
        return
    _line("GetCurrentProject", project.GetName())

    try:
        media_pool = project.GetMediaPool()
        root = media_pool.GetRootFolder()
        clip_count = len(root.GetClipList() or [])
        _line("GetMediaPool -> root clips", clip_count)
    except Exception as exc:
        _line("GetMediaPool", "FAILED -- {}: {}".format(type(exc).__name__, exc))
        return

    print()
    print("  This is what /api/resolve/media would see: the dashboard's")
    print("  Overview and Refresh Clips would work if run from here.")

    print()
    print("probe done -- read-only, nothing changed")


main()
