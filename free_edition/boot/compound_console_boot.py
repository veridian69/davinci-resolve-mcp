"""Start the 34-tool compound MCP server inside DaVinci Resolve. Paste in the console.

    Workspace -> Console -> Py3 dropdown, then:

    INPROC_REPO="/path/to/davinci-resolve-mcp"; exec(open(INPROC_REPO+"/free_edition/boot/compound_console_boot.py").read())

The granular bridge (free_edition/boot/resolve_console_boot.py) serves the 343
one-tool-per-method surface. This serves the OTHER FastMCP instance in
src/server.py -- 34 tools, including edit_engine, analysis_store, and
strata_story, which plan and execute cuts from a transcript rather than
exposing raw API methods.

free_edition/tools/probe_compound_inproc.py confirmed src.server imports clean
inside the console (0.1s, 34 tools, all five transcript-to-cuts pieces
importable). free_edition/tools/probe_compound_connect.py confirmed
_connect_resolve_read_only() -- the same call the compound path makes -- gets a
live handle inside the console, not just an import. Importing src.server here is
safe: everything that touches a port or stdio lives under
`if __name__ == "__main__":` at the bottom of that file, which this boot never
executes.

INPROC_REPO names the CHECKOUT ROOT, not the directory this file sits in, and
everything written at runtime stays at <checkout>/.inproc/. Neither meaning
changed when this script moved under free_edition/boot/; only the paste line did.

Runs on its own port (8768 by default) so it can boot alongside the granular
bridge (8765) and the dashboard (8766) in the same console session, in any
order -- installs the shim itself if it is not already there. Re-paste the
same line to reload after editing src/server.py, any src/utils module it
imports, or anything under free_edition/.

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


def _print_registrations(report):
    """Print what free_edition wired into upstream, and anything it could not.

    The compound path is the one that needs this printed. `src/server.py`
    carries its own byte-identical copy of `_launch_resolve`, and its own
    module-level `detect_capabilities` import (under an alias); both were missed
    by the static edits this replaces, and neither miss raises anything.
    """
    whisperx = report.get("whisperx") or {}
    guarded = report.get("autolaunch") or []
    rebound = whisperx.get("rebound") or []

    print(f"  state dir           : {report.get('state_dir')}")
    print(f"  autolaunch guarded  : {len(guarded)} {guarded}")
    # Say WHY the count is what it is. A bare "0" is ambiguous between the
    # correct case (no captor is loaded in this boot, so a later import binds
    # the wrapper on its own) and the catastrophic one (the rebind broke, and
    # whisperX is silently reported absent). Those must not print the same.
    import free_edition.integrate as _integrate
    _loaded = [n for n in _integrate.DETECT_CAPABILITIES_CAPTORS
               if n in sys.modules]
    if not _loaded:
        _why = "none loaded in this boot; a later import binds the wrapper"
    elif sorted(rebound) == sorted(_loaded):
        _why = f"all {len(_loaded)} loaded captors"
    else:
        _why = f"MISSED {sorted(set(_loaded) - set(rebound))} -- see problems below"
    print(f"  detect_capabilities : rebound in {len(rebound)} ({_why})")
    print(f"  _transcribe patched : {bool(whisperx.get('transcribe'))}")
    for problem in report.get("problems") or []:
        print(f"  PROBLEM: {problem}")


def _boot():
    if not REPO:
        raise BootFailed(
            "INPROC_REPO is not set. Paste this instead, with your own path:\n"
            '  INPROC_REPO="/path/to/davinci-resolve-mcp"; '
            'exec(open(INPROC_REPO+"/free_edition/boot/compound_console_boot.py").read())')

    if not os.path.isdir(os.path.join(REPO, "free_edition", "inproc")):
        raise BootFailed(
            f"{REPO} does not look like the checkout: no free_edition/inproc "
            f"in it (INPROC_REPO is the repo root, not this script's folder)")

    if _console_resolve is None:
        raise BootFailed(
            "no `resolve` in the console namespace -- run this from Resolve's "
            "Workspace -> Console with the Py3 dropdown selected")

    if not os.path.isdir(DEPS):
        raise BootFailed(
            f"dependencies missing at {DEPS}\n"
            f"  build them first, from a normal shell:\n"
            f"  python3 {os.path.join(REPO, 'free_edition', 'tools', 'setup_inproc.py')}")

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

    # Purge before importing so edits to src/server.py, the utils it imports, or
    # anything under free_edition are picked up on reload. Also clears any
    # partial "src" import left by a prior failed boot attempt in this same
    # console session. Both packages: a purge that covered only src.* would
    # reload upstream while silently re-running the free-edition code the edit
    # replaced. Safe for free_edition because this script is exec'd rather than
    # imported, so it has no module identity to destroy.
    doomed = [name for name in list(sys.modules)
              if name in ("src", "free_edition")
              or name.startswith(("src.", "free_edition."))]
    for name in doomed:
        del sys.modules[name]

    from free_edition.inproc import dispatch, encoding, launcher, shim, streams

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

    # Upstream is imported and pristine on disk; our layer goes in on top of it
    # at runtime. No granular_mcp and no dash here: the two subtitle tools hang
    # off the granular FastMCP instance only, and the dashboard card belongs to
    # the dashboard boot. What this call is for on the compound path is the
    # autolaunch guard and the whisperX registration, both of which have to land
    # before dispatch wraps the registry and before the server answers anything.
    import free_edition.integrate as integrate

    print()
    registrations = integrate.install_all(console_resolve=_console_resolve,
                                          repo_root=REPO)
    _print_registrations(registrations)

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
    print(f"  encoding: open() default was "
          f"{encoding_report['locale_getencoding']}, now utf-8")
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
