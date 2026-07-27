"""Start the 34-tool compound MCP server inside DaVinci Resolve. Paste in the console.

    Workspace -> Console -> Py3 dropdown, then:

    INPROC_REPO="/path/to/davinci-resolve-mcp"; exec(open(INPROC_REPO+"/compound_console_boot.py").read())

The granular bridge (resolve_console_boot.py) serves the 341+ one-tool-per-
method surface. This serves the OTHER FastMCP instance in src/server.py --
34 tools, including edit_engine, analysis_store, and strata_story, which plan
and execute cuts from a transcript rather than exposing raw API methods.

tools/probe_compound_inproc.py confirmed src.server imports clean inside the
console (0.1s, 34 tools, all five transcript-to-cuts pieces importable).
tools/probe_compound_connect.py confirmed _connect_resolve_read_only() -- the
same call the compound path makes -- gets a live handle inside the console,
not just an import. Importing src.server here is safe: everything that
touches a port or stdio lives under `if __name__ == "__main__":` at the
bottom of that file, which this boot never executes.

Runs on its own port (8768 by default) so it can boot alongside the granular
bridge (8765) and the dashboard (8766) in the same console session, in any
order -- installs the shim itself if it is not already there. Re-paste the
same line to reload after editing src/server.py or any src/utils module it
imports.

ASCII-only, like every other file the console reads before an encoding fix
can exist.
"""

import os
import sys

REPO = globals().get("INPROC_REPO") or os.environ.get("INPROC_REPO")
_INPROC = os.path.join(REPO, ".inproc") if REPO else ""
DEPS = os.path.join(_INPROC, "deps")
LOG_PATH = os.path.join(
    _INPROC, os.environ.get("INPROC_COMPOUND_LOG_NAME", "compound.log"))
STATE_PATH = os.path.join(
    _INPROC, os.environ.get("INPROC_COMPOUND_STATE_NAME", "compound_transport.json"))
TOKEN_PATH = os.path.join(
    _INPROC, os.environ.get("INPROC_COMPOUND_TOKEN_NAME", "compound_token"))
HOST = os.environ.get("COMPOUND_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("COMPOUND_MCP_PORT", "8768"))

_console_resolve = globals().get("resolve")


class BootFailed(Exception):
    """Aborts the boot. Never SystemExit -- that can take Resolve down with it."""


def _boot():
    if not REPO:
        raise BootFailed(
            "INPROC_REPO is not set. Paste this instead, with your own path:\n"
            '  INPROC_REPO="/path/to/davinci-resolve-mcp"; '
            'exec(open(INPROC_REPO+"/compound_console_boot.py").read())')

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

    # Stop a compound server from an earlier paste before anything else rebinds
    # state -- same shape as the granular boot's own reload story.
    previous = globals().get("__inproc_compound_handle__")
    stopped = None
    if previous is not None:
        stopped = previous.stop()

    for path in (DEPS, REPO):
        while path in sys.path:
            sys.path.remove(path)
    sys.path.insert(0, DEPS)
    sys.path.insert(0, REPO)

    # Purge before importing so edits to src/server.py or the utils it imports
    # are picked up on reload. Also clears any partial "src" import left by a
    # prior failed boot attempt in this same console session.
    doomed = [name for name in list(sys.modules)
              if name == "src" or name.startswith("src.")]
    for name in doomed:
        del sys.modules[name]

    from src.inproc import dispatch, encoding, launcher, shim, streams

    # Encoding first: everything after this reads files.
    encoding_report = encoding.install()

    # Streams second: from here a background traceback can survive. Shares the
    # granular bridge's log-thread-binding fix, on the compound boot's own
    # log file so the two servers' output does not interleave.
    stream_report = streams.install(LOG_PATH, globals())

    print("DaVinci Resolve MCP -- compound in-process boot")
    print(f"  repo    : {REPO}")
    print(f"  log     : {LOG_PATH}")
    if stopped is not None:
        print(f"  previous compound server stopped: {stopped}")
    print(f"  modules purged: {len(doomed)}")
    strays = stream_report["retargeted_handlers"]
    if strays:
        print(f"  stray log handlers retargeted: {len(strays)} -> {strays}")

    # The shim must land before any repo import -- idempotent, so this is safe
    # even if resolve_console_boot.py or dashboard_console_boot.py already
    # installed it earlier in this same console session.
    was_installed = shim.is_installed()
    shim.install(_console_resolve)
    print(f"  shim: {'already installed' if was_installed else 'installed now'}")

    from src import server as compound_server

    wrapped = dispatch.install(compound_server.mcp)

    # Independent token from the granular bridge's -- a client authorized for
    # 343 granular tools should not automatically also reach edit_engine and
    # the rest of the compound surface without a separate, explicit grant.
    token = os.environ.get("COMPOUND_MCP_TOKEN")
    source = "environment"
    if not token and os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, encoding="utf-8") as fh:
            token = fh.read().strip() or None
        source = "file"
    if not token:
        import secrets
        token = secrets.token_urlsafe(24)
        with open(TOKEN_PATH, "w", encoding="utf-8") as fh:
            fh.write(token)
        os.chmod(TOKEN_PATH, 0o600)
        source = "generated, saved"

    handle = launcher.start(compound_server.mcp, token, host=HOST, port=PORT)
    launcher.write_transport_state(handle, STATE_PATH)
    globals()["__inproc_compound_handle__"] = handle

    print()
    print(f"  tools wrapped for threaded dispatch: {wrapped}")
    print(f"  url   : {handle.url}")
    print(f"  token : {token}   ({source})")
    print(f"  state : {STATE_PATH}")
    print()
    print("  re-paste the same exec(...) line to reload after edits")
    return handle


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
